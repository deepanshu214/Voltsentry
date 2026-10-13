"""The twin's MeterValues, read back through the proxy's own parser and rules.

Pure: no sockets. Proves the simulated site speaks real OCPP units and that each
attack lands on the rule it is meant to.
"""

import pytest

from proxy.ocpp import extract_electrical, extract_meter_values
from proxy.rules import check_electrical
from shared import electrical as el
from shared.schemas import AttackTrigger, AttackType
from simulator.twin import FleetManager, SiteModel, StationTwin


def _charging_bay(mode: str = "clean", soc: float = 40.0) -> StationTwin:
    site = SiteModel(transformer_kva=1250.0)
    bay = StationTwin("CP-01", "ws://x", initial_soc=soc, site=site)
    site.stations[bay.cpid] = bay
    bay.mode = mode
    bay.transaction_id = 1001
    return bay


def _tick(bay: StationTwin):
    power, soc, energy = bay.battery.tick(1.0)
    bay.actual_power_kw = power
    payload, reported = bay.meter_payload(power, soc, energy)
    return payload, reported, power


def test_honest_bay_reports_consistent_real_units():
    payload, reported, actual = _tick(_charging_bay())
    mv, e = extract_meter_values(payload), extract_electrical(payload)
    assert mv["power_kw"] == pytest.approx(actual, abs=0.05)
    assert 600.0 < e.dc_voltage_v < 810.0           # 800 V-class pack
    assert 0.0 < e.dc_current_a <= el.CHARGER_MAX_DC_A
    assert 410.0 < e.ac_voltage_ll_v < 434.0         # three-phase LT bus, line-to-line
    assert el.GRID_BAND_HZ[0] <= e.frequency_hz <= el.GRID_BAND_HZ[1]
    assert e.ac_power_kw == pytest.approx(el.ac_input_kw(actual), abs=0.05)  # supply > output
    assert check_electrical(mv["power_kw"], e.dc_voltage_v, e.dc_current_a) is None


def test_meter_spoof_is_caught_by_r7_on_the_first_frame():
    payload, reported, actual = _tick(_charging_bay("meter_spoof"))
    mv, e = extract_meter_values(payload), extract_electrical(payload)
    assert reported == 5.0 and actual > 100.0
    v = check_electrical(mv["power_kw"], e.dc_voltage_v, e.dc_current_a)
    assert v is not None and v.rule_id == "R7_ELECTRICAL"


def test_drift_stays_self_consistent_so_only_ml_can_see_it():
    bay = _charging_bay("drift")
    for _ in range(40):
        payload, reported, actual = _tick(bay)
        mv, e = extract_meter_values(payload), extract_electrical(payload)
        assert check_electrical(mv["power_kw"], e.dc_voltage_v, e.dc_current_a) is None
    assert reported < 0.5 * actual  # well under-reported by now, yet V x I agrees


def test_site_bus_voltage_falls_as_bays_draw_power():
    fleet = FleetManager("ws://x", station_count=8)
    idle = fleet.site.lt_voltage_v()
    for bay in fleet.stations.values():
        bay.actual_power_kw = 120.0
    loaded = fleet.site.lt_voltage_v()
    assert idle == pytest.approx(el.LT_NO_LOAD_LL_V)
    assert 415.0 < loaded < idle
    assert fleet.site.load_kva() == pytest.approx(8 * el.bay_peak_kva())
    assert fleet.site.load_kva() < fleet.site.transformer_kva  # 1250 kVA covers 8 bays flat out


def test_grid_frequency_walk_stays_in_band():
    site = SiteModel(seed=3)
    lo, hi = el.GRID_BAND_HZ
    for _ in range(5000):
        site.step_frequency()
        assert lo <= site.frequency_hz <= hi


def test_prefix_names_a_second_site_distinctly():
    fleet = FleetManager("ws://x", station_count=4, prefix="CHN")
    assert list(fleet.stations) == ["CHN-01", "CHN-02", "CHN-03", "CHN-04"]
    assert fleet.stations["CHN-02"].id_tag == "TAG-CHN-02"


def test_tag_clone_swaps_the_card_and_reset_restores_it():
    fleet = FleetManager("ws://x", station_count=4, prefix="CHN")
    ack = fleet.apply_trigger(AttackTrigger(attack_type=AttackType.TAG_CLONE, station_id="CHN-02",
                                            params={"id_tag": "TAG-CP-03"}))
    assert ack.ok and fleet.stations["CHN-02"].id_tag == "TAG-CP-03"
    fleet.apply_trigger(AttackTrigger(attack_type=AttackType.RESET))
    assert fleet.stations["CHN-02"].id_tag == "TAG-CHN-02"


def test_tag_clone_without_a_card_is_refused():
    fleet = FleetManager("ws://x", station_count=2)
    ack = fleet.apply_trigger(AttackTrigger(attack_type=AttackType.TAG_CLONE, station_id="CP-01"))
    assert not ack.ok


@pytest.mark.asyncio
async def test_quarantined_bay_stops_loading_the_site_immediately():
    fleet = FleetManager("ws://x", station_count=8)
    for bay in fleet.stations.values():
        bay.actual_power_kw = 120.0
    loaded_v = fleet.site.lt_voltage_v()
    bay = fleet.stations["CP-03"]

    class DummyWS:
        async def send(self, msg):
            pass

    bay.ws = DummyWS()
    await bay._handle_inbound_call("q1", "ChangeAvailability", {"connectorId": 1, "type": "Inoperative"})
    assert bay.actual_power_kw == 0.0
    assert fleet.site.load_kva() == pytest.approx(7 * el.bay_peak_kva())
    assert fleet.site.lt_voltage_v() > loaded_v  # less load, less sag


class _ScriptedCsms:
    """Stands in for StationTwin.call: answers each Authorize from a script."""

    def __init__(self, auth_statuses):
        self.auth_statuses = list(auth_statuses)
        self.calls = []

    async def __call__(self, action, payload):
        self.calls.append((action, payload))
        if action == "Authorize":
            return {"idTagInfo": {"status": self.auth_statuses.pop(0)}}
        return {"transactionId": 4242, "idTagInfo": {"status": "Accepted"}}


@pytest.mark.asyncio
async def test_a_blocked_own_card_is_replaced_and_the_bay_charges(monkeypatch):
    monkeypatch.setattr("simulator.twin.CARD_REISSUE_SEC", 0.0)
    bay = _charging_bay()
    bay.call = csms = _ScriptedCsms(["Blocked", "Accepted"])
    await bay._start(bay.plug, "[test]")
    tried = [p["idTag"] for a, p in csms.calls if a == "Authorize"]
    assert tried == ["TAG-CP-01", "TAG-CP-01-R1"]
    start = [p for a, p in csms.calls if a == "StartTransaction"][0]
    assert start["idTag"] == "TAG-CP-01-R1" and bay.plug.charging
    # the replacement survives a reset: the old card is still blocked network-wide
    bay.plug.new_car(40.0)
    assert bay.id_tag == "TAG-CP-01-R1"


@pytest.mark.asyncio
async def test_a_bay_presenting_a_cloned_card_keeps_retrying_it(monkeypatch):
    monkeypatch.setattr("simulator.twin.AUTH_RETRY_SEC", 0.0)
    bay = _charging_bay()
    bay.id_tag_override = "TAG-CP-05"
    bay.call = csms = _ScriptedCsms(["Blocked", "Blocked", "Accepted"])
    await bay._start(bay.plug, "[test]")
    tried = [p["idTag"] for a, p in csms.calls if a == "Authorize"]
    assert tried == ["TAG-CP-05"] * 3 and bay.plug.card_issue == 0


def test_replacement_cards_stay_within_the_ocpp_idtag_limit():
    fleet = FleetManager("ws://x", station_count=6, prefix="CHN", connectors=2)
    plug = fleet.stations["CHN-06"].connectors[2]
    for _ in range(12):
        plug.reissue_card()
    assert plug.id_tag == "TAG-CHN-06-2-R12" and len(plug.id_tag) <= 20


# ---- the meter counts real time; oscillation really swings the load ------

class _FakeClock:
    def __init__(self, *readings):
        self.readings = list(readings)

    def __call__(self):
        return self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]


def test_the_meter_counts_real_time_between_ticks(monkeypatch):
    # The loop sleeps 1 s and then waits for the CSMS, so a turn takes ~1.03 s.
    # Counting it as 1 s let the register fall 2-3 % behind the reported power,
    # which looked like meter fraud on an honest charger.
    bay = _charging_bay()
    monkeypatch.setattr("simulator.twin.time.monotonic", _FakeClock(100.0, 101.031, 102.058))
    assert bay.plug.elapsed() == 1.0                       # first tick of a session
    assert bay.plug.elapsed() == pytest.approx(1.031)
    assert bay.plug.elapsed() == pytest.approx(1.027)
    bay.plug.new_car(20.0)
    assert bay.plug.last_tick is None                      # a new car starts its own clock


@pytest.mark.asyncio
async def test_oscillation_swings_between_full_power_and_nothing(monkeypatch):
    async def at_once(_delay):
        return None

    monkeypatch.setattr("simulator.twin.asyncio.sleep", at_once)
    # Even seconds off, odd seconds on: the clock reads just before each boundary.
    monkeypatch.setattr("simulator.twin.time.time", _FakeClock(999.4, 1000.4, 1001.4))
    bay = _charging_bay(mode="oscillate")
    plug = bay.plug
    plug.charging = True
    plug.actual_power_kw = 120.0
    bay.call = csms = _ScriptedCsms([])

    await bay._oscillate_switch(plug, "[test]")            # off
    assert [a for a, _ in csms.calls] == ["StopTransaction"]
    assert not plug.charging and plug.actual_power_kw == 0.0
    assert bay.site.load_kva() == 0.0

    await bay._oscillate_switch(plug, "[test]")            # on, at full power, reported honestly
    assert [a for a, _ in csms.calls][1:] == ["StartTransaction", "MeterValues"]
    assert plug.charging and plug.actual_power_kw == pytest.approx(120.0)
    mv = extract_meter_values(csms.calls[-1][1])
    assert mv["power_kw"] == pytest.approx(120.0)
    assert bay.site.load_kva() == pytest.approx(el.bay_peak_kva(), rel=0.01)


@pytest.mark.asyncio
async def test_oscillating_bays_stay_in_step_whenever_they_join(monkeypatch):
    async def at_once(_delay):
        return None

    monkeypatch.setattr("simulator.twin.asyncio.sleep", at_once)
    late, early = _charging_bay(mode="oscillate"), _charging_bay(mode="oscillate")
    for bay in (late, early):
        bay.plug.charging = True
        bay.call = _ScriptedCsms([])
    # `early` noticed the attack in second 1000, `late` only in second 1001.
    monkeypatch.setattr("simulator.twin.time.time", _FakeClock(999.9))
    await early._oscillate_switch(early.plug, "[test]")    # boundary 1000: even -> off
    monkeypatch.setattr("simulator.twin.time.time", _FakeClock(1000.9))
    await early._oscillate_switch(early.plug, "[test]")    # boundary 1001: odd -> on
    await late._oscillate_switch(late.plug, "[test]")      # joins at 1001: already on, no change
    assert early.plug.charging and late.plug.charging
    monkeypatch.setattr("simulator.twin.time.time", _FakeClock(1001.9))
    await early._oscillate_switch(early.plug, "[test]")
    await late._oscillate_switch(late.plug, "[test]")      # boundary 1002: both off together
    assert not early.plug.charging and not late.plug.charging
