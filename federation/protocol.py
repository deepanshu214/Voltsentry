"""VoltSentry federation wire protocol — pure functions, no I/O.

Every VoltSentry node (one per charging site) holds one outbound WebSocket to
the federation hub. Outbound-only means a site behind NAT or a carrier-grade
firewall needs no inbound port open.

Every message, both directions, is a signed envelope:

    {"v": 1, "node": "VS-VELLORE", "seq": 1760001234567, "ts": 1760001234.5,
     "kind": "heartbeat", "body": {...}, "sig": "<hex HMAC-SHA256>"}

* sig    HMAC-SHA256 over the canonical JSON of every other field, keyed with
         the sending node's key. The hub signs replies with the same per-node
         key, so a node can tell the hub from an impostor.
* keys   Each node's key is derived from the hub's master secret:
         key = HMAC(master, "voltsentry-node:" + node_id). The hub never stores
         a key list, and one leaked node key cannot speak for another node.
* seq    Strictly increasing per sender. It starts at the sender's clock in ms,
         so it also keeps rising across restarts. A replayed message is rejected.
* ts     Must be within MAX_SKEW_SEC of the receiver's clock.

RFID card numbers are personal data, so they never cross the wire. Nodes share
HMAC(tag_salt, idTag); the salt comes from the hub after authentication.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Optional

PROTOCOL_VERSION = 1
HUB_ID = "HUB"
MAX_SKEW_SEC = 120.0

# Development master secret. A hub started with it prints a warning; anything
# beyond a laptop demo must pass --master-secret / VOLTSENTRY_FED_MASTER.
DEV_MASTER_SECRET = "voltsentry-dev-master-change-me"

NODE_KINDS = {"hello", "heartbeat", "threat", "ioc", "session"}
HUB_KINDS = {"welcome", "roster", "threat", "ioc", "session", "session_reset"}


def derive_node_key(master: str, node_id: str) -> str:
    return hmac.new(master.encode(), f"voltsentry-node:{node_id}".encode(), hashlib.sha256).hexdigest()


def derive_tag_salt(master: str) -> str:
    return hmac.new(master.encode(), b"voltsentry-tag-salt", hashlib.sha256).hexdigest()


def hash_tag(salt: str, id_tag: str) -> str:
    """Pseudonymous card id shared across sites in place of the raw idTag."""
    return hmac.new(salt.encode(), id_tag.encode(), hashlib.sha256).hexdigest()[:20]


def ioc_id(kind: str, value: str) -> str:
    """Stable id, so the same indicator reported twice merges instead of duplicating."""
    return hashlib.sha256(f"{kind}:{value}".encode()).hexdigest()[:12]


def _canonical(fields: dict[str, Any]) -> bytes:
    return json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()


def _sign(key: str, fields: dict[str, Any]) -> str:
    return hmac.new(key.encode(), _canonical(fields), hashlib.sha256).hexdigest()


def seal(key: str, node: str, seq: int, kind: str, body: dict, ts: Optional[float] = None) -> str:
    fields = {
        "v": PROTOCOL_VERSION,
        "node": node,
        "seq": seq,
        "ts": time.time() if ts is None else ts,
        "kind": kind,
        "body": body,
    }
    return json.dumps({**fields, "sig": _sign(key, fields)})


class ProtocolError(ValueError):
    """A message failed parsing, authentication or replay checks."""


@dataclass
class Envelope:
    node: str
    seq: int
    ts: float
    kind: str
    body: dict


def peek_node(raw: str) -> str:
    """Claimed sender, read before verification so the right key can be chosen."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"not JSON: {exc}") from exc
    node = data.get("node") if isinstance(data, dict) else None
    if not isinstance(node, str) or not node:
        raise ProtocolError("missing sender")
    return node


def open_envelope(
    raw: str,
    key: str,
    last_seq: int,
    now: Optional[float] = None,
    allowed_kinds: Optional[set[str]] = None,
) -> Envelope:
    """Parse and authenticate one message. Raises ProtocolError on any failure.

    The caller stores the returned seq and passes it as last_seq next time.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ProtocolError("envelope must be an object")

    sig = data.pop("sig", None)
    if not isinstance(sig, str):
        raise ProtocolError("unsigned message")
    if not hmac.compare_digest(sig, _sign(key, data)):
        raise ProtocolError("bad signature")

    if data.get("v") != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported protocol version {data.get('v')!r}")
    seq, ts, kind, body = data.get("seq"), data.get("ts"), data.get("kind"), data.get("body")
    if not isinstance(seq, int) or not isinstance(ts, (int, float)):
        raise ProtocolError("bad seq/ts")
    if not isinstance(kind, str) or not isinstance(body, dict):
        raise ProtocolError("bad kind/body")
    if allowed_kinds is not None and kind not in allowed_kinds:
        raise ProtocolError(f"unexpected kind {kind!r}")

    now = time.time() if now is None else now
    if abs(now - ts) > MAX_SKEW_SEC:
        raise ProtocolError(f"timestamp outside +/-{MAX_SKEW_SEC:.0f}s window")
    if seq <= last_seq:
        raise ProtocolError(f"replayed or out-of-order seq {seq} <= {last_seq}")
    return Envelope(node=data["node"], seq=seq, ts=float(ts), kind=kind, body=body)


class SeqCounter:
    """Monotonic sequence numbers seeded from the wall clock in ms."""

    def __init__(self, clock=time.time) -> None:
        self._clock = clock
        self._last = 0

    def next(self) -> int:
        self._last = max(self._last + 1, int(self._clock() * 1000))
        return self._last
