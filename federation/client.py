"""Node side of the federation: one outbound link from a VoltSentry proxy to the hub.

Design rules:
  * Local protection never waits on the hub. Card checks read a local replica of
    the shared intel (IoCs + other sites' live sessions), so an Authorize is
    judged in microseconds and still judged if the hub is down.
  * Outbound only, with exponential backoff (1 s -> 15 s). Messages raised while
    disconnected wait in a bounded queue and flush on reconnect; heartbeats are
    never queued, since a stale one is worthless.
  * Raw RFID numbers never leave the site: they are hashed with the network salt
    the hub hands out after authentication. Until that salt arrives, card-bearing
    messages stay queued.
"""

from __future__ import annotations

import asyncio
import ssl
from collections import deque
from typing import Awaitable, Callable, Optional

import websockets

from federation.intel import DEFAULT_IOC_TTL_SEC, Indicator, IntelStore
from federation.protocol import (
    HUB_ID,
    HUB_KINDS,
    ProtocolError,
    SeqCounter,
    hash_tag,
    open_envelope,
    seal,
)

QUEUE_LIMIT = 500
MAX_BACKOFF_SEC = 15.0
WELCOME_TIMEOUT_SEC = 10.0
MAX_MESSAGE_BYTES = 1024 * 1024

OnMessage = Callable[[str, dict], Awaitable[None]]


class FederationClient:
    def __init__(
        self,
        hub_url: str,
        node_id: str,
        node_key: str,
        hello: dict,
        heartbeat: Callable[[], dict],
        live_sessions: Callable[[], list[tuple[str, str]]],
        on_message: Optional[OnMessage] = None,
        ssl_context: Optional[ssl.SSLContext] = None,
        heartbeat_sec: float = 2.0,
        log: Callable[[str], None] = print,
    ) -> None:
        self.hub_url = hub_url
        self.node_id = node_id
        self._key = node_key
        self._hello = hello
        self._heartbeat = heartbeat
        self._live_sessions = live_sessions
        self._on_message = on_message
        self._ssl = ssl_context
        self._heartbeat_sec = heartbeat_sec
        self._log = log

        self.intel = IntelStore()
        self.tag_salt: Optional[str] = None
        self.peers: list[dict] = []
        self.connected = False
        self.last_error = ""
        self.stats = {"sent": 0, "received": 0, "rejected": 0, "reconnects": 0, "dropped": 0}

        self._queue: deque[tuple[str, dict]] = deque()
        self._wake = asyncio.Event()
        self._seq = SeqCounter()
        self._last_hub_seq = 0

    # ---- public API used by the proxy ------------------------------------
    def card_hash(self, id_tag: str) -> Optional[str]:
        return hash_tag(self.tag_salt, id_tag) if self.tag_salt else None

    def check_card(self, id_tag: str) -> tuple[Optional[tuple[str, str]], Optional[str]]:
        """(remote_owner, blocked_reason) for a card, from the local intel replica."""
        h = self.card_hash(id_tag)
        if h is None:
            return None, None
        return self.intel.remote_owner(h, exclude_node=self.node_id), self.intel.blocked_reason("id_tag", h)

    def charge_point_blocked(self, cpid: str) -> Optional[str]:
        return self.intel.blocked_reason("charge_point", cpid, exclude_source=self.node_id)

    def publish_threat(self, threat: dict) -> None:
        threat = {k: v for k, v in threat.items() if k != "raw_frame"}  # raw frames may carry card numbers
        self._enqueue("threat", threat)

    def publish_ioc(
        self, kind: str, value: str, reason: str, severity: str = "high", ttl_sec: float = DEFAULT_IOC_TTL_SEC
    ) -> None:
        self._enqueue("ioc", {"kind": kind, "value": value, "reason": reason, "severity": severity, "ttl_sec": ttl_sec})

    def publish_card_ioc(self, id_tag: str, reason: str, ttl_sec: float = DEFAULT_IOC_TTL_SEC) -> None:
        self._enqueue("ioc", {"kind": "id_tag", "_card": id_tag, "reason": reason, "severity": "high", "ttl_sec": ttl_sec})

    def claim_session(self, id_tag: str, station: str, live: bool) -> None:
        self._enqueue("session", {"_card": id_tag, "station": station, "live": live})

    # ---- internals -------------------------------------------------------
    def _enqueue(self, kind: str, body: dict) -> None:
        if len(self._queue) >= QUEUE_LIMIT:
            self._queue.popleft()
            self.stats["dropped"] += 1
        self._queue.append((kind, body))
        self._wake.set()

    def _materialise(self, kind: str, body: dict) -> Optional[dict]:
        """Swap a raw card for its hash. None while the salt is still unknown."""
        if "_card" not in body:
            return body
        h = self.card_hash(body["_card"])
        if h is None:
            return None
        out = {k: v for k, v in body.items() if k != "_card"}
        out["value" if kind == "ioc" else "tag_hash"] = h
        return out

    async def _send(self, ws, kind: str, body: dict) -> None:
        await ws.send(seal(self._key, self.node_id, self._seq.next(), kind, body))
        self.stats["sent"] += 1

    def _open(self, raw: str, kinds: set[str]):
        env = open_envelope(raw, self._key, self._last_hub_seq, allowed_kinds=kinds)
        if env.node != HUB_ID:
            raise ProtocolError(f"message from {env.node}, expected the hub")
        self._last_hub_seq = env.seq
        return env

    def _apply_welcome(self, body: dict) -> None:
        salt = body.get("tag_salt")
        if not isinstance(salt, str) or not salt:
            raise ProtocolError("welcome without tag salt")
        self.tag_salt = salt
        self.peers = body.get("roster", []) or []
        self.intel = IntelStore()
        for d in body.get("iocs", []) or []:
            try:
                self.intel.add_ioc(Indicator.from_dict(d))
            except (KeyError, TypeError, ValueError):
                self.stats["rejected"] += 1
        for s in body.get("sessions", []) or []:
            if isinstance(s, dict) and all(isinstance(s.get(k), str) for k in ("node_id", "tag_hash", "station")):
                self.intel.set_session(s["node_id"], s["tag_hash"], s["station"], live=True)

    async def _dispatch(self, kind: str, body: dict) -> None:
        if kind == "roster":
            self.peers = body.get("nodes", []) or []
        elif kind == "ioc":
            try:
                ioc = Indicator.from_dict(body)
            except (KeyError, TypeError, ValueError):
                self.stats["rejected"] += 1
                return
            already_known = any(i.ioc_id == ioc.ioc_id for i in self.intel.active_iocs())
            self.intel.add_ioc(ioc)
            if already_known:
                return  # a repeat sighting refreshes the expiry; nothing new to act on
        elif kind == "session":
            node, tag_hash, station, live = (body.get(k) for k in ("node_id", "tag_hash", "station", "live"))
            if isinstance(node, str) and isinstance(tag_hash, str) and isinstance(station, str) and isinstance(live, bool):
                self.intel.set_session(node, tag_hash, station, live)
        elif kind == "session_reset":
            if isinstance(body.get("node_id"), str):
                self.intel.clear_node(body["node_id"])
        if self._on_message is not None:
            await self._on_message(kind, body)

    async def _sender(self, ws) -> None:
        while True:
            held: list[tuple[str, dict]] = []
            while self._queue:
                kind, body = self._queue.popleft()
                wire = self._materialise(kind, body)
                if wire is None:
                    held.append((kind, body))
                    continue
                await self._send(ws, kind, wire)
            self._queue.extendleft(reversed(held))
            self._wake.clear()
            await self._wake.wait()

    async def _receiver(self, ws) -> None:
        async for raw in ws:
            try:
                env = self._open(raw, HUB_KINDS - {"welcome"})
            except ProtocolError as exc:
                self.stats["rejected"] += 1
                self._log(f"federation: dropped hub message ({exc})")
                continue
            self.stats["received"] += 1
            await self._dispatch(env.kind, env.body)

    async def _heartbeater(self, ws) -> None:
        while True:
            await self._send(ws, "heartbeat", self._heartbeat())
            await asyncio.sleep(self._heartbeat_sec)

    async def _session(self) -> None:
        async with websockets.connect(
            self.hub_url, ssl=self._ssl, max_size=MAX_MESSAGE_BYTES, open_timeout=10
        ) as ws:
            await self._send(ws, "hello", self._hello)
            raw = await asyncio.wait_for(ws.recv(), timeout=WELCOME_TIMEOUT_SEC)
            env = self._open(raw, {"welcome"})
            self._apply_welcome(env.body)

            # Session claims queued while offline are stale; re-announce the truth.
            self._queue = deque(item for item in self._queue if item[0] != "session")
            for id_tag, station in self._live_sessions():
                self.claim_session(id_tag, station, live=True)

            self.connected = True
            self.last_error = ""
            self._log(f"federation: connected to {self.hub_url} ({len(self.peers)} node(s) in roster)")
            tasks = [asyncio.create_task(c) for c in (self._sender(ws), self._receiver(ws), self._heartbeater(ws))]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    if t.exception() is not None:
                        raise t.exception()
            finally:
                for t in tasks:
                    t.cancel()

    async def run(self) -> None:
        delay = 1.0
        while True:
            try:
                await self._session()
                self.last_error = "hub closed the link"
            except asyncio.CancelledError:
                raise
            except (OSError, asyncio.TimeoutError, ProtocolError, websockets.WebSocketException) as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
            was_connected = self.connected
            self.connected = False
            if was_connected:
                delay = 1.0
            self.stats["reconnects"] += 1
            self._log(f"federation: hub unreachable ({self.last_error}); retrying in {delay:.0f}s")
            await asyncio.sleep(delay)
            delay = min(delay * 2, MAX_BACKOFF_SEC)
