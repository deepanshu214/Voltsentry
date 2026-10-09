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
