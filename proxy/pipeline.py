"""Per-frame detection pipeline: parse -> update state -> score -> emit + verdict.

Sits between raw transport (main.py) and the pure logic (ocpp/state/rules/
ml_engine). It holds no sockets: main.py passes in the shared ProxyState, the
OscillationDetector and an async `broadcast` callback, and acts on the returned
Verdict. That keeps this glue unit-testable and main.py transport-only (BRIEF).

H6/H7 behaviour:
  * emit a real TelemetryEvent per MeterValues (feature vector from ml_engine)
  * on a forged-data rule, arm active quarantine: the offending frame is dropped
    and main.py sends ChangeAvailability downstream, then closes 4001 on the ack
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from shared.schemas import StationStatus, TelemetryEvent, ThreatEvent
from proxy import ml_engine, ocpp, rules
from proxy.state import MAIN_METER, ConnectorState, ProxyState, connector_label

# Tier-2 behavioural model, loaded once at import from the trained artifact
# (fit on a real dataset by proxy/train_tier2.py). If the pickle is absent the
# proxy still runs — ml_score just stays 0.0. Cold start: the first two samples
# of a session are not scored, so a session start never false-positives on
# camera (FIX-10 / BRIEF).
MODEL_PATH = Path(__file__).resolve().parent / "models" / "tier2.pkl"
ML_COLD_START_SAMPLES = 2
ML_RULE_ID = "ML_ANOMALY"

try:
    ML_MODEL = ml_engine.Tier2Model.load(MODEL_PATH)
except Exception:
    ML_MODEL = None
    print("[proxy] Tier-2 model not found, ml_score stays 0.0", flush=True)

# broadcast takes any feed schema model (Telemetry / Threat / Grid).
Broadcast = Callable[[Any], Awaitable[None]]

INCIDENTS_LOG = Path(__file__).resolve().parent.parent / "logs" / "incidents.jsonl"

# One threat per station per rule per cooldown window, so a sustained attack
# does not bury the dashboard timeline or the forensic log.
THROTTLE = rules.ThreatThrottle()


@dataclass
class Verdict:
    """What transport should do with the frame just inspected."""

    forward: bool = True                    # relay it upstream to the CSMS?
    start_quarantine: bool = False          # send ChangeAvailability downstream
    change_availability_uid: Optional[str] = None
    close: bool = False                     # close the charger socket with 4001
    reply: Optional[str] = None             # frame the proxy answers the charger with itself
    quarantine_reason: Optional[str] = None
    card_blocked: bool = False              # an Authorize answered Blocked by the proxy (R8/R9)


# Federation link (federation.client.FederationClient), set by main.py when this
# node is part of a network. None means a standalone site: card checks fall
# back to this site's own sessions.
FEDERATION: Any = None
# This node's id, stamped on every ThreatEvent (and so on the forensic log).
NODE_ID: Optional[str] = None
# Enforcement mode, set by main.py (--mode). False is monitor-only: every rule
# and the ML model still run and every threat is reported, but no frame is
# dropped or answered, no charger is quarantined, and no indicator is shared.
# A new site runs this way until its rules are proven on its own meters.
ENFORCE: bool = True


def _card_violation(state: ProxyState, cpid: str, id_tag: Optional[str]) -> Optional[rules.RuleViolation]:
    """R8 / R9 for the card presented in an Authorize."""
    if not id_tag:
        return None
    remote_owner, blocked = (None, None)
    if FEDERATION is not None:
        remote_owner, blocked = FEDERATION.check_card(id_tag)
    return rules.check_id_tag(cpid, state.tag_owner(id_tag), remote_owner, blocked)


def build_threat(
    cpid: str,
    violation: rules.RuleViolation,
    raw_frame: Optional[str],
    action_taken: str,
    recommended_action: Optional[str] = None,
    connector_id: Optional[int] = None,
) -> ThreatEvent:
    """Turn a RuleViolation into a Tier-1 ThreatEvent (never a hand-built dict)."""
    return ThreatEvent(
        station_id=cpid,
        connector_id=connector_id,
        ts=time.time(),
        tier=1,
        rule_id=violation.rule_id,
        severity=violation.severity,
        reason=violation.reason,
        action_taken=action_taken,
        raw_frame=raw_frame,
        node_id=NODE_ID,
        recommended_action=recommended_action,
    )


def _log_incident(event: ThreatEvent) -> None:
    INCIDENTS_LOG.parent.mkdir(exist_ok=True)
    with INCIDENTS_LOG.open("a", encoding="utf-8") as fh:
        fh.write(event.model_dump_json() + "\n")


async def raise_threat(
    cpid: str,
    violation: rules.RuleViolation,
    raw_frame: Optional[str],
    broadcast: Broadcast,
    action_taken: str = "logged",
    connector_id: Optional[int] = None,
) -> None:
    """Report a Tier-1 finding. action_taken is what enforce mode does; in
    monitor mode it is recorded as recommended_action and the event says logged."""
    key = rules.throttle_key(cpid, violation.rule_id)
    if not THROTTLE.should_emit(key, violation.rule_id, time.time()):
        return
    recommended = None
    if not ENFORCE and action_taken != "logged":
        recommended, action_taken = action_taken, "logged"
    event = build_threat(cpid, violation, raw_frame, action_taken, recommended, connector_id)
    if recommended is None:
        label = action_taken
    else:
        label = "monitor: would " + ("quarantine" if recommended == "quarantined" else "block")
    where = cpid if connector_id in (None, 1) else connector_label(cpid, connector_id)
    print(f"[proxy] THREAT {where} {violation.rule_id} ({label}): {violation.reason}", flush=True)
    _log_incident(event)
    await broadcast(event)


def _score_sample(feats) -> float:
    """Tier-2 score for one sample, honouring the cold-start contract.

    dp_dt is undefined on sample 1 and noisy on sample 2, so samples 1–2 score
    0.0 (BRIEF / FIX-10). From sample 3 on, the fitted forest scores the frozen
    5-dim vector; the residual feature is what climbs on a spoof/drift.
    """
    if feats.sample_count <= ML_COLD_START_SAMPLES or ML_MODEL is None:
        return 0.0
    return ML_MODEL.score(feats.as_vector())


async def _raise_ml_threat(
    conn: ConnectorState, ml_score: float, raw: str, broadcast: Broadcast
) -> None:
    """Emit a throttled Tier-2 ThreatEvent (the yellow ML badge, plan H13)."""
    if not THROTTLE.should_emit(conn.label, ML_RULE_ID, time.time()):
        return
    event = ThreatEvent(
        station_id=conn.cpid,
        connector_id=conn.connector_id,
        ts=time.time(),
        tier=2,
        ml_score=round(ml_score, 3),
        severity="medium",
        reason=f"behavioural anomaly on {conn.label} (ML {ml_score:.2f})",
        action_taken="logged",
        raw_frame=raw,
        node_id=NODE_ID,
    )
    print(f"[proxy] THREAT {conn.label} {ML_RULE_ID} (logged): ML score {ml_score:.2f}", flush=True)
    _log_incident(event)
    await broadcast(event)


async def _emit_telemetry(
    state: ProxyState,
    conn: ConnectorState,
    mv: dict,
    elec: ocpp.Electrical,
    raw: str,
    broadcast: Broadcast,
):
    """Fold a MeterValues into its plug's state, score it, broadcast a TelemetryEvent.

    Returns (SessionFeatures, TelemetryEvent | None): the features let the Tier-1
    pass judge the energy residual (R6) off the same accumulators, rather than
    integrating the session twice.
    """
    now = time.time()
    feats = conn.record_meter_sample(
        power_kw=mv["power_kw"],
        soc=mv["soc"],
        energy_register_kwh=mv["energy_register_kwh"],
        ts=now,
    )
    conn.record_electrical(elec.ac_power_kw, elec.ac_voltage_ll_v, elec.frequency_hz, now)
    ml_score = _score_sample(feats)
    status = StationStatus.QUARANTINED if state.is_quarantined(conn.cpid) else conn.status
    try:
        event = TelemetryEvent(
            station_id=conn.cpid,
            connector_id=conn.connector_id,
            transaction_id=conn.transaction_id,
            ts=now,
            power_kw=feats.power_kw,
            soc=min(100.0, max(0.0, feats.soc)),  # schema bounds; a spoof may lie
            dp_dt=feats.dp_dt,
            duration_sec=feats.duration_sec,
            energy_register_kwh=mv["energy_register_kwh"],
            energy_residual_kwh=feats.energy_residual_kwh,
            status=status,
            ml_score=ml_score,
            dc_voltage_v=elec.dc_voltage_v,
            dc_current_a=elec.dc_current_a,
            ac_voltage_v=round(elec.ac_voltage_ll_v, 1) if elec.ac_voltage_ll_v is not None else None,
            frequency_hz=elec.frequency_hz,
        )
    except Exception as exc:  # never let one bad frame kill the feed
        print(f"[proxy] {conn.label} telemetry build failed: {exc}", flush=True)
        return feats, None
    conn.last_telemetry = event
    await broadcast(event)
    if ml_score > ml_engine.ML_ALERT_THRESHOLD:
        await _raise_ml_threat(conn, ml_score, raw, broadcast)
    return feats, event


def _connector_id(payload: dict) -> int:
    """connectorId from a payload; malformed or missing reads as connector 1."""
    cid = payload.get("connectorId")
    return cid if isinstance(cid, int) and not isinstance(cid, bool) and cid >= 0 else 1


def _on_plug(violation: rules.RuleViolation, connector_id: int) -> rules.RuleViolation:
    """Name the plug in a finding from a multi-connector charger."""
    if connector_id == 1 or "connector" in violation.reason:
        return violation
    return rules.RuleViolation(violation.rule_id, violation.severity, f"connector {connector_id}: {violation.reason}")


async def _publish_quarantined(station, broadcast: Broadcast) -> None:
    """Re-publish every plug of a severed charger as Quarantined, delivering nothing.

    The telemetry that tripped the rule went out before the verdict, still
    "Charging", and the charger's other plugs stop too.
    """
    for c in station.plugs():
        t = c.last_telemetry
        if t is None:
            continue
        c.last_telemetry = t.model_copy(update={
            "status": StationStatus.QUARANTINED,
            "power_kw": 0.0,
            "dc_current_a": 0.0 if t.dc_current_a is not None else None,
        })
        await broadcast(c.last_telemetry)


async def _publish_stopped(conn: ConnectorState, broadcast: Broadcast) -> None:
    """A plug whose session just ended delivers nothing; say so on the feed.

    Telemetry otherwise only follows MeterValues, so the dashboard would keep
    showing the last reading (Charging, 120 kW) until the next session.
    """
    t = conn.last_telemetry
    if t is None:
        return
    conn.last_telemetry = t.model_copy(update={
        "ts": time.time(),
        "status": StationStatus.FINISHING,
        "power_kw": 0.0,
        "dc_current_a": 0.0 if t.dc_current_a is not None else None,
    })
    await broadcast(conn.last_telemetry)


async def inspect_upstream(
    state: ProxyState,
    oscillation: rules.OscillationDetector,
    cpid: str,
    raw: str,
    broadcast: Broadcast,
) -> Verdict:
    """Score a charger->CSMS frame, emit telemetry/threats, return a Verdict."""
    # Mid-quarantine: drop everything, and close once the ChangeAvailability ack
    # comes back from the charger.
    pending_uid = state.quarantine_ack_pending.get(cpid)
    if pending_uid is not None:
        try:
            frame = ocpp.parse_frame(raw)
        except ValueError:
            return Verdict(forward=False)
        if frame.message_type_id == ocpp.CALLRESULT and frame.unique_id == pending_uid:
            state.quarantine_ack_pending.pop(cpid, None)
            return Verdict(forward=False, close=True)
        return Verdict(forward=False)

    try:
        frame = ocpp.parse_frame(raw)
    except ValueError as exc:
        print(f"[proxy] {cpid} unparseable frame: {exc}", flush=True)
        return Verdict(forward=True)  # stay a transparent relay for junk we can't read

    if frame.message_type_id != ocpp.CALL:
        return Verdict(forward=True)

    state.note_pending(cpid, frame.unique_id, frame.action)
    action = frame.action
    station = state.station(cpid)
    payload = frame.payload or {}

    if action == "Authorize":
        id_tag = payload.get("idTag") if isinstance(payload.get("idTag"), str) else None
        card = _card_violation(state, cpid, id_tag)
        if card is not None:
            await raise_threat(cpid, card, raw, broadcast, action_taken="frame_dropped")
        if card is not None and ENFORCE:
            # Answer the charger ourselves; the CSMS never sees the cloned card.
            state.resolve_pending(cpid, frame.unique_id)
            if card.rule_id == "R8_TAG_CLONE" and FEDERATION is not None and id_tag:
                FEDERATION.publish_card_ioc(id_tag, card.reason)
            reply = ocpp.serialise_result(frame.unique_id, {"idTagInfo": {"status": "Blocked"}})
            return Verdict(forward=False, reply=reply, card_blocked=True)
        state.apply_authorize(cpid)

    elif action == "MeterValues":
        cid = _connector_id(payload)
        txn = payload.get("transactionId")
        elec = ocpp.extract_electrical(payload)
        now = time.time()
        conn = station.connector(cid)
        if cid == MAIN_METER or (txn is None and not conn.session_live):
            # The charger's main meter, or a clock-aligned reading from an idle
            # plug: legitimate OCPP with no session to judge. Keep the supply
            # voltage and frequency, run no session rules.
            conn.record_electrical(None, elec.ac_voltage_ll_v, elec.frequency_hz, now)
            return Verdict(forward=True)

        mv = ocpp.extract_meter_values(payload)
        feats, _telemetry = await _emit_telemetry(state, conn, mv, elec, raw, broadcast)

        quarantine_uid: Optional[str] = None
        quarantine_reason: Optional[str] = None
        violations = [
            rules.check_state_order(conn.label, conn.session_live, action) if txn is not None else None,
            rules.check_txn_integrity(state, cpid, txn, cid),
            rules.check_physics(mv["power_kw"], mv["soc"]),
            rules.check_electrical(mv["power_kw"], elec.dc_voltage_v, elec.dc_current_a),
            rules.check_meter_fraud(feats.energy_residual_kwh, feats.residual_rate_kwh_per_sec),
        ]
        for violation in violations:
            if not violation:
                continue
            violation = _on_plug(violation, cid)
            quarantine = rules.should_quarantine(violation.rule_id)
            await raise_threat(
                cpid, violation, raw, broadcast,
                action_taken="quarantined" if quarantine else "logged",
                connector_id=cid,
            )
            if quarantine and ENFORCE and quarantine_uid is None:
                quarantine_uid = uuid.uuid4().hex
                quarantine_reason = f"{violation.rule_id}: {violation.reason}"
                state.quarantine(cpid)
                state.quarantine_ack_pending[cpid] = quarantine_uid

        if quarantine_uid is not None:
            await _publish_quarantined(station, broadcast)
            return Verdict(
                forward=False, start_quarantine=True, change_availability_uid=quarantine_uid,
                quarantine_reason=quarantine_reason,
            )

    elif action == "StartTransaction":
        cid = _connector_id(payload) or 1
        id_tag = payload.get("idTag") if isinstance(payload.get("idTag"), str) else None
        station.pending_start[frame.unique_id] = (cid, id_tag)
        conn = station.connector(cid)
        now = time.time()
        since_stop = now - conn.stopped_at if conn.stopped_at is not None else None
        if rules.is_oscillation_transition(action, conn.status, since_stop):
            await _record_transition(oscillation, cpid, raw, broadcast, now)

    elif action == "StopTransaction":
        txn = payload.get("transactionId")
        conn = state.connector_for_txn(cpid, txn)
        if conn is None:
            order = rules.RuleViolation(
                "R1_STATE_ORDER", "high", f"StopTransaction on {cpid} for no live transaction ({txn})"
            )
            await raise_threat(cpid, order, raw, broadcast)
        else:
            stopped = state.apply_stop_transaction(cpid, txn)
            if stopped is not None and not state.is_quarantined(cpid):
                await _publish_stopped(stopped, broadcast)
        await _record_transition(oscillation, cpid, raw, broadcast, time.time())

    return Verdict(forward=True)


async def _record_transition(oscillation, cpid: str, raw: str, broadcast: Broadcast, now: float) -> None:
    oscillation.record_transition(now)
    osc = oscillation.check(now)
    if osc:
        await raise_threat(cpid, osc, raw, broadcast)


def inspect_downstream(state: ProxyState, cpid: str, raw: str) -> None:
    """Catch a StartTransaction CALLRESULT and link its transactionId to the plug."""
    try:
        frame = ocpp.parse_frame(raw)
    except ValueError:
        return
    if frame.message_type_id != ocpp.CALLRESULT:
        return
    action = state.resolve_pending(cpid, frame.unique_id)
    if action == "StartTransaction":
        payload = frame.payload or {}
        cid, id_tag = state.station(cpid).pending_start.pop(frame.unique_id, (1, None))
        accepted = (payload.get("idTagInfo") or {}).get("status") == "Accepted"
        txn = payload.get("transactionId")
        if isinstance(txn, int):
            state.apply_start_transaction(cpid, txn, cid, id_tag=id_tag if accepted else None)
