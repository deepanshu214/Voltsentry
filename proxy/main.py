"""VoltSentry proxy — asyncio transport + fanout. One process per charging site.

  * OCPP ingress   accept ws://HOST:8000/ocpp/{cpid}, relay to the CSMS
  * dashboard feed fan out schema events on ws://HOST:8100/feed
                   (HTTP GET /healthz on the same port for liveness probes)
  * federation     optional outbound link to the VoltSentry hub (--hub-url)

Transport only. Per-frame detection is delegated to pipeline.py; judgement to
rules.py; memory to state.py (BRIEF.md). It detects, emits TelemetryEvent +
GridEvent on the feed, and actively quarantines a compromised station
(ChangeAvailability downstream, then close 4001 on the ack).

Every setting has a flag and an environment variable, so the same build runs
as a laptop demo (defaults) or as one node of a multi-site network:

    python -m proxy.main --node-id VS-CHENNAI --site-name "Chennai Highway Hub" \\
        --ingress-port 8001 --feed-port 8101 --hub-url ws://localhost:7700/node
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import ssl
import time
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Optional

import websockets
from websockets.client import connect
from websockets.server import serve

from shared import electrical as el
from shared.schemas import DefenceStats, GridEvent, ThreatEvent
from proxy import ocpp, pipeline, rules
from proxy.state import ProxyState, connector_label

OCPP_SUBPROTOCOL = "ocpp1.6"
GRID_TICK_SEC = 1.0
QUARANTINE_ACK_TIMEOUT_SEC = 3.0  # close anyway if the charger never acks
# An OCPP 1.6 frame is a few hundred bytes. Anything near this is an attack on
# the proxy's memory, not a meter reading.
MAX_OCPP_FRAME_BYTES = 64 * 1024
# How long another site's report about a charge point keeps it off this site.
CHARGE_POINT_IOC_TTL_SEC = 900.0
VERSION = "2.0"


@dataclass
class Config:
    node_id: str
    site_name: str
    region: str
    lat: Optional[float]
    lon: Optional[float]
    bind: str
    ingress_port: int
    feed_port: int
    csms_url: str
    bays: int
    transformer_kva: float
    hub_url: Optional[str]
    fed_key: Optional[str]
    hub_ca: Optional[str]
    tls_cert: Optional[str]
    tls_key: Optional[str]
    mode: str


def parse_config(argv: Optional[list[str]] = None) -> Config:
    env = os.environ.get
    p = argparse.ArgumentParser(description="VoltSentry OCPP security proxy (one site)")
    p.add_argument("--node-id", default=env("VOLTSENTRY_NODE_ID", "VS-VELLORE"))
    p.add_argument("--site-name", default=env("VOLTSENTRY_SITE_NAME", "Vellore Campus Hub"))
    p.add_argument("--region", default=env("VOLTSENTRY_REGION", "Tamil Nadu"))
    p.add_argument("--lat", type=float, default=float(env("VOLTSENTRY_LAT", "12.9692")))
    p.add_argument("--lon", type=float, default=float(env("VOLTSENTRY_LON", "79.1559")))
    p.add_argument("--bind", default=env("VOLTSENTRY_BIND", "localhost"),
                   help="interface to listen on; 0.0.0.0 for real chargers on the LAN")
    p.add_argument("--ingress-port", type=int, default=int(env("VOLTSENTRY_INGRESS_PORT", "8000")))
    p.add_argument("--feed-port", type=int, default=int(env("VOLTSENTRY_FEED_PORT", "8100")))
    p.add_argument("--csms-url", default=env("VOLTSENTRY_CSMS_URL", "ws://localhost:9000"),
                   help="upstream CSMS base; /ocpp/{cpid} is appended")
    p.add_argument("--bays", type=int, default=int(env("VOLTSENTRY_BAYS", str(el.SITE_BAYS))),
                   help="charger bays at this site")
    p.add_argument("--transformer-kva", type=float, default=env("VOLTSENTRY_TRANSFORMER_KVA"),
                   help="site transformer rating (default: smallest standard size for --bays at full power)")
    p.add_argument("--hub-url", default=env("VOLTSENTRY_HUB_URL"),
                   help="federation hub, e.g. ws://hub:7700/node (omit for a standalone site)")
    p.add_argument("--fed-key", default=env("VOLTSENTRY_FED_KEY"),
                   help="this node's federation key (python -m federation.keys NODE_ID)")
    p.add_argument("--hub-ca", default=env("VOLTSENTRY_HUB_CA"), help="CA bundle to verify a wss:// hub")
    p.add_argument("--tls-cert", default=env("VOLTSENTRY_TLS_CERT"), help="serve OCPP ingress as wss://")
    p.add_argument("--tls-key", default=env("VOLTSENTRY_TLS_KEY"))
    p.add_argument("--mode", choices=("enforce", "monitor"), default=env("VOLTSENTRY_MODE", "enforce"),
                   help="monitor: detect and report everything, block nothing (start new sites here)")
    a = p.parse_args(argv)
    transformer_kva = float(a.transformer_kva) if a.transformer_kva else float(el.transformer_for_fleet(a.bays).total_kva)
    return Config(
        node_id=a.node_id, site_name=a.site_name, region=a.region, lat=a.lat, lon=a.lon,
        bind=a.bind, ingress_port=a.ingress_port, feed_port=a.feed_port,
        csms_url=a.csms_url.rstrip("/"), bays=a.bays, transformer_kva=transformer_kva,
        hub_url=a.hub_url, fed_key=a.fed_key, hub_ca=a.hub_ca,
        tls_cert=a.tls_cert, tls_key=a.tls_key, mode=a.mode,
    )


# Process-wide shared state: one registry for the whole fleet (R3 is fleet-wide,
# the txn registry spans stations) and one oscillation window.
STATE = ProxyState()
OSCILLATION = rules.OscillationDetector()
CFG: Config = None  # type: ignore[assignment]  # set in main()
FED: Any = None     # federation.client.FederationClient when --hub-url is set
FED_DEV_KEY = False  # the node is signing with the development key
STARTED = time.time()


@dataclass
class DefenceCounters:
    """What the defences have done since start; published as GridEvent.defences."""

    frames_inspected: int = 0
    frames_forwarded: int = 0
    frames_dropped: int = 0
    quarantines: int = 0
    cards_blocked: int = 0
    duplicate_sessions_refused: int = 0
    blocklisted_chargers_refused: int = 0
    oversize_frames_refused: int = 0
    malformed_connections_refused: int = 0
    threats_recorded: int = 0


DEFENCES = DefenceCounters()


def log(msg: str) -> None:
    print(f"[proxy] {msg}", flush=True)


class FeedHub:
    """Connected dashboard sockets + a broadcast coroutine."""

    def __init__(self) -> None:
        self._clients: set = set()

    def add(self, ws) -> None:
        self._clients.add(ws)

    def discard(self, ws) -> None:
        self._clients.discard(ws)

    @property
    def count(self) -> int:
        return len(self._clients)

    async def broadcast(self, event) -> None:
        # Always serialise via the schema model — never hand-build the dict.
        if not self._clients:
            return
        payload = event.model_dump_json()
        await asyncio.gather(
            *(c.send(payload) for c in list(self._clients)),
            return_exceptions=True,
        )


FEED = FeedHub()


async def emit(event) -> None:
    """Every event leaves through here: stamped with this node, shared if a threat."""
    if isinstance(event, ThreatEvent):
        DEFENCES.threats_recorded += 1
        if FED is not None:
            FED.publish_threat(event.model_dump())
    await FEED.broadcast(event)


def _cpid_from_path(path: str) -> str | None:
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2 and parts[0] == "ocpp":
        return parts[1]
    return None


async def _quarantine_deadline(charger, cpid: str) -> None:
    """Close a quarantined charger even if it never acks the ChangeAvailability.

    BRIEF: without this the quarantine would hang mid-demo if the twin doesn't
    reply. Harmless if the ack path already closed — close() is idempotent.
    """
    await asyncio.sleep(QUARANTINE_ACK_TIMEOUT_SEC)
    if cpid in STATE.quarantine_ack_pending:
        STATE.quarantine_ack_pending.pop(cpid, None)
        log(f"{cpid} quarantine ack timed out; closing 4001")
        await charger.close(code=4001, reason="quarantined")


async def _pump_upstream(charger, csms, cpid: str) -> None:
    try:
        await _relay_upstream(charger, csms, cpid)
    except websockets.ConnectionClosed as exc:
        # websockets fails the connection with 1009 when a frame is over max_size.
        if exc.sent is not None and exc.sent.code == 1009:
            DEFENCES.oversize_frames_refused += 1
            log(f"{cpid} sent a frame over {MAX_OCPP_FRAME_BYTES // 1024} KB; connection refused (1009)")
        raise


async def _relay_upstream(charger, csms, cpid: str) -> None:
    async for raw in charger:
        DEFENCES.frames_inspected += 1
        verdict = await pipeline.inspect_upstream(STATE, OSCILLATION, cpid, raw, emit)
        if verdict.card_blocked:
            DEFENCES.cards_blocked += 1
        if not verdict.forward and not verdict.close:
            DEFENCES.frames_dropped += 1
        if verdict.reply is not None:
            await charger.send(verdict.reply)
        if verdict.start_quarantine:
            # connectorId 0 is the whole charge point: a charger whose meter lies
            # is compromised on every plug, not just the one that reported.
            ca = ocpp.serialise_call(
                "ChangeAvailability",
                {"connectorId": 0, "type": "Inoperative"},
                unique_id=verdict.change_availability_uid,
            )
            log(f"{cpid} QUARANTINE -> ChangeAvailability(Inoperative)")
            DEFENCES.quarantines += 1
            await charger.send(ca)
            if FED is not None:
                FED.publish_ioc("charge_point", cpid, verdict.quarantine_reason or "quarantined",
                                ttl_sec=CHARGE_POINT_IOC_TTL_SEC)
            asyncio.create_task(_quarantine_deadline(charger, cpid))
            continue  # drop the offending frame
        if verdict.close:
            log(f"{cpid} quarantine ack received; closing 4001")
            await charger.close(code=4001, reason="quarantined")
            return
        if verdict.forward:
            DEFENCES.frames_forwarded += 1
            await csms.send(raw)


async def _pump_downstream(csms, charger, cpid: str) -> None:
    async for raw in csms:
        pipeline.inspect_downstream(STATE, cpid, raw)
        await charger.send(raw)


async def _handle_charger(charger) -> None:
    cpid = _cpid_from_path(charger.path)
    if cpid is None:
        DEFENCES.malformed_connections_refused += 1
        await charger.close(code=1008, reason="expected /ocpp/{cpid}")
        return

    # R9 — another site reported this charge point identity as compromised.
    if FED is not None:
        blocked = FED.charge_point_blocked(cpid)
        if blocked:
            v = rules.RuleViolation("R9_THREAT_INTEL", "high", f"charge point on federation blocklist: {blocked}")
            await pipeline.raise_threat(cpid, v, None, emit, action_taken="quarantined")
            if pipeline.ENFORCE:
                DEFENCES.blocklisted_chargers_refused += 1
                await charger.close(code=4003, reason="blocked by threat intelligence")
                return

    # R4 — reject a second handshake for a station that already has a live socket.
    shadow = rules.check_session_unique(STATE, cpid)
    if shadow is not None:
        await pipeline.raise_threat(cpid, shadow, None, emit)
        DEFENCES.duplicate_sessions_refused += 1
        await charger.close(code=4001, reason="duplicate session")
        return

    STATE.reset_session(cpid)       # a new socket is always a new session
    STATE.clear_quarantine(cpid)    # reconnect un-quarantines (CONTEXT [FIX-7])
    STATE.register_connection(cpid)
    upstream_uri = f"{CFG.csms_url}/ocpp/{cpid}"
    log(f"{cpid} connected; dialing upstream {upstream_uri}")
    try:
        async with connect(upstream_uri, subprotocols=[OCPP_SUBPROTOCOL]) as csms:
            # Tear down as soon as EITHER side closes. gather() would wait for
            # both, so a charger hang-up would leave the downstream pump parked
            # on the CSMS socket, the handler never reaching its finally — the
            # station would stay registered and its reconnect would trip R4.
            pumps = [
                asyncio.create_task(_pump_upstream(charger, csms, cpid)),
                asyncio.create_task(_pump_downstream(csms, charger, cpid)),
            ]
            done, pending = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                exc = task.exception()
                if exc is not None and not isinstance(exc, websockets.ConnectionClosed):
                    raise exc
    except OSError as exc:
        log(f"{cpid} upstream dial failed: {exc}")
        await charger.close(code=1011, reason="upstream unavailable")
    except websockets.ConnectionClosed:
        log(f"{cpid} connection closed")
    finally:
        STATE.unregister_connection(cpid)
        STATE.reset_session(cpid)   # releases the card, so the federation hears the session end


async def _handle_feed(ws) -> None:
    # A dashboard client: we only broadcast to it; inbound frames are ignored.
    log(f"dashboard client connected on :{CFG.feed_port}/feed")
    FEED.add(ws)
    try:
        await ws.wait_closed()
    finally:
        FEED.discard(ws)
        log("dashboard client disconnected")


def grid_event() -> GridEvent:
    """The site transformer, in kVA against its kVA rating (CONTEXT.md §5.C)."""
    snap = STATE.grid()
    kva = el.apparent_kva(snap.input_kw)
    loading = kva / CFG.transformer_kva * 100.0
    v_ll = snap.lt_voltage_v
    return GridEvent(
        ts=time.time(),
        total_load_kw=round(snap.input_kw, 2),
        transformer_capacity_kva=CFG.transformer_kva,
        headroom_pct=round(max(0.0, 100.0 - loading), 1),
        active_stations=snap.active_stations,
        node_id=CFG.node_id,
        site_name=CFG.site_name,
        output_kw=round(snap.output_kw, 2),
        total_load_kva=round(kva, 2),
        loading_pct=round(loading, 1),
        power_factor=el.CHARGER_POWER_FACTOR,
        charger_efficiency=el.CHARGER_EFFICIENCY,
        hv_supply_kv=el.HV_SUPPLY_KV,
        lt_nominal_v=el.LT_NOMINAL_LL_V,
        lt_voltage_v=round(v_ll, 1) if v_ll is not None else None,
        lt_current_a=round(el.line_current_a(kva, v_ll or el.LT_NOMINAL_LL_V), 1),
        frequency_hz=round(snap.frequency_hz, 3) if snap.frequency_hz is not None else None,
        enforcement=CFG.mode,
        defences=defence_stats(),
    )


def defence_stats() -> DefenceStats:
    """The counters plus how this node is set up, for the dashboard's Defences tab."""
    if FED is None:
        federation = "standalone"
    else:
        federation = "connected" if FED.connected else "disconnected"
    return DefenceStats(
        **vars(DEFENCES),
        ingress_tls=bool(CFG.tls_cert),
        bind=CFG.bind,
        max_frame_kb=MAX_OCPP_FRAME_BYTES // 1024,
        federation=federation,
        federation_dev_key=FED_DEV_KEY,
    )


async def _grid_ticker() -> None:
    # Emit the transformer aggregate once a second (CONTEXT.md §5.C, [FIX-12]).
    # Real per-station telemetry is event-driven off MeterValues in the pipeline.
    while True:
        await asyncio.sleep(GRID_TICK_SEC)
        await FEED.broadcast(grid_event())


def _heartbeat() -> dict:
    g = grid_event()
    return {
        "stations_connected": len(STATE.live_connections),
        "charging": g.active_stations,
        "quarantined": len(STATE.quarantined),
        "output_kw": g.output_kw,
        "input_kw": g.total_load_kw,
        "load_kva": g.total_load_kva,
        "transformer_kva": g.transformer_capacity_kva,
        "loading_pct": g.loading_pct,
        "lt_voltage_v": g.lt_voltage_v,
        "frequency_hz": g.frequency_hz,
        "uptime_sec": round(time.time() - STARTED),
        "mode": CFG.mode,
    }


async def _on_federation_message(kind: str, body: dict) -> None:
    """React locally to intel another site published."""
    if kind != "ioc" or body.get("source_node") == CFG.node_id:
        return
    if body.get("kind") == "id_tag":
        for id_tag, cpid, connector_id in STATE.live_cards():
            if FED.card_hash(id_tag) == body.get("value"):
                v = rules.RuleViolation(
                    "R9_THREAT_INTEL", "high",
                    f"card charging on {connector_label(cpid, connector_id)} was flagged elsewhere: "
                    f"{body.get('reason', '')}",
                )
                await pipeline.raise_threat(cpid, v, None, emit, action_taken="logged", connector_id=connector_id)
    elif body.get("kind") == "charge_point" and body.get("value") in STATE.live_connections:
        cpid = body["value"]
        v = rules.RuleViolation(
            "R9_THREAT_INTEL", "high",
            f"{cpid} is connected here but {body.get('source_node')} reported it: {body.get('reason', '')}",
        )
        await pipeline.raise_threat(cpid, v, None, emit, action_taken="logged")


async def _health(path: str, request_headers):
    """HTTP GET /healthz on the feed port; WebSocket upgrades fall through."""
    if request_headers.get("Upgrade", "").lower() == "websocket":
        return None
    if path.split("?", 1)[0] != "/healthz":
        return HTTPStatus.NOT_FOUND, [("Content-Type", "text/plain")], b"not found"
    fed = "disabled" if FED is None else ("connected" if FED.connected else "disconnected")
    body = {
        "status": "ok",
        "node_id": CFG.node_id,
        "version": VERSION,
        "uptime_sec": round(time.time() - STARTED),
        "stations_connected": len(STATE.live_connections),
        "quarantined": sorted(STATE.quarantined),
        "dashboard_clients": FEED.count,
        "federation": fed,
        "mode": CFG.mode,
        "ml_model_loaded": pipeline.ML_MODEL is not None,
    }
    return HTTPStatus.OK, [("Content-Type", "application/json"), ("Cache-Control", "no-store")], json.dumps(body).encode()


def _ingress_tls(cfg: Config) -> Optional[ssl.SSLContext]:
    if not cfg.tls_cert:
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cfg.tls_cert, cfg.tls_key)
    return ctx


def _start_federation(cfg: Config):
    from federation.client import FederationClient
    from federation.protocol import DEV_MASTER_SECRET, derive_node_key

    global FED_DEV_KEY
    key = cfg.fed_key
    if not key:
        FED_DEV_KEY = True
        key = derive_node_key(DEV_MASTER_SECRET, cfg.node_id)
        log("WARNING: no --fed-key; using the development key. Issue real keys with python -m federation.keys.")
    hub_ssl = None
    if cfg.hub_url.startswith("wss://"):
        hub_ssl = ssl.create_default_context(cafile=cfg.hub_ca)
    hello = {
        "site_name": cfg.site_name, "region": cfg.region, "lat": cfg.lat, "lon": cfg.lon,
        "version": VERSION, "transformer_kva": cfg.transformer_kva, "bays": cfg.bays,
    }
    client = FederationClient(
        cfg.hub_url, cfg.node_id, key, hello,
        heartbeat=_heartbeat, live_sessions=STATE.live_sessions,
        on_message=_on_federation_message, ssl_context=hub_ssl, log=log,
    )
    STATE.tag_listener = client.claim_session
    pipeline.FEDERATION = client
    return client


async def main(argv: Optional[list[str]] = None) -> None:
    global CFG, FED
    CFG = parse_config(argv)
    pipeline.NODE_ID = CFG.node_id
    pipeline.ENFORCE = CFG.mode == "enforce"
    tls = _ingress_tls(CFG)
    tasks = []
    if CFG.hub_url:
        FED = _start_federation(CFG)
        tasks.append(asyncio.create_task(FED.run()))

    scheme = "wss" if tls else "ws"
    async with serve(_handle_charger, CFG.bind, CFG.ingress_port, subprotocols=[OCPP_SUBPROTOCOL],
                     ssl=tls, max_size=MAX_OCPP_FRAME_BYTES), \
            serve(_handle_feed, CFG.bind, CFG.feed_port, process_request=_health):
        log(f"node {CFG.node_id} - {CFG.site_name}")
        log(f"OCPP ingress {scheme}://{CFG.bind}:{CFG.ingress_port}/ocpp/{{cpid}} -> CSMS {CFG.csms_url}")
        log(f"dashboard feed ws://{CFG.bind}:{CFG.feed_port}/feed  (health: http://{CFG.bind}:{CFG.feed_port}/healthz)")
        log(f"site supply {el.HV_SUPPLY_KV:.0f} kV -> {CFG.transformer_kva:.0f} kVA transformer -> "
            f"{el.LT_NOMINAL_LL_V:.0f} V three-phase ({el.phase_to_neutral_v(el.LT_NOMINAL_LL_V):.0f} V phase-neutral)")
        log(f"federation: {CFG.hub_url or 'disabled (standalone site)'}")
        if CFG.mode == "monitor":
            log("mode: MONITOR-ONLY - every threat is reported, nothing is blocked or quarantined")
        else:
            log("mode: enforce - forged data is quarantined, cloned cards are blocked")
        await _grid_ticker()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("shutting down")
