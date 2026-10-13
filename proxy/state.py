"""Per-charger, per-connector session state and cross-station registries — pure logic, no I/O.

This is the memory the rule engine reads. The relay (main.py) applies observed
frames to this state and asks rules.py to judge them.

A charge point (cpid) has one or more connectors (plugs), and each connector
runs its own charging session: its own transaction, card, meter register and
ML features. Connector 0 is OCPP's "the charger as a whole" (its main meter);
it never carries a transaction.

What lives here (BRIEF.md):
  * per-connector session — "is there a live transaction on this plug?" (R1)
  * transaction registry — transactionId -> (cpid, connector) (R5)
  * card registry — idTag -> (cpid, connector) with a live session (R8)
  * connection registry — which cpids have a live socket (R4)
  * quarantine set — cpids the proxy has severed; a quarantine takes the whole charger
  * pending-call map — uniqueId -> action, so a CALLRESULT can be correlated back
    to the CALL that produced it (a CALLRESULT does not carry the action name)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional

from shared.electrical import ac_input_kw
from shared.schemas import StationStatus
from proxy.ml_engine import SessionFeatures, compute_session_features

MAIN_METER = 0


def connector_label(cpid: str, connector_id: int) -> str:
    """How a plug is named in logs, threats and the federation: CP-01, CP-01/2."""
    return cpid if connector_id == 1 else f"{cpid}/{connector_id}"


@dataclass
class ConnectorState:
    """One plug of a charger and the session running on it."""

    cpid: str
    connector_id: int
    status: StationStatus = StationStatus.AVAILABLE
    transaction_id: Optional[int] = None
    id_tag: Optional[str] = None           # card of the live session
    stopped_at: Optional[float] = None     # when this plug's last transaction stopped (R3)

    # Session accumulators for the ML features and R6.
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

    # Last TelemetryEvent broadcast for this plug, so a quarantine can re-publish
    # every plug of the charger as Quarantined.
    last_telemetry: Any = None

    @property
    def label(self) -> str:
        return connector_label(self.cpid, self.connector_id)

    @property
    def session_live(self) -> bool:
        """True from the CSMS-confirmed StartTransaction until StopTransaction."""
        return self.transaction_id is not None

    def begin_session(self, ts: float) -> None:
        """A new transaction on this plug: the meter story starts over."""
        self.session_start_ts = ts
        self.last_ts = None
        self.last_power_kw = None
        self.energy_integral_kwh = 0.0
        self.sample_count = 0
        self.energy_register_start_kwh = None
        self.last_energy_residual_kwh = None
        self.last_power_reported_kw = 0.0
        # The supply-side reading belongs to the session that just ended. Kept,
        # it made a plug that had not reported yet look like it drew full power
        # (0 kW to the car, 127 kVA from the transformer).
        self.last_ac_power_kw = None

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

    def record_meter_sample(
        self, power_kw: float, soc: float, energy_register_kwh: float, ts: float
    ) -> SessionFeatures:
        """Fold one MeterValues into this plug's accumulators; return features."""
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
class StationState:
    """Everything the proxy knows about one charge point."""

    cpid: str
    # Charger-wide: AVAILABLE, or QUARANTINED once the proxy severs it.
    status: StationStatus = StationStatus.AVAILABLE
    # An Authorize was accepted. Informational only: Authorize names no
    # connector, and OCPP lets chargers authorise from a local list without it.
    authorized: bool = False

    # uniqueId -> action, for correlating CALLRESULT back to its CALL.
    pending: dict[str, str] = field(default_factory=dict)
    # uniqueId of an in-flight StartTransaction -> (connectorId, idTag) it carried.
    pending_start: dict[str, tuple[int, Optional[str]]] = field(default_factory=dict)
    connectors: dict[int, ConnectorState] = field(default_factory=dict)

    def connector(self, connector_id: int) -> ConnectorState:
        c = self.connectors.get(connector_id)
        if c is None:
            c = ConnectorState(cpid=self.cpid, connector_id=connector_id)
            self.connectors[connector_id] = c
        return c

    def plugs(self) -> Iterator[ConnectorState]:
        """Every real connector (1..n), not the main meter."""
        return (c for cid, c in sorted(self.connectors.items()) if cid != MAIN_METER)


@dataclass
class GridSnapshot:
    """Aggregate of the charging fleet, in the site's real units."""

    output_kw: float                 # DC delivered to vehicles
    input_kw: float                  # AC drawn from the transformer
    active_stations: int             # connectors charging a vehicle
    lt_voltage_v: Optional[float]    # mean measured inlet voltage, line-to-line
    frequency_hz: Optional[float]    # mean measured supply frequency


# A voltage / frequency report counts toward the site figure only while it is
# this fresh; a quarantined or idle plug stops reporting.
ELECTRICAL_FRESH_SEC = 5.0


class ProxyState:
    """All station state plus the cross-station registries the rules need."""

    def __init__(self) -> None:
        self.stations: dict[str, StationState] = {}
        self.txn_owner: dict[int, tuple[str, int]] = {}  # transactionId -> (cpid, connector)  (R5)
        self.live_tags: dict[str, tuple[str, int]] = {}  # idTag -> (cpid, connector) live (R8)
        self.live_connections: set[str] = set()          # cpids with an open socket (R4)
        self.quarantined: set[str] = set()
        # Called as (id_tag, plug label, live) when a card's session starts or
        # ends; main.py points it at the federation link.
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
        """Clear a charger's sessions. A new socket is always a new session.

        Without this the proxy keeps FSM state across a reconnect, so a station
        that reconnects mid-transaction (after an attack, or a dropped socket)
        would trip R1 on every frame until its next StartTransaction.
        """
        st = self.station(cpid)
        for c in list(st.connectors.values()):
            if c.transaction_id is not None:
                self.txn_owner.pop(c.transaction_id, None)
            self._release_tag(c)
        st.connectors.clear()
        st.authorized = False
        st.status = StationStatus.AVAILABLE
        st.pending.clear()
        st.pending_start.clear()

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
        self.station(cpid).authorized = True

    def apply_start_transaction(
        self,
        cpid: str,
        transaction_id: int,
        connector_id: int = 1,
        id_tag: Optional[str] = None,
        now: Optional[float] = None,
    ) -> ConnectorState:
        """Link a CSMS-issued transactionId to its plug (from the CALLRESULT)."""
        now = time.time() if now is None else now
        c = self.station(cpid).connector(connector_id)
        if c.transaction_id is not None:
            self.txn_owner.pop(c.transaction_id, None)
        self._release_tag(c)
        c.transaction_id = transaction_id
        c.status = StationStatus.CHARGING
        c.begin_session(now)
        self.txn_owner[transaction_id] = (cpid, connector_id)
        if id_tag:
            c.id_tag = id_tag
            self.live_tags[id_tag] = (cpid, connector_id)
            if self.tag_listener is not None:
                self.tag_listener(id_tag, c.label, True)
        return c

    def connector_for_txn(self, cpid: str, transaction_id: Optional[int]) -> Optional[ConnectorState]:
        """The plug of *this* charger running that transaction, if any."""
        owner = self.txn_owner.get(transaction_id) if transaction_id is not None else None
        if owner is None or owner[0] != cpid:
            return None
        return self.station(cpid).connector(owner[1])

    def apply_stop_transaction(
        self, cpid: str, transaction_id: Optional[int], now: Optional[float] = None
    ) -> Optional[ConnectorState]:
        """End the session the transaction belongs to; other plugs keep charging."""
        c = self.connector_for_txn(cpid, transaction_id)
        if c is None:
            return None
        self.txn_owner.pop(c.transaction_id, None)
        self._release_tag(c)
        c.transaction_id = None
        c.status = StationStatus.FINISHING
        c.stopped_at = time.time() if now is None else now
        return c

    # ---- RFID card registry (R8) ----------------------------------------
    def _release_tag(self, c: ConnectorState) -> None:
        if c.id_tag is not None and self.live_tags.get(c.id_tag) == (c.cpid, c.connector_id):
            self.live_tags.pop(c.id_tag, None)
            if self.tag_listener is not None:
                self.tag_listener(c.id_tag, c.label, False)
        c.id_tag = None

    def live_sessions(self) -> list[tuple[str, str]]:
        """(id_tag, plug label) for every card with a live session at this site."""
        return [(tag, connector_label(cpid, cid)) for tag, (cpid, cid) in self.live_tags.items()]

    def live_cards(self) -> list[tuple[str, str, int]]:
        """(id_tag, cpid, connector) for every card with a live session at this site."""
        return [(tag, cpid, cid) for tag, (cpid, cid) in self.live_tags.items()]

    def tag_owner(self, id_tag: str) -> Optional[str]:
        """Charger currently charging on this card, if any."""
        owner = self.live_tags.get(id_tag)
        return owner[0] if owner else None

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
        """Fleet aggregate across every charging plug.

        A quarantined charger draws nothing, so all its plugs drop out.
        Supply-side power is the plug's own Inlet reading when it reports one;
        otherwise it is derived from the DC output and the charger efficiency.
        Bus voltage and frequency also come from main-meter (connector 0) reports.
        """
        now = time.time() if now is None else now
        output = 0.0
        supply = 0.0
        active = 0
        volts: list[float] = []
        hertz: list[float] = []
        for st in self.stations.values():
            if st.cpid in self.quarantined:
                continue
            for c in st.connectors.values():
                if c.connector_id != MAIN_METER and c.status == StationStatus.CHARGING:
                    out_kw = max(0.0, c.last_power_reported_kw)
                    output += out_kw
                    supply += c.last_ac_power_kw if c.last_ac_power_kw is not None else ac_input_kw(out_kw)
                    active += 1
                fresh = c.last_electrical_ts is not None and now - c.last_electrical_ts <= ELECTRICAL_FRESH_SEC
                if fresh:
                    if c.last_ac_voltage_v is not None:
                        volts.append(c.last_ac_voltage_v)
                    if c.last_frequency_hz is not None:
                        hertz.append(c.last_frequency_hz)
        return GridSnapshot(
            output_kw=output,
            input_kw=supply,
            active_stations=active,
            lt_voltage_v=sum(volts) / len(volts) if volts else None,
            frequency_hz=sum(hertz) / len(hertz) if hertz else None,
        )
