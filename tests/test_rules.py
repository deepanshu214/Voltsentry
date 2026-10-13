"""Tests for proxy.rules — Tier-1 deterministic checks (CONTEXT.md §5.B).

Rules are pure functions over state + frame. Trip conditions are pinned in the
CONTEXT.md §5.B table; these tests encode that table exactly.
"""

from shared.schemas import StationStatus

from proxy.rules import (
    OscillationDetector,
    ThreatThrottle,
    check_electrical,
    check_id_tag,
    check_meter_fraud,
    check_physics,
    check_session_unique,
    check_state_order,
    is_oscillation_transition,
    should_quarantine,
    throttle_key,
)
from proxy.state import ProxyState


# ---------------------------------------------------------------- R2 physics
# power_kw > 150.0, OR power_kw > 60.0 while soc > 80.0 (CV taper), OR power_kw < 0

def test_r2_normal_charging_passes():
    assert check_physics(power_kw=120.0, soc=45.0) is None


def test_r2_over_150_kw_trips():
    v = check_physics(power_kw=155.0, soc=40.0)
    assert v is not None and v.rule_id == "R2_PHYSICS"


def test_r2_high_power_at_high_soc_trips_cv_taper():
    # 90 kW while SoC 85% violates the CV taper (should be < 60 kW up there).
    v = check_physics(power_kw=90.0, soc=85.0)
    assert v is not None and v.rule_id == "R2_PHYSICS"


def test_r2_moderate_power_at_high_soc_passes():
    # 55 kW at SoC 85% is within the CV taper band.
    assert check_physics(power_kw=55.0, soc=85.0) is None


def test_r2_negative_power_trips():
    v = check_physics(power_kw=-5.0, soc=50.0)
    assert v is not None and v.rule_id == "R2_PHYSICS"


# ------------------------------------------------------------- R1 state order
# MeterValues / StopTransaction for a plug with no live transaction trips.

def test_r1_metervalues_without_session_trips():
    v = check_state_order("CP-01", session_live=False, action="MeterValues")
    assert v is not None and v.rule_id == "R1_STATE_ORDER"


def test_r1_metervalues_with_live_session_passes():
    state = ProxyState()
    state.apply_authorize("CP-01")
    plug = state.apply_start_transaction("CP-01", 1041)
    assert check_state_order(plug.label, plug.session_live, "MeterValues") is None


def test_r1_does_not_require_a_prior_authorize():
    # OCPP 1.6 lets a charger authorise a card from its local list, so a
    # CSMS-confirmed StartTransaction alone makes the session live.
    state = ProxyState()
    plug = state.apply_start_transaction("CP-01", 1041)
    assert check_state_order(plug.label, plug.session_live, "MeterValues") is None


def test_r1_stop_without_session_trips():
    v = check_state_order("CP-02", session_live=False, action="StopTransaction")
    assert v is not None and v.rule_id == "R1_STATE_ORDER"


# ---------------------------------------------------------- R4 session unique
# Second handshake for a cpid with a live session trips.

def test_r4_first_connection_passes():
    state = ProxyState()
    assert check_session_unique(state, "CP-03") is None


def test_r4_duplicate_live_connection_trips():
    state = ProxyState()
    assert state.register_connection("CP-03")  # first socket
    v = check_session_unique(state, "CP-03")
    assert v is not None and v.rule_id == "R4_SESSION_UNIQUE"


# ------------------------------------------------------------ R3 oscillation
# >= 6 Start/Stop transitions fleet-wide in a 10 s window (fake clock).

def test_r3_five_transitions_in_window_pass():
    det = OscillationDetector()
    for i in range(5):
        det.record_transition(ts=float(i))
    assert det.check(now=4.0) is None


def test_r3_six_transitions_in_window_trip():
    det = OscillationDetector()
    for i in range(6):
        det.record_transition(ts=float(i))
    v = det.check(now=5.0)
    assert v is not None and v.rule_id == "R3_OSCILLATION"


def test_r3_old_transitions_outside_window_are_pruned():
    det = OscillationDetector()
    for i in range(6):
        det.record_transition(ts=float(i))  # events at t=0..5
    # At t=20, all six are older than the 10 s window -> no trip.
    assert det.check(now=20.0) is None


# Which frames count as an oscillation transition. A fleet of 8 stations booting
# fires 8 StartTransactions inside the 10 s window; if those counted, R3 would
# trip on normal startup before any attack. Only a *re-start* (a station that
# just stopped) is part of an oscillation cycle.

def test_r3_stop_transaction_always_counts():
    assert is_oscillation_transition("StopTransaction", StationStatus.CHARGING) is True


def test_r3_restart_after_stop_counts():
    # oscillate does Stop -> Start; the station sits in FINISHING when it restarts.
    assert is_oscillation_transition("StartTransaction", StationStatus.FINISHING) is True


def test_r3_first_start_of_a_session_does_not_count():
    # Normal fleet startup: Authorize (-> Preparing) then the session's first Start.
    assert is_oscillation_transition("StartTransaction", StationStatus.PREPARING) is False
    assert is_oscillation_transition("StartTransaction", StationStatus.AVAILABLE) is False


def test_r3_metervalues_is_not_a_transition():
    assert is_oscillation_transition("MeterValues", StationStatus.CHARGING) is False


# ------------------------------------------------------------ threat throttle
# An ongoing attack trips its rule on every frame. Without a cooldown the
# oscillate demo emits ~145 identical R3 events in 9 s, burying the dashboard
# timeline and the forensic log. One badge per station per rule per window.

def test_throttle_allows_first_threat():
    t = ThreatThrottle()
    assert t.should_emit("CP-03", "R3_OSCILLATION", now=100.0) is True


def test_throttle_suppresses_immediate_repeat():
    t = ThreatThrottle()
    t.should_emit("CP-03", "R3_OSCILLATION", now=100.0)
    assert t.should_emit("CP-03", "R3_OSCILLATION", now=100.5) is False


def test_throttle_allows_again_after_cooldown():
    t = ThreatThrottle()
    t.should_emit("CP-03", "R3_OSCILLATION", now=100.0)
    assert t.should_emit("CP-03", "R3_OSCILLATION", now=100.0 + t.COOLDOWN_SEC) is True


def test_throttle_is_per_station_and_per_rule():
    t = ThreatThrottle()
    t.should_emit("CP-03", "R2_PHYSICS", now=100.0)
    # a different station is throttled independently
    assert t.should_emit("CP-04", "R2_PHYSICS", now=100.1) is True
    # so is a different rule on the same station
    assert t.should_emit("CP-03", "R1_STATE_ORDER", now=100.1) is True


def test_a_blocked_card_retried_by_one_charger_is_reported_once_a_minute():
    t = ThreatThrottle()
    assert t.should_emit("CHN-02", "R8_TAG_CLONE", now=100.0) is True
    # the charger retries every 10 s; each retry is refused but not re-reported
    assert t.should_emit("CHN-02", "R8_TAG_CLONE", now=110.0) is False
    assert t.should_emit("CHN-02", "R8_TAG_CLONE", now=150.0) is False
    assert t.should_emit("CHN-02", "R8_TAG_CLONE", now=100.0 + t.BLOCK_COOLDOWN_SEC) is True
    # a forged meter on the same charger keeps the short cooldown
    t.should_emit("CHN-02", "R7_ELECTRICAL", now=100.0)
    assert t.should_emit("CHN-02", "R7_ELECTRICAL", now=100.0 + t.COOLDOWN_SEC) is True


# R3 is defined fleet-wide (CONTEXT.md §5.B), so it must collapse to ONE badge
# per window, not one per station — an 8-station oscillate otherwise emits 8.

def test_throttle_key_is_fleet_wide_for_r3():
    assert throttle_key("CP-03", "R3_OSCILLATION") == throttle_key("CP-04", "R3_OSCILLATION")


def test_throttle_key_is_per_station_for_station_scoped_rules():
    assert throttle_key("CP-03", "R2_PHYSICS") != throttle_key("CP-04", "R2_PHYSICS")


def test_fleet_wide_r3_emits_once_across_stations():
    t = ThreatThrottle()
    assert t.should_emit(throttle_key("CP-03", "R3_OSCILLATION"), "R3_OSCILLATION", now=100.0) is True
    # a second station tripping the same fleet-wide rule is suppressed
    assert t.should_emit(throttle_key("CP-04", "R3_OSCILLATION"), "R3_OSCILLATION", now=100.1) is False


# ---------------------------------------------------------- quarantine policy
# A forged-data rule severs the station (drop frame + ChangeAvailability + 4001).
# A fleet-wide or transient rule alerts only — quarantining on R3 would sever all
# 8 stations on an oscillate, and R4 is already rejected at the handshake.

def test_quarantine_on_forged_data_rules():
    assert should_quarantine("R2_PHYSICS") is True
    assert should_quarantine("R5_TXN_INTEGRITY") is True


def test_no_quarantine_on_fleetwide_or_transient_rules():
    assert should_quarantine("R3_OSCILLATION") is False
    assert should_quarantine("R1_STATE_ORDER") is False
    assert should_quarantine("R4_SESSION_UNIQUE") is False


# ------------------------------------------------------- R6 meter fraud
# energy_residual_kwh = reported register - integral(reported power dt).
# An honest session holds this on its N(0, 0.05) baseline (CONTEXT.md FIX-9).
# A station under-reporting power while its register accrues the truth makes it
# climb, which is the direct signature of billing fraud.
#
# The rule reads the *current* rate of climb, not the session mean. A mean over
# duration_sec is diluted by however long the station behaved honestly first:
# observed live, CP-03 charged cleanly for ~70 s and then spoofed for ~40 s,
# giving 1.60 kWh over 110.7 s = 0.0145 kWh/s -- under threshold, so the attack
# went undetected. The longer a station behaves before it turns, the more
# invisible the fraud becomes, which is backwards.
#
# Rate also separates the two under-reporting attacks, which an absolute level
# cannot. meter_spoof reports a flat 5 kW against a real ~120 kW, a constant
# 0.032 kWh/s. subtle_drift compounds -2%/tick, so its rate ramps and does not
# reach 0.025 kWh/s until ~69 s -- well after its ML score crosses 0.65 at
# ~40 s, which keeps the "ML only" demo beat intact.

def test_r6_clean_session_passes():
    assert check_meter_fraud(energy_residual_kwh=0.04, residual_rate_kwh_per_sec=0.0) is None


def test_r6_meter_spoof_trips():
    # 5 kW reported against a real 120 kW draw => 0.032 kWh/s.
    v = check_meter_fraud(energy_residual_kwh=0.319, residual_rate_kwh_per_sec=0.0319)
    assert v is not None and v.rule_id == "R6_METER_FRAUD"
    assert v.severity == "high"


def test_r6_spoof_starting_late_in_a_session_still_trips():
    # Regression: an honest prefix must not dilute detection. This station ran
    # clean for ~70 s, then spoofed; the session mean was only 0.0145 kWh/s but
    # the current rate is the full 0.032.
    v = check_meter_fraud(energy_residual_kwh=1.60, residual_rate_kwh_per_sec=0.0319)
    assert v is not None and v.rule_id == "R6_METER_FRAUD"


def test_r6_subtle_drift_does_not_trip_before_the_ml_badge():
    # The headline demo beat: at 40 s subtle_drift must still be ML-only.
    assert check_meter_fraud(energy_residual_kwh=0.428, residual_rate_kwh_per_sec=0.0185) is None


def test_r6_sustained_drift_eventually_trips():
    # By ~69 s the compounding under-report is no longer subtle.
    v = check_meter_fraud(energy_residual_kwh=1.6, residual_rate_kwh_per_sec=0.0252)
    assert v is not None and v.rule_id == "R6_METER_FRAUD"


def test_r6_small_residual_below_floor_passes():
    # A high rate over a tiny accumulated residual is startup jitter, not fraud.
    assert check_meter_fraud(energy_residual_kwh=0.2, residual_rate_kwh_per_sec=0.2) is None


def test_r6_first_sample_has_no_rate_and_passes():
    assert check_meter_fraud(energy_residual_kwh=0.0, residual_rate_kwh_per_sec=0.0) is None


def test_r6_negative_residual_passes():
    # Over-reporting energy is not the under-report fraud R6 detects.
    assert check_meter_fraud(energy_residual_kwh=-1.5, residual_rate_kwh_per_sec=-0.03) is None


def test_r6_quarantines():
    assert should_quarantine("R6_METER_FRAUD") is True


# ---------------------------------------------------------------- R7 electrical consistency
# A DC meter reports power, voltage and current from the same instant, so
# power must equal V x I. A charger that rewrites only the power figure fails.

def test_r7_honest_sample_passes():
    # 712.4 V x 168.4 A = 119.97 kW, reported 120.0 kW.
    assert check_electrical(power_kw=120.0, dc_voltage_v=712.4, dc_current_a=168.4) is None


def test_r7_low_power_taper_rounding_passes():
    # Wire values are rounded to 0.1; at 3 kW that is a few % but well under 2 kW.
    assert check_electrical(power_kw=3.4, dc_voltage_v=800.9, dc_current_a=4.2) is None


def test_r7_meter_spoof_trips():
    v = check_electrical(power_kw=5.0, dc_voltage_v=712.4, dc_current_a=168.4)
    assert v is not None and v.rule_id == "R7_ELECTRICAL" and v.severity == "high"
    assert "5.0 kW" in v.reason and "120.0 kW" in v.reason


def test_r7_consistent_under_report_is_left_to_ml():
    # A careful attacker scales current with power; V x I agrees, R7 stays quiet.
    assert check_electrical(power_kw=96.0, dc_voltage_v=712.4, dc_current_a=134.8) is None


def test_r7_without_voltage_or_current_cannot_judge():
    assert check_electrical(power_kw=5.0, dc_voltage_v=None, dc_current_a=168.4) is None
    assert check_electrical(power_kw=5.0, dc_voltage_v=712.4, dc_current_a=None) is None


def test_r7_quarantines():
    assert should_quarantine("R7_ELECTRICAL")


# ---------------------------------------------------------------- R8 / R9 id-tag checks
# One RFID card can only charge one car at a time. R8 catches a card already in
# a live session (here or at another site); R9 a card on the federation blocklist.

def test_r8_r9_unknown_tag_passes():
    assert check_id_tag("CP-01", local_owner=None, remote_owner=None, blocked_reason=None) is None


def test_r8_same_tag_live_on_another_local_station_trips():
    v = check_id_tag("CP-02", local_owner="CP-05", remote_owner=None, blocked_reason=None)
    assert v is not None and v.rule_id == "R8_TAG_CLONE"
    assert "CP-05" in v.reason


def test_r8_same_station_reauthorising_is_not_a_clone():
    assert check_id_tag("CP-02", local_owner="CP-02", remote_owner=None, blocked_reason=None) is None


def test_r8_tag_live_at_another_site_trips():
    v = check_id_tag("CHN-CP-02", local_owner=None, remote_owner=("VS-VELLORE", "CP-03"), blocked_reason=None)
    assert v is not None and v.rule_id == "R8_TAG_CLONE"
    assert "VS-VELLORE" in v.reason and "CP-03" in v.reason


def test_r9_blocklisted_tag_trips():
    v = check_id_tag("CP-01", local_owner=None, remote_owner=None, blocked_reason="cloned at VS-CHENNAI")
    assert v is not None and v.rule_id == "R9_THREAT_INTEL"


def test_r8_r9_do_not_quarantine_the_station():
    # The card is compromised, not the charger: refuse the card, keep the bay.
    assert not should_quarantine("R8_TAG_CLONE")
    assert not should_quarantine("R9_THREAT_INTEL")
