"""Chargers with more than one connector: each plug runs its own session.

State and rules are tested directly; the pipeline is driven with hand-built
OCPP frames exactly as a dual-connector charger sends them.
"""

import json

import pytest

from proxy import pipeline, rules
from proxy.state import ProxyState
from shared.schemas import StationStatus, TelemetryEvent, ThreatEvent


# ---------------------------------------------------------------- state

def test_two_plugs_run_independent_sessions():
    state = ProxyState()
    p1 = state.apply_start_transaction("CP-01", 1001, connector_id=1)
    p2 = state.apply_start_transaction("CP-01", 1002, connector_id=2)
    assert p1.session_live and p2.session_live
    assert state.txn_owner[1001] == ("CP-01", 1) and state.txn_owner[1002] == ("CP-01", 2)

    stopped = state.apply_stop_transaction("CP-01", 1001)
    assert stopped is p1 and p1.status == StationStatus.FINISHING
    assert p2.session_live and p2.status == StationStatus.CHARGING  # the other car keeps charging


def test_stop_for_an_unknown_transaction_changes_nothing():
    state = ProxyState()
    p1 = state.apply_start_transaction("CP-01", 1001, connector_id=1)
    assert state.apply_stop_transaction("CP-01", 9999) is None
    assert state.apply_stop_transaction("CP-02", 1001) is None  # another charger's transaction
    assert p1.session_live


def test_cards_are_tracked_per_plug():
    state = ProxyState()
    heard = []
    state.tag_listener = lambda tag, label, live: heard.append((tag, label, live))
    state.apply_start_transaction("CP-01", 1001, connector_id=1, id_tag="TAG-A")
    state.apply_start_transaction("CP-01", 1002, connector_id=2, id_tag="TAG-B")
    assert sorted(state.live_sessions()) == [("TAG-A", "CP-01"), ("TAG-B", "CP-01/2")]
    assert state.tag_owner("TAG-B") == "CP-01"

    state.reset_session("CP-01")  # socket dropped: both sessions end
    assert state.live_sessions() == []
    assert ("TAG-A", "CP-01", False) in heard and ("TAG-B", "CP-01/2", False) in heard


def test_grid_sums_every_charging_plug_and_drops_a_quarantined_charger():
    state = ProxyState()
    for cid, kw in ((1, 60.0), (2, 60.0)):
        plug = state.apply_start_transaction("CP-01", 1000 + cid, connector_id=cid)
        plug.last_power_reported_kw = kw
        plug.record_electrical(ac_power_kw=kw / 0.95, ac_voltage_v=421.0, frequency_hz=50.0, ts=1000.0)
    snap = state.grid(now=1001.0)
    assert snap.active_stations == 2 and snap.output_kw == 120.0
    assert snap.input_kw == pytest.approx(120.0 / 0.95)

    state.quarantine("CP-01")
    assert state.grid(now=1001.0).active_stations == 0


def test_main_meter_supplies_bus_voltage_but_is_not_a_charging_plug():
    state = ProxyState()
    state.station("CP-01").connector(0).record_electrical(None, 419.5, 49.99, ts=1000.0)
    snap = state.grid(now=1001.0)
    assert snap.active_stations == 0 and snap.lt_voltage_v == 419.5 and snap.frequency_hz == 49.99


# ---------------------------------------------------------------- rules

def test_r5_rejects_a_transaction_reported_on_the_wrong_plug():
    state = ProxyState()
    state.apply_start_transaction("CP-01", 1001, connector_id=1)
    state.apply_start_transaction("CP-01", 1002, connector_id=2)
    assert rules.check_txn_integrity(state, "CP-01", 1002, 2) is None
    v = rules.check_txn_integrity(state, "CP-01", 1002, 1)
    assert v is not None and v.rule_id == "R5_TXN_INTEGRITY" and "connector 2" in v.reason


def test_r3_counts_a_rapid_restart_but_not_the_next_car():
    finishing = StationStatus.FINISHING
    assert rules.is_oscillation_transition("StartTransaction", finishing, since_stop_sec=0.5)
    assert not rules.is_oscillation_transition("StartTransaction", finishing, since_stop_sec=40.0)
    assert not rules.is_oscillation_transition("StartTransaction", StationStatus.AVAILABLE, since_stop_sec=0.5)
    assert rules.is_oscillation_transition("StopTransaction", StationStatus.CHARGING)


# ---------------------------------------------------------------- pipeline on real frames

class Feed:
    def __init__(self):
        self.events = []

    async def __call__(self, event):
        self.events.append(event)

    def of(self, cls):
        return [e for e in self.events if isinstance(e, cls)]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(pipeline, "THROTTLE", rules.ThreatThrottle())
    monkeypatch.setattr(pipeline, "FEDERATION", None)
    monkeypatch.setattr(pipeline, "ENFORCE", True)
    monkeypatch.setattr(pipeline, "_log_incident", lambda event: None)


def _call(uid, action, payload):
    return json.dumps([2, uid, action, payload])


def _meter(connector_id, txn, power_kw, energy_kwh, soc=40.0, volts=715.0):
    sampled = [
        {"value": f"{power_kw:.1f}", "measurand": "Power.Active.Import", "unit": "kW", "location": "Outlet"},
        {"value": f"{energy_kwh:.3f}", "measurand": "Energy.Active.Import.Register", "unit": "kWh", "location": "Outlet"},
        {"value": f"{soc:.1f}", "measurand": "SoC", "unit": "Percent", "location": "EV"},
        {"value": f"{volts:.1f}", "measurand": "Voltage", "unit": "V", "location": "Outlet"},
        {"value": f"{power_kw * 1000 / volts:.1f}", "measurand": "Current.Import", "unit": "A", "location": "Outlet"},
    ]
    payload = {"connectorId": connector_id, "meterValue": [{"timestamp": "t", "sampledValue": sampled}]}
    if txn is not None:
        payload["transactionId"] = txn
    return payload


async def _start(state, osc, feed, connector_id, tag, txn):
    uid = f"s{connector_id}"
    await pipeline.inspect_upstream(
        state, osc, "CP-01",
        _call(uid, "StartTransaction", {"connectorId": connector_id, "idTag": tag, "meterStart": 0, "timestamp": "t"}),
        feed,
    )
    pipeline.inspect_downstream(state, "CP-01", json.dumps([3, uid, {"transactionId": txn, "idTagInfo": {"status": "Accepted"}}]))


@pytest.mark.asyncio
async def test_two_cars_on_one_charger_raise_no_alarms():
    state, osc, feed = ProxyState(), rules.OscillationDetector(), Feed()
    await _start(state, osc, feed, 1, "TAG-A", 1001)
    await _start(state, osc, feed, 2, "TAG-B", 1002)
    for k in range(1, 6):
        for cid, txn in ((1, 1001), (2, 1002)):
            verdict = await pipeline.inspect_upstream(
                state, osc, "CP-01", _call(f"m{cid}{k}", "MeterValues", _meter(cid, txn, 60.0, 60.0 * k / 3600)), feed
            )
            assert verdict.forward
    assert [t for t in feed.of(ThreatEvent) if t.tier == 1] == []
    telemetry = feed.of(TelemetryEvent)
    assert {t.connector_id for t in telemetry} == {1, 2}
    assert {t.transaction_id for t in telemetry if t.connector_id == 2} == {1002}

    # The first car leaves; the second keeps charging without an R1.
    await pipeline.inspect_upstream(
        state, osc, "CP-01",
        _call("x1", "StopTransaction", {"transactionId": 1001, "meterStop": 0, "timestamp": "t"}), feed,
    )
    await pipeline.inspect_upstream(state, osc, "CP-01", _call("m26", "MeterValues", _meter(2, 1002, 60.0, 0.1)), feed)
    assert [t.rule_id for t in feed.of(ThreatEvent) if t.tier == 1] == []


@pytest.mark.asyncio
async def test_main_meter_and_idle_clock_aligned_readings_are_not_flagged():
    state, osc, feed = ProxyState(), rules.OscillationDetector(), Feed()
    main = {"connectorId": 0, "meterValue": [{"timestamp": "t", "sampledValue": [
        {"value": "250000", "measurand": "Energy.Active.Import.Register", "unit": "Wh"},
        {"value": "420.4", "measurand": "Voltage", "unit": "V", "location": "Inlet", "phase": "L1-L2"},
        {"value": "50.01", "measurand": "Frequency", "location": "Inlet"},
    ]}]}
    await pipeline.inspect_upstream(state, osc, "CP-01", _call("mm", "MeterValues", main), feed)
    await pipeline.inspect_upstream(state, osc, "CP-01", _call("idle", "MeterValues", _meter(2, None, 0.0, 12.0)), feed)
    assert feed.of(ThreatEvent) == [] and feed.of(TelemetryEvent) == []
    assert state.grid().lt_voltage_v == 420.4


@pytest.mark.asyncio
async def test_a_spoof_on_plug_two_quarantines_the_whole_charger():
    state, osc, feed = ProxyState(), rules.OscillationDetector(), Feed()
    await _start(state, osc, feed, 1, "TAG-A", 1001)
    await _start(state, osc, feed, 2, "TAG-B", 1002)
    await pipeline.inspect_upstream(state, osc, "CP-01", _call("m1", "MeterValues", _meter(1, 1001, 60.0, 0.02)), feed)
    spoof = _meter(2, 1002, 60.0, 0.02)
    spoof["meterValue"][0]["sampledValue"][0]["value"] = "5.0"  # power rewritten, V x A untouched

    verdict = await pipeline.inspect_upstream(state, osc, "CP-01", _call("m2", "MeterValues", spoof), feed)

    assert verdict.start_quarantine and not verdict.forward
    r7 = [t for t in feed.of(ThreatEvent) if t.rule_id == "R7_ELECTRICAL"][0]
    assert r7.connector_id == 2 and r7.reason.startswith("connector 2:")
    last = {t.connector_id: t for t in feed.of(TelemetryEvent)}
    assert last[1].status == StationStatus.QUARANTINED and last[2].status == StationStatus.QUARANTINED
    assert last[1].power_kw == 0.0 and last[2].power_kw == 0.0


# ---------------------------------------------------------------- stop and restart

@pytest.mark.asyncio
async def test_a_stopped_session_shows_finishing_at_zero_kw():
    state, osc, feed = ProxyState(), rules.OscillationDetector(), Feed()
    await _start(state, osc, feed, 1, "TAG-A", 1001)
    await pipeline.inspect_upstream(state, osc, "CP-01", _call("m1", "MeterValues", _meter(1, 1001, 120.0, 0.03)), feed)
    await pipeline.inspect_upstream(
        state, osc, "CP-01",
        _call("x1", "StopTransaction", {"transactionId": 1001, "meterStop": 30, "timestamp": "t"}), feed,
    )
    last = feed.of(TelemetryEvent)[-1]
    assert last.status == StationStatus.FINISHING and last.power_kw == 0.0 and last.dc_current_a == 0.0


def test_a_new_session_does_not_inherit_the_previous_supply_reading():
    # During an oscillation the dashboard showed 0 kW to the cars but 511 kVA
    # and 711 A: a restarted plug kept the last session's 126.5 kW inlet reading.
    state = ProxyState()
    plug = state.apply_start_transaction("CP-01", 1001, connector_id=1)
    plug.last_power_reported_kw = 120.0
    plug.record_electrical(ac_power_kw=126.5, ac_voltage_v=420.7, frequency_hz=50.0, ts=1000.0)
    state.apply_stop_transaction("CP-01", 1001)
    state.apply_start_transaction("CP-01", 1002, connector_id=1)   # no MeterValues yet
    snap = state.grid(now=1001.0)
    assert snap.output_kw == 0.0
    assert snap.input_kw < 1.0     # standby at most, not the old 126.5 kW
