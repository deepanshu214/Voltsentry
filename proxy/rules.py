"""Tier-1 deterministic detection rules (CONTEXT.md §5.B) — pure functions.

Each rule reads state + a frame fact and returns a RuleViolation or None. The
relay (main.py) turns a RuleViolation into a schema ThreatEvent; keeping the
rules free of I/O and of the wire schema is what makes them unit-testable.

Trip table (CONTEXT.md §5.B):
  R1_STATE_ORDER   MeterValues/StopTransaction with no live Authorize->StartTransaction
  R2_PHYSICS       power_kw > 150.0, or power_kw > 60.0 while soc > 80.0, or power_kw < 0
  R3_OSCILLATION   >= 6 Start/Stop transitions fleet-wide in a 10 s window
  R4_SESSION_UNIQUE second handshake for a cpid with a live session
  R5_TXN_INTEGRITY  MeterValues.transactionId unknown or owned by another cpid
  R6_METER_FRAUD   energy residual climbing faster than an honest session ever does
  R7_ELECTRICAL    reported power disagrees with reported DC voltage x current
  R8_TAG_CLONE     an RFID card already in a live session here or at another site
  R9_THREAT_INTEL  an RFID card / charge point on the federation blocklist
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional

from shared.schemas import StationStatus
from proxy.state import ProxyState, StationState

# R2 physics envelope constants (CONTEXT.md §5.B).
MAX_POWER_KW = 150.0
CV_TAPER_POWER_KW = 60.0
CV_TAPER_SOC = 80.0

# R6 meter-fraud envelope. energy_residual_kwh is the reported register minus
# the integral of reported power (CONTEXT.md FIX-9); an honest session holds it
# on its N(0, 0.05) baseline, so anything climbing steadily is a station whose
# meter disagrees with its own power reports.
#
# The rule is a *rate*, not an absolute level, because absolute level cannot
# separate the two under-reporting attacks. meter_spoof reports a flat 5 kW
# against a real ~120 kW, a constant 0.032 kWh/s. subtle_drift compounds
# -2%/tick, so any absolute threshold low enough to catch a spoof quickly also
# trips drift before its ML score crosses 0.65, and Tier-2 stops being
# demonstrable.
#
# It is the *current* rate, not the session mean. A mean over duration_sec is
# diluted by however long the station behaved honestly beforehand: live, CP-03
# ran clean for ~70 s then spoofed for ~40 s, giving 1.60 kWh over 110.7 s =
# 0.0145 kWh/s, under threshold, and the attack went undetected. Under a mean,
# the longer a station behaves before it turns, the better it hides.
#
# At 0.025 kWh/s a spoof (0.032) trips on its first samples, while drift's
# ramping rate does not reach the line until ~69 s -- comfortably after its ML
# badge at ~40 s.
#
# The floor keeps a single jittery frame early in a session, before any
# meaningful residual has accumulated, from looking like fraud.
METER_FRAUD_MIN_RESIDUAL_KWH = 0.25
METER_FRAUD_RATE_KWH_PER_SEC = 0.025

# R7 tolerance. Wire values carry one decimal, so at low taper power rounding
# alone is a few percent; 2 kW absolute covers that, 10 % covers sampling skew
# at high power. A meter_spoof (5 kW reported vs ~120 kW from V x I) is off by 96 %.
ELECTRICAL_ABS_TOL_KW = 2.0
ELECTRICAL_REL_TOL = 0.10

# Actions that are only legal inside a live session (R1).
_SESSION_ONLY_ACTIONS = {"MeterValues", "StopTransaction"}


@dataclass
class RuleViolation:
    """A tripped Tier-1 rule. The relay converts this into a ThreatEvent."""

    rule_id: str
    severity: str  # "low" | "medium" | "high"
    reason: str


def check_physics(power_kw: float, soc: float) -> Optional[RuleViolation]:
    """R2 — CC-CV physics envelope."""
    if power_kw < 0:
        return RuleViolation(
            "R2_PHYSICS", "high", f"negative power draw {power_kw:.1f} kW"
        )
    if power_kw > MAX_POWER_KW:
        return RuleViolation(
            "R2_PHYSICS", "high", f"power {power_kw:.1f} kW exceeds {MAX_POWER_KW:.0f} kW"
        )
    if power_kw > CV_TAPER_POWER_KW and soc > CV_TAPER_SOC:
        return RuleViolation(
            "R2_PHYSICS",
            "high",
            f"power {power_kw:.1f} kW above CV taper at SoC {soc:.0f}%",
        )
    return None


def check_meter_fraud(
    energy_residual_kwh: float, residual_rate_kwh_per_sec: float
) -> Optional[RuleViolation]:
    """R6 — the energy register is pulling away from the reported power *now*.

    Only a positive residual climbing counts: that is the station claiming less
    power than its own meter accrued, the direction that under-bills. A negative
    or flat residual is not this fraud.
    """
    if energy_residual_kwh < METER_FRAUD_MIN_RESIDUAL_KWH:
        return None
    if residual_rate_kwh_per_sec < METER_FRAUD_RATE_KWH_PER_SEC:
        return None
    return RuleViolation(
        "R6_METER_FRAUD",
        "high",
        f"meter under-reporting: {energy_residual_kwh:.2f} kWh unaccounted, "
        f"hiding {residual_rate_kwh_per_sec * 3600:.0f} kW",
    )


def check_electrical(
    power_kw: float, dc_voltage_v: Optional[float], dc_current_a: Optional[float]
) -> Optional[RuleViolation]:
    """R7 — reported power must equal the reported voltage x current.

    The three readings come from the same meter at the same instant, so a
    station that rewrites only the power figure contradicts itself. Silent when
    the charger does not report V and I: there is nothing to cross-check.
    """
    if dc_voltage_v is None or dc_current_a is None:
        return None
    implied_kw = dc_voltage_v * dc_current_a / 1000.0
    tolerance = max(ELECTRICAL_ABS_TOL_KW, ELECTRICAL_REL_TOL * max(abs(implied_kw), abs(power_kw)))
    if abs(implied_kw - power_kw) <= tolerance:
        return None
    return RuleViolation(
        "R7_ELECTRICAL",
        "high",
        f"reported {power_kw:.1f} kW but {dc_voltage_v:.0f} V x {dc_current_a:.0f} A "
        f"= {implied_kw:.1f} kW",
    )


def check_id_tag(
    cpid: str,
    local_owner: Optional[str],
    remote_owner: Optional[tuple[str, str]],
    blocked_reason: Optional[str],
) -> Optional[RuleViolation]:
    """R8 / R9 — an RFID card can only be in one live session at a time.

    local_owner:   station at this site already charging on the same card
    remote_owner:  (node_id, station) at another site, from the federation
    blocked_reason: why the card is on the federation blocklist, if it is
    """
    if blocked_reason:
        return RuleViolation("R9_THREAT_INTEL", "high", f"card on federation blocklist: {blocked_reason}")
    if remote_owner is not None:
        node, station = remote_owner
        return RuleViolation(
            "R8_TAG_CLONE", "high", f"card already charging at {node}/{station}; cloned RFID on {cpid}"
        )
    if local_owner is not None and local_owner != cpid:
        return RuleViolation(
            "R8_TAG_CLONE", "high", f"card already charging on {local_owner}; cloned RFID on {cpid}"
        )
    return None


def check_state_order(station: StationState, action: str) -> Optional[RuleViolation]:
    """R1 — session-only actions require a live Authorize->StartTransaction."""
    if action in _SESSION_ONLY_ACTIONS and not station.session_live:
        return RuleViolation(
            "R1_STATE_ORDER",
            "high",
            f"{action} on {station.cpid} with no live transaction",
        )
    return None


def check_session_unique(state: ProxyState, cpid: str) -> Optional[RuleViolation]:
    """R4 — a second handshake while a session is already live for this cpid."""
    if cpid in state.live_connections:
        return RuleViolation(
            "R4_SESSION_UNIQUE",
            "high",
            f"duplicate handshake for live station {cpid}",
        )
    return None


def check_txn_integrity(
    state: ProxyState, cpid: str, transaction_id: Optional[int]
) -> Optional[RuleViolation]:
    """R5 — MeterValues.transactionId must be known and owned by this cpid."""
    if transaction_id is None:
        return None
    owner = state.txn_owner.get(transaction_id)
    if owner is None:
        return RuleViolation(
            "R5_TXN_INTEGRITY", "high", f"unknown transactionId {transaction_id}"
        )
    if owner != cpid:
        return RuleViolation(
            "R5_TXN_INTEGRITY",
            "high",
            f"transactionId {transaction_id} belongs to {owner}, not {cpid}",
        )
    return None


def is_oscillation_transition(action: str, station_status: StationStatus) -> bool:
    """R3 — does this frame count as a Start/Stop transition?

    A StopTransaction always does. A StartTransaction only counts when it is a
    *re-start* (the station just stopped, so it sits in FINISHING) — the second
    half of an oscillation cycle. The first StartTransaction of a session is not
    a transition out of charging: counting it would trip R3 on normal fleet
    startup, where 8 staggered stations start well inside the 10 s window.
    """
    if action == "StopTransaction":
        return True
    if action == "StartTransaction":
        return station_status == StationStatus.FINISHING
    return False


# Rules judged across the whole fleet rather than per station (CONTEXT.md §5.B).
FLEET_SCOPED_RULES = {"R3_OSCILLATION"}
_FLEET_KEY = "*fleet*"

# Rules that actively sever the station (CONTEXT.md §5.A [FIX-6]): a station
# reporting forged data is compromised, so it is quarantined. Fleet-wide (R3)
# and handshake-time (R4) rules alert only — quarantining on R3 would sever all
# eight stations on an oscillate, and R4 is already rejected at the handshake.
QUARANTINE_RULES = {"R2_PHYSICS", "R5_TXN_INTEGRITY", "R6_METER_FRAUD", "R7_ELECTRICAL"}


def should_quarantine(rule_id: str) -> bool:
    return rule_id in QUARANTINE_RULES


def throttle_key(cpid: str, rule_id: str) -> str:
    """Scope key for throttling: fleet-wide rules collapse to a single key.

    R3 counts transitions fleet-wide, so an 8-station oscillate would otherwise
    emit 8 identical badges per window instead of the one real finding.
    """
    return _FLEET_KEY if rule_id in FLEET_SCOPED_RULES else cpid


class ThreatThrottle:
    """Collapse a sustained attack into one threat per station per rule.

    A live attack trips its rule on every frame, so an unthrottled oscillate
    demo emits ~145 identical R3 events in 9 s — it buries the dashboard
    timeline and the forensic log (AGENTS.md: readable, not a debug firehose).
    The clock is injected so this is unit-testable.
    """

    COOLDOWN_SEC = 10.0
    # A blocked card or charge point is retried every few seconds for as long
    # as the attack lasts. Each retry is refused, but it is one incident, so it
    # is reported once a minute rather than every ten seconds.
    BLOCK_RULES = frozenset({"R8_TAG_CLONE", "R9_THREAT_INTEL"})
    BLOCK_COOLDOWN_SEC = 60.0

    def __init__(self) -> None:
        self._last: dict[tuple[str, str], float] = {}

    def cooldown(self, rule_id: str) -> float:
        return self.BLOCK_COOLDOWN_SEC if rule_id in self.BLOCK_RULES else self.COOLDOWN_SEC

    def should_emit(self, cpid: str, rule_id: str, now: float) -> bool:
        key = (cpid, rule_id)
        last = self._last.get(key)
        if last is not None and now - last < self.cooldown(rule_id):
            return False
        self._last[key] = now
        return True

    def clear(self) -> None:
        self._last.clear()


class OscillationDetector:
    """R3 — countable predicate over a sliding window with an injectable clock.

    A "transition" is one StartTransaction or StopTransaction fleet-wide. The
    relay calls record_transition() on each, then check(now). Restated from
    v1's untestable ">0.5 Hz" per CONTEXT.md [FIX-8].
    """

    WINDOW_SEC = 10.0
    THRESHOLD = 6

    def __init__(self) -> None:
        self._events: deque[float] = deque()

    def record_transition(self, ts: float) -> None:
        self._events.append(ts)

    def _prune(self, now: float) -> None:
        while self._events and now - self._events[0] > self.WINDOW_SEC:
            self._events.popleft()

    def check(self, now: float) -> Optional[RuleViolation]:
        self._prune(now)
        if len(self._events) >= self.THRESHOLD:
            return RuleViolation(
                "R3_OSCILLATION",
                "high",
                f"{len(self._events)} start/stop transitions in {self.WINDOW_SEC:.0f}s",
            )
        return None
