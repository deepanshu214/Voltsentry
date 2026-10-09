"""proxy.pipeline end to end on real twin frames, with no sockets.

Drives inspect_upstream / inspect_downstream exactly as main.py does and
captures what would be broadcast to the dashboard.
"""

import json

import pytest

from proxy import pipeline, rules
from proxy.state import ProxyState
from shared.schemas import GridEvent, StationStatus, TelemetryEvent, ThreatEvent
from simulator.twin import FleetManager


class Feed:
    def __init__(self):
        self.events = []

    async def __call__(self, event):
        self.events.append(event)

    def of(self, cls):
        return [e for e in self.events if isinstance(e, cls)]


def _call(uid, action, payload):
    return json.dumps([2, uid, action, payload])


def _result(uid, payload):
    return json.dumps([3, uid, payload])


@pytest.fixture(autouse=True)
def _fresh_throttle(monkeypatch):
    monkeypatch.setattr(pipeline, "THROTTLE", rules.ThreatThrottle())
    monkeypatch.setattr(pipeline, "FEDERATION", None)
    monkeypatch.setattr(pipeline, "ENFORCE", True)
    monkeypatch.setattr(pipeline, "_log_incident", lambda event: None)


async def _start_session(state, osc, feed, cpid, tag, txn):
    for uid, action, payload, reply in (
        ("b1", "BootNotification", {"chargePointVendor": "v", "chargePointModel": "m"}, {"status": "Accepted"}),
        ("a1", "Authorize", {"idTag": tag}, {"idTagInfo": {"status": "Accepted"}}),
        ("s1", "StartTransaction", {"connectorId": 1, "idTag": tag, "meterStart": 0, "timestamp": "t"},
         {"transactionId": txn, "idTagInfo": {"status": "Accepted"}}),
    ):
        verdict = await pipeline.inspect_upstream(state, osc, cpid, _call(uid, action, payload), feed)
        assert verdict.forward
        pipeline.inspect_downstream(state, cpid, _result(uid, reply))


@pytest.mark.asyncio
async def test_meter_spoof_is_quarantined_and_the_dashboard_sees_it():
    state, osc, feed = ProxyState(), rules.OscillationDetector(), Feed()
    fleet = FleetManager("ws://x", station_count=1)
    bay = fleet.stations["CP-01"]
    await _start_session(state, osc, feed, "CP-01", "TAG-CP-01", 1001)
    bay.transaction_id = 1001

    # One honest sample, then the spoof.
    for mode in ("clean", "meter_spoof"):
        bay.mode = mode
        p, soc, e = bay.battery.tick(1.0)
        payload, _ = bay.meter_payload(p, soc, e)
        verdict = await pipeline.inspect_upstream(state, osc, "CP-01", _call(f"m-{mode}", "MeterValues", payload), feed)

    assert verdict.start_quarantine and not verdict.forward
    assert verdict.quarantine_reason.startswith("R7_ELECTRICAL")
    threats = feed.of(ThreatEvent)
    tier1 = [t for t in threats if t.tier == 1]
    assert [(t.rule_id, t.action_taken) for t in tier1] == [("R7_ELECTRICAL", "quarantined")]

    last = feed.of(TelemetryEvent)[-1]
    assert last.status == StationStatus.QUARANTINED and last.power_kw == 0.0 and last.dc_current_a == 0.0
    honest = feed.of(TelemetryEvent)[0]
    assert honest.dc_voltage_v and honest.dc_current_a and honest.ac_voltage_v and honest.frequency_hz

    # A quarantined bay drops out of the transformer aggregate.
    assert state.grid().active_stations == 0


@pytest.mark.asyncio
async def test_card_already_charging_on_another_bay_is_blocked_at_authorize():
    state, osc, feed = ProxyState(), rules.OscillationDetector(), Feed()
    await _start_session(state, osc, feed, "CP-01", "TAG-CP-01", 1001)
    assert state.tag_owner("TAG-CP-01") == "CP-01"

    verdict = await pipeline.inspect_upstream(
        state, osc, "CP-02", _call("x1", "Authorize", {"idTag": "TAG-CP-01"}), feed
    )
    assert not verdict.forward  # the CSMS never sees the cloned card
    assert json.loads(verdict.reply) == [3, "x1", {"idTagInfo": {"status": "Blocked"}}]
    threat = feed.of(ThreatEvent)[-1]
    assert (threat.rule_id, threat.action_taken, threat.station_id) == ("R8_TAG_CLONE", "frame_dropped", "CP-02")

    # When the first session ends the card is free again.
    await pipeline.inspect_upstream(
        state, osc, "CP-01",
        _call("st1", "StopTransaction", {"transactionId": 1001, "meterStop": 0, "timestamp": "t", "idTag": "TAG-CP-01"}),
        feed,
    )
    verdict = await pipeline.inspect_upstream(
        state, osc, "CP-02", _call("x2", "Authorize", {"idTag": "TAG-CP-01"}), feed
    )
    assert verdict.forward and verdict.reply is None


@pytest.mark.asyncio
async def test_rejected_start_does_not_mark_the_card_live():
    state, osc, feed = ProxyState(), rules.OscillationDetector(), Feed()
    await pipeline.inspect_upstream(
        state, osc, "CP-01",
        _call("s1", "StartTransaction", {"connectorId": 1, "idTag": "TAG-X", "meterStart": 0, "timestamp": "t"}), feed,
    )
    pipeline.inspect_downstream(state, "CP-01", _result("s1", {"transactionId": 7, "idTagInfo": {"status": "Invalid"}}))
    assert state.tag_owner("TAG-X") is None


def test_grid_snapshot_converts_to_real_units():
    state = ProxyState()
    st = state.station("CP-01")
    st.status = StationStatus.CHARGING
    st.last_power_reported_kw = 120.0
    st.record_electrical(ac_power_kw=126.4, ac_voltage_v=420.7, frequency_hz=50.01, ts=1000.0)
    snap = state.grid(now=1001.0)
    assert (snap.output_kw, snap.input_kw, snap.active_stations) == (120.0, 126.4, 1)
    assert snap.lt_voltage_v == 420.7 and snap.frequency_hz == 50.01
    stale = state.grid(now=1010.0)  # readings older than 5 s no longer count
    assert stale.lt_voltage_v is None and stale.frequency_hz is None
    assert GridEvent.model_fields["transformer_capacity_kva"].default == 1250.0


# ---------------------------------------------------------------- monitor-only mode
# A new site runs with --mode monitor until its rules are proven on its own
# meters: every detection is reported with what enforce mode would have done,
# and nothing is blocked.


class FakeFederation:
    def __init__(self, remote_owner=None, blocked=None):
        self.remote_owner, self.blocked = remote_owner, blocked
        self.card_iocs = []

    def check_card(self, id_tag):
        return self.remote_owner, self.blocked

    def publish_card_ioc(self, id_tag, reason, ttl_sec=3600.0):
        self.card_iocs.append(id_tag)


@pytest.mark.asyncio
async def test_monitor_mode_reports_a_meter_spoof_but_keeps_the_charger_online(monkeypatch):
    monkeypatch.setattr(pipeline, "ENFORCE", False)
    state, osc, feed = ProxyState(), rules.OscillationDetector(), Feed()
    fleet = FleetManager("ws://x", station_count=1)
    bay = fleet.stations["CP-01"]
    await _start_session(state, osc, feed, "CP-01", "TAG-CP-01", 1001)
    bay.transaction_id = 1001
    bay.mode = "meter_spoof"
    p, soc, e = bay.battery.tick(1.0)
    payload, _ = bay.meter_payload(p, soc, e)

    verdict = await pipeline.inspect_upstream(state, osc, "CP-01", _call("m1", "MeterValues", payload), feed)

    assert verdict.forward and not verdict.start_quarantine and verdict.reply is None
    r7 = [t for t in feed.of(ThreatEvent) if t.rule_id == "R7_ELECTRICAL"]
    assert [(t.action_taken, t.recommended_action) for t in r7] == [("logged", "quarantined")]
    assert not state.is_quarantined("CP-01")
    assert all(t.status == StationStatus.CHARGING for t in feed.of(TelemetryEvent))
    assert state.grid().active_stations == 1


@pytest.mark.asyncio
async def test_monitor_mode_reports_a_cloned_card_but_lets_the_csms_decide(monkeypatch):
    monkeypatch.setattr(pipeline, "ENFORCE", False)
    fed = FakeFederation(remote_owner=("VS-VELLORE", "CP-05"))
    monkeypatch.setattr(pipeline, "FEDERATION", fed)
    state, osc, feed = ProxyState(), rules.OscillationDetector(), Feed()

    verdict = await pipeline.inspect_upstream(
        state, osc, "CHN-02", _call("a1", "Authorize", {"idTag": "TAG-CP-05"}), feed
    )

    assert verdict.forward and verdict.reply is None  # the CSMS answers, not the proxy
    threat = feed.of(ThreatEvent)[-1]
    assert (threat.rule_id, threat.action_taken, threat.recommended_action) == (
        "R8_TAG_CLONE", "logged", "frame_dropped")
    assert fed.card_iocs == []  # unproven detections never become network-wide blocks
    assert state.station("CHN-02").authorized


@pytest.mark.asyncio
async def test_enforce_mode_blocks_the_cloned_card_and_shares_it():
    fed = FakeFederation(remote_owner=("VS-VELLORE", "CP-05"))
    pipeline.FEDERATION = fed
    try:
        state, osc, feed = ProxyState(), rules.OscillationDetector(), Feed()
        verdict = await pipeline.inspect_upstream(
            state, osc, "CHN-02", _call("a1", "Authorize", {"idTag": "TAG-CP-05"}), feed
        )
    finally:
        pipeline.FEDERATION = None
    assert not verdict.forward and verdict.reply is not None
    threat = feed.of(ThreatEvent)[-1]
    assert (threat.action_taken, threat.recommended_action) == ("frame_dropped", None)
    assert fed.card_iocs == ["TAG-CP-05"]
