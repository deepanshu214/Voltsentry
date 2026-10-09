"""Shared threat intelligence — pure logic with an injectable clock.

Two things travel across the federation and are held here, on the hub and on
every node:

* Indicators of compromise (IoCs) with a time-to-live. kind "id_tag" carries a
  hashed RFID card; kind "charge_point" a charge point identity.
* Live charging sessions, keyed by hashed card. One card can only charge one
  car at a time, so the same card live at two sites means a cloned card. No
  single site can see that; the federation can.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Callable, Optional

from federation.protocol import ioc_id

DEFAULT_IOC_TTL_SEC = 3600.0
IOC_KINDS = ("id_tag", "charge_point")


@dataclass
class Indicator:
    ioc_id: str
    kind: str              # "id_tag" | "charge_point"
    value: str             # hashed card, or charge point id
    reason: str
    severity: str          # "low" | "medium" | "high"
    source_node: str
    first_seen: float
    expires: float
    hits: int = 1

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Indicator":
        return cls(
            ioc_id=str(d["ioc_id"]),
            kind=str(d["kind"]),
            value=str(d["value"]),
            reason=str(d.get("reason", "")),
            severity=str(d.get("severity", "high")),
            source_node=str(d["source_node"]),
            first_seen=float(d.get("first_seen", 0.0)),
            expires=float(d["expires"]),
            hits=int(d.get("hits", 1)),
        )


def new_indicator(
    kind: str, value: str, reason: str, severity: str, source_node: str, now: float,
    ttl_sec: float = DEFAULT_IOC_TTL_SEC,
) -> Indicator:
    if kind not in IOC_KINDS:
        raise ValueError(f"unknown IoC kind {kind!r}")
    return Indicator(
        ioc_id=ioc_id(kind, value), kind=kind, value=value, reason=reason,
        severity=severity, source_node=source_node, first_seen=now, expires=now + ttl_sec,
    )


@dataclass
class SessionClaim:
    node: str
    station: str
    since: float


class IntelStore:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._iocs: dict[str, Indicator] = {}
        # tag_hash -> node -> claim. A dict per tag so one node ending its
        # session never erases another node's claim on the same card.
        self._sessions: dict[str, dict[str, SessionClaim]] = {}

    # ---- indicators ------------------------------------------------------
    def _prune(self) -> None:
        now = self._clock()
        for key in [k for k, i in self._iocs.items() if i.expires <= now]:
            del self._iocs[key]

    def add_ioc(self, ioc: Indicator) -> Indicator:
        """Insert or merge. A repeat sighting extends the expiry and counts a hit."""
        self._prune()
        existing = self._iocs.get(ioc.ioc_id)
        if existing is None:
            self._iocs[ioc.ioc_id] = ioc
            return ioc
        existing.expires = max(existing.expires, ioc.expires)
        existing.hits += 1
        existing.reason = ioc.reason
        return existing

    def active_iocs(self) -> list[Indicator]:
        self._prune()
        return sorted(self._iocs.values(), key=lambda i: i.first_seen, reverse=True)

    def blocked_reason(self, kind: str, value: str, exclude_source: Optional[str] = None) -> Optional[str]:
        self._prune()
        ioc = self._iocs.get(ioc_id(kind, value))
        if ioc is None or ioc.source_node == exclude_source:
            return None
        return f"{ioc.reason} (reported by {ioc.source_node})"

    # ---- live sessions ---------------------------------------------------
    def set_session(self, node: str, tag_hash: str, station: str, live: bool) -> None:
        claims = self._sessions.setdefault(tag_hash, {})
        if live:
            claims[node] = SessionClaim(node=node, station=station, since=self._clock())
        else:
            current = claims.get(node)
            if current is not None and current.station == station:
                del claims[node]
        if not claims:
            self._sessions.pop(tag_hash, None)

    def clear_node(self, node: str) -> None:
        for tag_hash in list(self._sessions):
            self._sessions[tag_hash].pop(node, None)
            if not self._sessions[tag_hash]:
                del self._sessions[tag_hash]

    def remote_owner(self, tag_hash: str, exclude_node: str) -> Optional[tuple[str, str]]:
        """(node, station) where this card is live at a site other than exclude_node."""
        for claim in self._sessions.get(tag_hash, {}).values():
            if claim.node != exclude_node:
                return claim.node, claim.station
        return None

    def sessions(self) -> list[tuple[str, str, str]]:
        """(tag_hash, node, station) for every live claim."""
        return [(t, c.node, c.station) for t, claims in self._sessions.items() for c in claims.values()]

    def live_count(self, node: str) -> int:
        return sum(1 for claims in self._sessions.values() if node in claims)


def correlate_session(
    store: IntelStore, node: str, tag_hash: str, station: str, live: bool, now: float
) -> Optional[Indicator]:
    """Hub-side backstop: record a claim; return an IoC if it collides with another site.

    Nodes already refuse a card live elsewhere at Authorize, from their copy of
    the session table. This catches the race where two sites authorise the same
    card before either has heard about the other.
    """
    clash = store.remote_owner(tag_hash, exclude_node=node) if live else None
    store.set_session(node, tag_hash, station, live)
    if clash is None:
        return None
    other_node, other_station = clash
    return new_indicator(
        "id_tag", tag_hash,
        reason=f"card live at {other_node}/{other_station} and {node}/{station} at the same time",
        severity="high", source_node="HUB", now=now,
    )
