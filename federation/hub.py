"""VoltSentry federation hub — joins per-site VoltSentry nodes into one network.

    node port  ws://HOST:7700/node      VoltSentry proxies dial in (outbound from the site)
    feed port  ws://HOST:7701/network   network snapshot for the dashboard, 1 Hz
               http://HOST:7701/healthz liveness probe

What the hub does:
  * authenticates every node (per-node HMAC keys derived from one master secret)
  * relays threats and indicators of compromise from one site to every other
  * keeps the cross-site table of live charging sessions (hashed RFID cards)
    and flags a card that is live at two sites at once
  * reports node health: a node that stops sending heartbeats is marked stale,
    one whose socket drops is marked offline

The hub holds no charger traffic. If it goes down, every site keeps detecting
and quarantining on its own; only the cross-site view pauses.

    python -m federation.hub [--port 7700] [--feed-port 7701] [--master-secret S]
                             [--tls-cert cert.pem --tls-key key.pem]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import ssl
import time
from collections import deque
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any, Optional

import websockets
from websockets.server import serve

from federation.intel import (
    DEFAULT_IOC_TTL_SEC,
    IOC_KINDS,
    Indicator,
    IntelStore,
    correlate_session,
    new_indicator,
)
from federation.protocol import (
    DEV_MASTER_SECRET,
    HUB_ID,
    NODE_KINDS,
    ProtocolError,
    SeqCounter,
    derive_node_key,
    derive_tag_salt,
    open_envelope,
    peek_node,
    seal,
)

HELLO_TIMEOUT_SEC = 10.0
HEARTBEAT_STALE_SEC = 6.0
MAX_MESSAGE_BYTES = 256 * 1024
IOC_TTL_RANGE_SEC = (60.0, 86_400.0)
THREAT_HISTORY = 100
EVENT_HISTORY = 50


def log(msg: str) -> None:
    print(f"[hub] {msg}", flush=True)


@dataclass
class NodeRecord:
    node_id: str
    site_name: str = ""
    region: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    version: str = ""
    transformer_kva: Optional[float] = None
    bays: Optional[int] = None
    connected_since: Optional[float] = None
    last_seen: float = 0.0
    heartbeat: dict = field(default_factory=dict)
    last_heartbeat_ts: Optional[float] = None
    threats_total: int = 0
    ws: Any = None
    seq_out: SeqCounter = field(default_factory=SeqCounter)

    def status(self, now: float) -> str:
        if self.ws is None:
            return "offline"
        if self.last_heartbeat_ts is None or now - self.last_heartbeat_ts > HEARTBEAT_STALE_SEC:
            return "stale"
        return "online"


def _num(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


class Hub:
    def __init__(self, master: str, tls: bool = False) -> None:
        self.master = master
        self.tag_salt = derive_tag_salt(master)
        self.tls = tls
        self.dev_secret = master == DEV_MASTER_SECRET
        self.nodes: dict[str, NodeRecord] = {}
        self.last_seq_in: dict[str, int] = {}  # survives reconnects: replay protection
        self.intel = IntelStore()
        self.threats: deque[dict] = deque(maxlen=THREAT_HISTORY)
        self.events: deque[dict] = deque(maxlen=EVENT_HISTORY)
        self.feed_clients: set = set()
        self.started = time.time()
        self.stats = {"messages_in": 0, "relayed": 0, "auth_failures": 0, "rejected": 0}

    # ---- helpers ---------------------------------------------------------
    def key(self, node_id: str) -> str:
        return derive_node_key(self.master, node_id)

    def note(self, level: str, text: str) -> None:
        self.events.appendleft({"ts": time.time(), "level": level, "text": text})
        log(text)

    async def send(self, rec: NodeRecord, kind: str, body: dict) -> None:
        ws = rec.ws
        if ws is None:
            return
        try:
            await ws.send(seal(self.key(rec.node_id), HUB_ID, rec.seq_out.next(), kind, body))
        except websockets.ConnectionClosed:
            pass

    async def fanout(self, kind: str, body: dict, exclude: Optional[str] = None) -> None:
        targets = [r for r in self.nodes.values() if r.ws is not None and r.node_id != exclude]
        await asyncio.gather(*(self.send(r, kind, body) for r in targets))
        self.stats["relayed"] += len(targets)

    def roster(self) -> list[dict]:
        now = time.time()
        return [
            {"node_id": r.node_id, "site_name": r.site_name, "region": r.region, "status": r.status(now)}
            for r in self.nodes.values()
        ]

    # ---- node connection -------------------------------------------------
    async def handle_node(self, ws) -> None:
        peer = getattr(ws, "remote_address", None)
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=HELLO_TIMEOUT_SEC)
            node_id = peek_node(raw)
            env = open_envelope(
                raw, self.key(node_id), self.last_seq_in.get(node_id, 0), allowed_kinds={"hello"}
            )
        except (ProtocolError, asyncio.TimeoutError, websockets.ConnectionClosed) as exc:
            self.stats["auth_failures"] += 1
            self.note("warn", f"rejected connection from {peer}: {exc}")
            await ws.close(code=4401, reason="unauthorized")
            return

        self.last_seq_in[node_id] = env.seq
        rec = self.nodes.get(node_id) or NodeRecord(node_id=node_id)
        self.nodes[node_id] = rec
        if rec.ws is not None:  # a reconnect after a network blip replaces the old socket
            old = rec.ws
            rec.ws = None
            await old.close(code=4409, reason="superseded by a newer connection")

        hello = env.body
        rec.site_name = str(hello.get("site_name", ""))[:80]
        rec.region = str(hello.get("region", ""))[:80]
        rec.lat, rec.lon = _num(hello.get("lat")), _num(hello.get("lon"))
        rec.version = str(hello.get("version", ""))[:20]
        rec.transformer_kva = _num(hello.get("transformer_kva"))
        bays = _num(hello.get("bays"))
        rec.bays = int(bays) if bays is not None else None
        rec.connected_since = rec.last_seen = time.time()
        rec.last_heartbeat_ts = None
        rec.heartbeat = {}
        rec.ws = ws

        # The node re-announces its live sessions after the welcome.
        self.intel.clear_node(node_id)
        await self.fanout("session_reset", {"node_id": node_id}, exclude=node_id)
        await self.send(rec, "welcome", {
            "tag_salt": self.tag_salt,
            "hub_time": time.time(),
            "roster": self.roster(),
            "iocs": [i.to_dict() for i in self.intel.active_iocs()],
            "sessions": [
                {"node_id": n, "tag_hash": t, "station": s} for t, n, s in self.intel.sessions() if n != node_id
            ],
        })
        await self.fanout("roster", {"nodes": self.roster()})
        self.note("info", f"{node_id} ({rec.site_name or 'unnamed site'}) joined from {peer}")

        try:
            async for raw in ws:
                await self._on_message(rec, raw)
        except websockets.ConnectionClosed:
            pass
        finally:
            if rec.ws is ws:
                rec.ws = None
                self.intel.clear_node(node_id)
                await self.fanout("session_reset", {"node_id": node_id})
                await self.fanout("roster", {"nodes": self.roster()})
                self.note("warn", f"{node_id} disconnected")

    async def _on_message(self, rec: NodeRecord, raw: str) -> None:
        self.stats["messages_in"] += 1
        try:
            env = open_envelope(
                raw, self.key(rec.node_id), self.last_seq_in.get(rec.node_id, 0), allowed_kinds=NODE_KINDS
            )
            if env.node != rec.node_id:
                raise ProtocolError(f"sender {env.node} on {rec.node_id}'s socket")
        except ProtocolError as exc:
            self.stats["rejected"] += 1
            self.note("warn", f"dropped message from {rec.node_id}: {exc}")
            return
        self.last_seq_in[rec.node_id] = env.seq
        now = time.time()
        rec.last_seen = now
        body = env.body

        if env.kind == "heartbeat":
            rec.heartbeat = body
            rec.last_heartbeat_ts = now
        elif env.kind == "threat":
            threat = {k: body.get(k) for k in (
                "station_id", "ts", "tier", "rule_id", "ml_score", "severity", "reason", "action_taken",
                "recommended_action")}
            threat["node_id"] = rec.node_id
            rec.threats_total += 1
            self.threats.appendleft(threat)
            await self.fanout("threat", threat, exclude=rec.node_id)
        elif env.kind == "ioc":
            await self._on_ioc(rec, body, now)
        elif env.kind == "session":
            await self._on_session(rec, body, now)

    async def _on_ioc(self, rec: NodeRecord, body: dict, now: float) -> None:
        kind, value = body.get("kind"), body.get("value")
        if kind not in IOC_KINDS or not isinstance(value, str) or not value:
            self.stats["rejected"] += 1
            return
        ttl = _num(body.get("ttl_sec")) or DEFAULT_IOC_TTL_SEC
        ttl = min(max(ttl, IOC_TTL_RANGE_SEC[0]), IOC_TTL_RANGE_SEC[1])
        severity = body.get("severity") if body.get("severity") in ("low", "medium", "high") else "high"
        ioc = new_indicator(kind, value[:128], str(body.get("reason", ""))[:200], severity, rec.node_id, now, ttl)
        merged = self.intel.add_ioc(ioc)
        await self.fanout("ioc", merged.to_dict(), exclude=rec.node_id)
        if merged.hits == 1:
            self.note("alert", f"IoC from {rec.node_id}: {kind} {value[:12]} - {merged.reason}")

    async def _on_session(self, rec: NodeRecord, body: dict, now: float) -> None:
        tag_hash, station, live = body.get("tag_hash"), body.get("station"), body.get("live")
        if not isinstance(tag_hash, str) or not isinstance(station, str) or not isinstance(live, bool):
            self.stats["rejected"] += 1
            return
        clone = correlate_session(self.intel, rec.node_id, tag_hash, station, live, now)
        await self.fanout(
            "session", {"node_id": rec.node_id, "tag_hash": tag_hash, "station": station, "live": live},
            exclude=rec.node_id,
        )
        if clone is not None:
            merged: Indicator = self.intel.add_ioc(clone)
            await self.fanout("ioc", merged.to_dict())
            self.threats.appendleft({
                "station_id": f"{rec.node_id}/{station}", "ts": now, "tier": 1,
                "rule_id": "R8_TAG_CLONE", "ml_score": None, "severity": "high",
                "reason": merged.reason, "action_taken": "logged", "node_id": HUB_ID,
            })
            self.note("alert", f"cloned card {tag_hash[:8]}: {merged.reason}")

    # ---- dashboard feed --------------------------------------------------
    def snapshot(self) -> dict:
        now = time.time()
        nodes = []
        for r in sorted(self.nodes.values(), key=lambda n: n.node_id):
            nodes.append({
                "node_id": r.node_id, "site_name": r.site_name, "region": r.region,
                "lat": r.lat, "lon": r.lon, "version": r.version,
                "transformer_kva": r.transformer_kva, "bays": r.bays,
                "status": r.status(now),
                "last_seen_age_sec": round(now - r.last_seen, 1) if r.last_seen else None,
                "connected_since": r.connected_since,
                "threats_total": r.threats_total,
                "live_sessions": self.intel.live_count(r.node_id),
                "heartbeat": r.heartbeat,
            })
        iocs = []
        for i in self.intel.active_iocs():
            d = i.to_dict()
            d["ttl_remaining_sec"] = round(i.expires - now)
            iocs.append(d)
        return {
            "event": "network",
            "ts": now,
            "hub": {
                "uptime_sec": round(now - self.started),
                "tls": self.tls,
                "dev_secret": self.dev_secret,
                "nodes_online": sum(1 for n in nodes if n["status"] == "online"),
                **self.stats,
            },
            "nodes": nodes,
            "iocs": iocs,
            "threats": list(self.threats)[:40],
            "events": list(self.events)[:30],
        }

    async def handle_feed(self, ws) -> None:
        self.feed_clients.add(ws)
        try:
            await ws.send(json.dumps(self.snapshot()))
            await ws.wait_closed()
        finally:
            self.feed_clients.discard(ws)

    async def feed_ticker(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            if self.feed_clients:
                payload = json.dumps(self.snapshot())
                await asyncio.gather(*(c.send(payload) for c in list(self.feed_clients)), return_exceptions=True)

    async def http_routes(self, path: str, request_headers):
        if request_headers.get("Upgrade", "").lower() == "websocket":
            return None
        route = path.split("?", 1)[0]
        if route == "/healthz":
            snap = self.snapshot()["hub"]
            body = json.dumps({"status": "ok", "nodes_online": snap["nodes_online"], "uptime_sec": snap["uptime_sec"]})
            return HTTPStatus.OK, [("Content-Type", "application/json"), ("Cache-Control", "no-store")], body.encode()
        if route == "/network.json":
            body = json.dumps(self.snapshot()).encode()
            return HTTPStatus.OK, [("Content-Type", "application/json"), ("Access-Control-Allow-Origin", "*")], body
        return HTTPStatus.NOT_FOUND, [("Content-Type", "text/plain")], b"not found"


def tls_context(cert: Optional[str], key: Optional[str]) -> Optional[ssl.SSLContext]:
    if not cert:
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    return ctx


async def run(host: str, port: int, feed_port: int, master: str, cert: Optional[str], key: Optional[str]) -> None:
    ctx = tls_context(cert, key)
    hub = Hub(master, tls=ctx is not None)
    scheme = "wss" if ctx else "ws"
    async with serve(hub.handle_node, host, port, ssl=ctx, max_size=MAX_MESSAGE_BYTES), \
            serve(hub.handle_feed, host, feed_port, process_request=hub.http_routes):
        log(f"nodes dial {scheme}://{host}:{port}/node")
        log(f"dashboard feed ws://{host}:{feed_port}/network  (health: http://{host}:{feed_port}/healthz)")
        if hub.dev_secret:
            log("WARNING: using the built-in development master secret. Set VOLTSENTRY_FED_MASTER in production.")
        if not ctx:
            log("WARNING: node links are not encrypted. Pass --tls-cert/--tls-key outside a lab network.")
        await hub.feed_ticker()


def main() -> None:
    p = argparse.ArgumentParser(description="VoltSentry federation hub")
    p.add_argument("--host", default=os.environ.get("VOLTSENTRY_HUB_HOST", "localhost"))
    p.add_argument("--port", type=int, default=int(os.environ.get("VOLTSENTRY_HUB_PORT", 7700)))
    p.add_argument("--feed-port", type=int, default=int(os.environ.get("VOLTSENTRY_HUB_FEED_PORT", 7701)))
    p.add_argument("--master-secret", default=os.environ.get("VOLTSENTRY_FED_MASTER", DEV_MASTER_SECRET))
    p.add_argument("--tls-cert", default=os.environ.get("VOLTSENTRY_HUB_TLS_CERT"))
    p.add_argument("--tls-key", default=os.environ.get("VOLTSENTRY_HUB_TLS_KEY"))
    a = p.parse_args()
    try:
        asyncio.run(run(a.host, a.port, a.feed_port, a.master_secret, a.tls_cert, a.tls_key))
    except KeyboardInterrupt:
        log("shutting down")


if __name__ == "__main__":
    main()
