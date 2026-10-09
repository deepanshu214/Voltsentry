"""Per-station session state and cross-station registries — pure logic, no I/O.

This is the memory the rule engine reads. The relay (main.py) applies observed
frames to this state and asks rules.py to judge them.

What lives here (BRIEF.md):
  * per-station FSM  — enough to answer "is there a live Authorize->StartTransaction?"
  * transaction registry — transactionId -> owning cpid (R5)
  * connection registry — which cpids have a live socket (R4)
  * quarantine set — cpids the proxy has severed
  * pending-call map — uniqueId -> action, so a CALLRESULT can be correlated back
    to the CALL that produced it (a CALLRESULT does not carry the action name)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from shared.electrical import ac_input_kw
from shared.schemas import StationStatus
from proxy.ml_engine import SessionFeatures, compute_session_features


@dataclass
class StationState:
    """Everything the proxy knows about one charge point."""

    cpid: str
    status: StationStatus = StationStatus.AVAILABLE
    authorized: bool = False
    transaction_id: Optional[int] = None

    # uniqueId -> action, for correlating CALLRESULT back to its CALL.
    pending: dict[str, str] = field(default_factory=dict)

    # Telemetry / ML accumulators (populated later, kept here so one object
    # carries a station's whole story).
    session_start_ts: Optional[float] = None
    last_ts: Optional[float] = None
    last_power_kw: Optional[float] = None
    energy_integral_kwh: float = 0.0
    sample_count: int = 0
    # Register reading when this session was first observed; the residual is
    # measured from it, so attaching mid-session does not read as fraud.
    energy_register_start_kwh: Optional[float] = None
    # previous sample's residual, so R6 can read its current climb rate
    last_energy_residual_kwh: Optional[float] = None

    # kW of the last MeterValues, kept for the fleet grid aggregate.
    last_power_reported_kw: float = 0.0

    # Electrical context of the last MeterValues (None = not reported).
    last_ac_power_kw: Optional[float] = None
    last_ac_voltage_v: Optional[float] = None
    last_frequency_hz: Optional[float] = None
    last_electrical_ts: Optional[float] = None

    # RFID card of the current session (from StartTransaction / Authorize).
    id_tag: Optional[str] = None
    # uniqueId of an in-flight StartTransaction -> idTag it carried.
    pending_start_tag: dict[str, str] = field(default_factory=dict)

    def record_electrical(
        self,
        ac_power_kw: Optional[float],
        ac_voltage_v: Optional[float],
        frequency_hz: Optional[float],
        ts: float,
    ) -> None:
        self.last_ac_power_kw = ac_power_kw
        self.last_ac_voltage_v = ac_voltage_v
        self.last_frequency_hz = frequency_hz
        self.last_electrical_ts = ts

    @property
    def session_live(self) -> bool:
        """True once Authorize -> StartTransaction has completed and not stopped."""
        return self.authorized and self.transaction_id is not None

    def record_meter_sample(
        self, power_kw: float, soc: float, energy_register_kwh: float, ts: float
    ) -> SessionFeatures:
        """Fold one MeterValues into this station's accumulators; return features."""
        f = compute_session_features(
            power_kw=power_kw,
            soc=soc,
            energy_register_kwh=energy_register_kwh,
            ts=ts,
            prev_ts=self.last_ts,
            prev_power_kw=self.last_power_kw,
            energy_integral_kwh=self.energy_integral_kwh,
            session_start_ts=self.session_start_ts,
            sample_count=self.sample_count,
            energy_register_start_kwh=self.energy_register_start_kwh,
            prev_energy_residual_kwh=self.last_energy_residual_kwh,
        )
        self.last_ts = ts
        self.last_power_kw = power_kw
        self.last_power_reported_kw = power_kw
        self.energy_integral_kwh = f.energy_integral_kwh
        self.sample_count = f.sample_count
        self.energy_register_start_kwh = f.energy_register_start_kwh
        self.last_energy_residual_kwh = f.energy_residual_kwh
        return f


@dataclass
class GridSnapshot:
    """Aggregate of the charging fleet, in the site's real units."""

    output_kw: float                 # DC delivered to vehicles
    input_kw: float                  # AC drawn from the transformer
    active_stations: int
    lt_voltage_v: Optional[float]    # mean measured inlet voltage, line-to-line
    frequency_hz: Optional[float]    # mean measured supply frequency


# A station's voltage / frequency report counts toward the site figure only
# while it is this fresh; a quarantined or idle bay stops reporting.
ELECTRICAL_FRESH_SEC = 5.0


class ProxyState:
    """All station state plus the cross-station registries the rules need."""

    def __init__(self) -> None:
        self.stations: dict[str, StationState] = {}
        self.txn_owner: dict[int, str] = {}       # transactionId -> cpid  (R5)
        self.live_tags: dict[str, str] = {}       # idTag -> cpid with a live session (R8)
        self.live_connections: set[str] = set()   # cpids with an open socket (R4)
        self.quarantined: set[str] = set()
        # Called as (id_tag, cpid, live) when a card's session starts or ends;
        # main.py points it at the federation link.
        self.tag_listener: Optional[Callable[[str, str, bool], None]] = None
        # cpid -> uniqueId of the ChangeAvailability we sent, awaiting its ack
        self.quarantine_ack_pending: dict[str, str] = {}

    # ---- station lookup -------------------------------------------------
    def station(self, cpid: str) -> StationState:
        st = self.stations.get(cpid)
        if st is None:
            st = StationState(cpid=cpid)
            self.stations[cpid] = st
        return st

    def reset_session(self, cpid: str) -> None:
        """Clear a station's session FSM. A new socket is always a new session.

        Without this the proxy keeps FSM state across a reconnect, so a station
        that reconnects mid-transaction (after an attack, or a dropped socket)
        would trip R1 on every frame until it happened to re-authorise.
        """
        st = self.station(cpid)
        if st.transaction_id is not None:
            self.txn_owner.pop(st.transaction_id, None)
        self._release_tag(st)
        st.authorized = False
        st.transaction_id = None
        st.status = StationStatus.AVAILABLE
        st.pending.clear()
        st.pending_start_tag.clear()
        st.last_ac_power_kw = None
        st.last_ac_voltage_v = None
        st.last_frequency_hz = None
        st.last_electrical_ts = None
        st.session_start_ts = None
        st.last_ts = None
        st.last_power_kw = None
        st.last_power_reported_kw = 0.0
        st.energy_integral_kwh = 0.0
        st.sample_count = 0
        st.energy_register_start_kwh = None
        st.last_energy_residual_kwh = None

    # ---- connection registry (R4) --------------------------------------
    def register_connection(self, cpid: str) -> bool:
        """Register a new socket for cpid. Returns False if one is already live."""
        if cpid in self.live_connections:
            return False
        self.live_connections.add(cpid)
        return True

    def unregister_connection(self, cpid: str) -> None:
        self.live_connections.discard(cpid)

    # ---- pending-call map ----------------------------------------------
    def note_pending(self, cpid: str, unique_id: str, action: str) -> None:
        self.station(cpid).pending[unique_id] = action

    def resolve_pending(self, cpid: str, unique_id: str) -> Optional[str]:
        """Pop and return the action a CALLRESULT's uniqueId corresponds to."""
        return self.station(cpid).pending.pop(unique_id, None)

    # ---- FSM transitions ------------------------------------------------
    def apply_authorize(self, cpid: str) -> None:
        st = self.station(cpid)
        st.authorized = True
        st.status = StationStatus.PREPARING

    def apply_start_transaction(
        self, cpid: str, transaction_id: int, id_tag: Optional[str] = None
    ) -> None:
        """Link a returned transactionId to its station (from the CALLRESULT)."""
        st = self.station(cpid)
        st.transaction_id = transaction_id
        st.status = StationStatus.CHARGING
        if st.session_start_ts is None:
            st.session_start_ts = time.time()
        self.txn_owner[transaction_id] = cpid
        if id_tag:
            self._release_tag(st)
            st.id_tag = id_tag
            self.live_tags[id_tag] = cpid
            if self.tag_listener is not None:
                self.tag_listener(id_tag, cpid, True)

    def apply_stop_transaction(self, cpid: str) -> None:
        st = self.station(cpid)
        if st.transaction_id is not None:
            self.txn_owner.pop(st.transaction_id, None)
        self._release_tag(st)
        st.transaction_id = None
        st.authorized = False
        st.status = StationStatus.FINISHING

    # ---- RFID card registry (R8) ----------------------------------------
    def _release_tag(self, st: StationState) -> None:
        if st.id_tag is not None and self.live_tags.get(st.id_tag) == st.cpid:
            self.live_tags.pop(st.id_tag, None)
            if self.tag_listener is not None:
                self.tag_listener(st.id_tag, st.cpid, False)
        st.id_tag = None

    def live_sessions(self) -> list[tuple[str, str]]:
        """(id_tag, cpid) for every card with a live session at this site."""
        return list(self.live_tags.items())

    def tag_owner(self, id_tag: str) -> Optional[str]:
        return self.live_tags.get(id_tag)

    # ---- quarantine -----------------------------------------------------
    def quarantine(self, cpid: str) -> None:
        self.quarantined.add(cpid)
        self.station(cpid).status = StationStatus.QUARANTINED

    def is_quarantined(self, cpid: str) -> bool:
        return cpid in self.quarantined

    def clear_quarantine(self, cpid: Optional[str] = None) -> None:
        """Clear one station, or all when cpid is None (attack.py reset)."""
        if cpid is None:
            self.quarantined.clear()
            self.quarantine_ack_pending.clear()
        else:
            self.quarantined.discard(cpid)
            self.quarantine_ack_pending.pop(cpid, None)

    # ---- transformer / grid aggregate (CONTEXT.md §5.C, [FIX-12]) --------
    def grid(self, now: Optional[float] = None) -> GridSnapshot:
        """Fleet aggregate across the live, charging stations.

        A quarantined station draws nothing, so it drops out. Supply-side power
        is the charger's own Inlet reading when it reports one; otherwise it is
        derived from the DC output and the charger efficiency.
        """
        now = time.time() if now is None else now
        output = 0.0
        supply = 0.0
        active = 0
        volts: list[float] = []
        hertz: list[float] = []
        for st in self.stations.values():
            if st.status == StationStatus.CHARGING and st.cpid not in self.quarantined:
                out_kw = max(0.0, st.last_power_reported_kw)
                output += out_kw
                supply += st.last_ac_power_kw if st.last_ac_power_kw is not None else ac_input_kw(out_kw)
                active += 1
            fresh = st.last_electrical_ts is not None and now - st.last_electrical_ts <= ELECTRICAL_FRESH_SEC
            if fresh and st.cpid not in self.quarantined:
                if st.last_ac_voltage_v is not None:
                    volts.append(st.last_ac_voltage_v)
                if st.last_frequency_hz is not None:
                    hertz.append(st.last_frequency_hz)
        return GridSnapshot(
            output_kw=output,
            input_kw=supply,
            active_stations=active,
            lt_voltage_v=sum(volts) / len(volts) if volts else None,
            frequency_hz=sum(hertz) / len(hertz) if hertz else None,
        )
