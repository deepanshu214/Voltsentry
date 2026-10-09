import json
import pytest
from shared.schemas import AttackTrigger, AttackType
from simulator.battery import Battery
from simulator.twin import FleetManager, StationTwin


# ==============================================================================
# Pure Battery Model Tests
# ==============================================================================

def test_soc_monotonic():
    """Assert SoC is monotonically increasing during charging."""
    battery = Battery(initial_soc=10.0)
    last_soc = battery.soc

    while battery.soc < 100.0:
        power_kw, soc, energy_kwh = battery.tick(1.0)
        assert soc >= last_soc, f"SoC decreased from {last_soc} to {soc}"
        last_soc = soc

    assert battery.soc == 100.0


def test_power_never_exceeds_150kw():
    """Assert power never exceeds 150 kW limit under any configuration."""
    battery = Battery(initial_soc=0.0, max_power_kw=200.0)
    while battery.soc < 100.0:
        power_kw, soc, energy_kwh = battery.tick(1.0)
        assert power_kw <= 150.0, f"Power {power_kw} kW exceeded 150 kW cap"


def test_cv_taper_power_at_95_soc_below_60kw():
    """Assert power at 95% SoC is below 60 kW (CV taper phase)."""
    battery = Battery(initial_soc=94.5)

    while battery.soc < 95.0:
        battery.tick(1.0)

    power_kw, soc, _ = battery.tick(1.0)
    assert soc >= 95.0
    assert power_kw < 60.0, f"Power at {soc}% SoC was {power_kw} kW, expected < 60 kW"


def test_energy_accumulation():
    """Assert cumulative kWh is tracked accurately."""
    battery = Battery(initial_soc=20.0, max_power_kw=120.0)
    initial_energy = battery.energy_kwh

    power_kw, soc, energy_kwh = battery.tick(3600.0)  # 1 hour
    expected_energy = initial_energy + 120.0
    assert pytest.approx(energy_kwh, rel=1e-3) == expected_energy


def test_battery_edge_cases():
    """Assert battery handles 100% initial SoC and zero dt gracefully."""
    battery = Battery(initial_soc=100.0)
    power, soc, energy = battery.tick(10.0)
    assert power == 0.0
    assert soc == 100.0
    assert energy == 0.0

    battery_zero_dt = Battery(initial_soc=50.0)
    power, soc, energy = battery_zero_dt.tick(0.0)
    assert soc == 50.0
    assert energy == 0.0


# ==============================================================================
# Fleet Manager & Attack Trigger Unit Tests
# ==============================================================================

def test_fleet_manager_initialization():
    """Verify fleet manager creates 8 stations with staggered SoCs and start delays."""
    fleet = FleetManager(base_url="ws://localhost:8000", station_count=8)
    assert len(fleet.stations) == 8

    # Verify stations are CP-01 through CP-08
    station_ids = list(fleet.stations.keys())
    assert station_ids == [f"CP-{i:02d}" for i in range(1, 9)]

    # Check staggered initial SoCs
    socs = [s.initial_soc for s in fleet.stations.values()]
    assert len(set(socs)) > 1  # Not all identical


def test_fleet_manager_meter_spoof_trigger():
    """Verify Meter Spoof attack trigger targets named station."""
    fleet = FleetManager(base_url="ws://localhost:8000", station_count=8)
    trigger = AttackTrigger(attack_type=AttackType.METER_SPOOF, station_id="CP-03")

    ack = fleet.apply_trigger(trigger)
    assert ack.ok is True
    assert fleet.stations["CP-03"].mode == "meter_spoof"
    assert fleet.stations["CP-01"].mode == "clean"


def test_fleet_manager_oscillate_trigger():
    """Verify Fleet Oscillate attack trigger toggles all stations when station_id is None."""
    fleet = FleetManager(base_url="ws://localhost:8000", station_count=8)
    trigger = AttackTrigger(attack_type=AttackType.FLEET_OSCILLATE, station_id=None)

    ack = fleet.apply_trigger(trigger)
    assert ack.ok is True
    for s in fleet.stations.values():
        assert s.mode == "oscillate"


def test_fleet_manager_subtle_drift_trigger():
    """Verify Subtle Drift attack trigger sets drift mode and resets ticks."""
    fleet = FleetManager(base_url="ws://localhost:8000", station_count=8)
    station = fleet.stations["CP-02"]
    station.drift_ticks = 25

    trigger = AttackTrigger(attack_type=AttackType.SUBTLE_DRIFT, station_id="CP-02")
    ack = fleet.apply_trigger(trigger)
    assert ack.ok is True
    assert station.mode == "drift"
    assert station.drift_ticks == 0


def test_fleet_manager_reset_trigger():
    """Verify Reset returns all stations to clean mode and clears quarantine."""
    fleet = FleetManager(base_url="ws://localhost:8000", station_count=8)
    fleet.stations["CP-01"].mode = "oscillate"
    fleet.stations["CP-02"].mode = "drift"
    fleet.stations["CP-03"].mode = "meter_spoof"
    fleet.stations["CP-03"].is_quarantined = True

    trigger = AttackTrigger(attack_type=AttackType.RESET)
    ack = fleet.apply_trigger(trigger)
    assert ack.ok is True

    for s in fleet.stations.values():
        assert s.mode == "clean"
        assert s.drift_ticks == 0
        assert s.is_quarantined is False


def test_subtle_drift_math():
    """Verify drift compounds at 2% per tick and reaches target decay in ~40 ticks."""
    power = 120.0
    for _ in range(40):
        power *= 0.98
    # After 40 ticks, power should decay smoothly to between 50 and 55 kW
    assert 50.0 < power < 55.0


# ==============================================================================
# Inbound CALL Handling Tests ([FIX-6])
# ==============================================================================

@pytest.mark.asyncio
async def test_station_twin_inbound_change_availability():
    """Assert StationTwin handles inbound ChangeAvailability CALL and replies with CALLRESULT."""
    station = StationTwin("CP-01", "ws://localhost:8000")

    sent_messages = []
    class DummyWS:
        async def send(self, data):
            sent_messages.append(data)

    station.ws = DummyWS()

    # 1. Inoperative -> Quarantines station
    await station._handle_inbound_call(
        "uuid-123", "ChangeAvailability", {"connectorId": 1, "type": "Inoperative"}
    )
    assert station.is_quarantined is True
    assert len(sent_messages) == 1
    res1 = json.loads(sent_messages[0])
    assert res1 == [3, "uuid-123", {"status": "Accepted"}]

    # 2. Operative -> Clears quarantine
    await station._handle_inbound_call(
        "uuid-124", "ChangeAvailability", {"connectorId": 1, "type": "Operative"}
    )
    assert station.is_quarantined is False
    assert len(sent_messages) == 2
    res2 = json.loads(sent_messages[1])
    assert res2 == [3, "uuid-124", {"status": "Accepted"}]


@pytest.mark.asyncio
async def test_station_twin_inbound_reset():
    """Assert StationTwin handles inbound Reset CALL, resets state, and replies with CALLRESULT."""
    station = StationTwin("CP-01", "ws://localhost:8000")
    station.mode = "meter_spoof"
    station.is_quarantined = True
    station.drift_ticks = 15

    sent_messages = []
    class DummyWS:
        async def send(self, data):
            sent_messages.append(data)

    station.ws = DummyWS()

    await station._handle_inbound_call("uuid-999", "Reset", {"type": "Soft"})
    assert station.mode == "clean"
    assert station.is_quarantined is False
    assert station.drift_ticks == 0
    assert len(sent_messages) == 1
    res = json.loads(sent_messages[0])
    assert res == [3, "uuid-999", {"status": "Accepted"}]


# --------------------------------------------- CV taper vs the R2 envelope
# The proxy's R2 rule trips on power_kw > 60.0 while soc > 80.0 (CONTEXT.md
# 5.B). An honest CC-CV charger must therefore be under 60 kW as soon as it is
# past the 80% knee -- otherwise every clean station in the fleet quarantines
# itself on a false positive the moment it crosses 80%, which is exactly what
# the fleet was doing.

def test_cv_taper_is_under_the_r2_envelope_across_the_whole_taper():
    """Above the 80% knee an honest charger must stay under R2's 60 kW limit."""
    for soc in (80.5, 82.0, 85.0, 88.0, 90.0, 95.0, 99.0):
        battery = Battery(initial_soc=soc, max_power_kw=120.0)
        assert battery.power_kw < 60.0, (
            f"power at {soc}% SoC was {battery.power_kw:.1f} kW; R2 quarantines "
            f"anything over 60 kW above 80% SoC"
        )


def test_constant_current_phase_holds_through_the_bulk_of_the_charge():
    """The taper fix must not touch the CC phase the demo charts depend on."""
    for soc in (10.0, 40.0, 77.9):
        battery = Battery(initial_soc=soc, max_power_kw=120.0)
        assert battery.power_kw == 120.0


def test_power_falls_continuously_into_the_cv_phase():
    """No cliff at the knee.

    R2 forbids >60 kW above 80% SoC, so power must already be under 60 by the
    time SoC crosses 80. Dropping it there in a single tick spikes dp_dt to
    ~-62 kW/s, which the Tier-2 forest scores as an anomaly (0.81, over the
    0.65 threshold) -- trading a Tier-1 false positive for a Tier-2 one. The
    hand-off is eased in over the approach to the knee instead.
    """
    prev = 120.0
    for tenth in range(750, 1000):
        soc = tenth / 10.0
        power = Battery(initial_soc=soc, max_power_kw=120.0).power_kw
        assert power <= prev + 1e-9, f"power rose at {soc}% SoC"
        assert prev - power < 12.0, (
            f"power fell {prev - power:.1f} kW in 0.1% SoC at {soc}% -- that is a "
            f"cliff, and dp_dt will read as anomalous"
        )
        prev = power


def test_reset_returns_stations_to_a_low_soc_battery():
    """`r` must hand back a fleet that can actually charge again.

    Without this, reset reconnects stations at whatever SoC they had reached --
    typically above 80% -- so they re-quarantine within seconds and the reset
    looks like it did nothing.
    """
    fleet = FleetManager(base_url="ws://localhost:8000", station_count=8)
    for s in fleet.stations.values():
        s.battery.soc = 96.0
        s.battery.energy_kwh = 61.0

    fleet.apply_trigger(AttackTrigger(attack_type=AttackType.RESET))

    for s in fleet.stations.values():
        assert s.battery.soc < 80.0, f"{s.cpid} came back at {s.battery.soc}% SoC"
        assert s.battery.energy_kwh == 0.0


# ==============================================================================
# Pack electrical model (voltage / current)
# ==============================================================================

from simulator.battery import (  # noqa: E402
    CELLS_IN_SERIES,
    MAX_CELL_V,
    open_circuit_voltage_v,
    terminal_voltage_current,
)
from shared.electrical import CHARGER_MAX_DC_A  # noqa: E402


def test_pack_open_circuit_voltage_spans_an_800v_class_pack():
    assert open_circuit_voltage_v(0.0) == pytest.approx(192 * 3.30)
    assert open_circuit_voltage_v(100.0) == pytest.approx(192 * 4.18)
    prev = 0.0
    for soc in range(0, 101):
        v = open_circuit_voltage_v(float(soc))
        assert v >= prev  # OCV rises with charge
        prev = v


def test_power_equals_voltage_times_current():
    for soc in (5.0, 40.0, 79.0, 92.0):
        v, i = terminal_voltage_current(120.0, soc)
        assert v * i / 1000.0 == pytest.approx(120.0, rel=1e-9)
        assert v > open_circuit_voltage_v(soc)  # charging lifts the terminal by I*R


def test_full_power_stays_inside_the_ccs2_cable_current_limit():
    # The worst case is the emptiest pack: lowest voltage -> highest current.
    for soc in range(0, 79):
        _, i = terminal_voltage_current(120.0, float(soc))
        assert i <= CHARGER_MAX_DC_A, f"{i:.0f} A at {soc}% SoC"


def test_terminal_voltage_never_exceeds_max_charge_voltage_across_a_session():
    battery = Battery(initial_soc=10.0, max_power_kw=120.0)
    for _ in range(4000):
        p, soc, _ = battery.tick(1.0)
        assert battery.voltage_v <= CELLS_IN_SERIES * MAX_CELL_V
        assert battery.voltage_v * battery.current_a / 1000.0 == pytest.approx(p, abs=1e-6)
        if soc >= 100.0:
            break


def test_idle_pack_draws_no_current():
    v, i = terminal_voltage_current(0.0, 55.0)
    assert i == 0.0 and v == pytest.approx(open_circuit_voltage_v(55.0))
