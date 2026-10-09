"""Tests for the federation protocol and shared intel store (pure logic)."""

import json

import pytest

from federation.intel import IntelStore, correlate_session, new_indicator
from federation.protocol import (
    ProtocolError,
    SeqCounter,
    derive_node_key,
    derive_tag_salt,
    hash_tag,
    open_envelope,
    peek_node,
    seal,
)

MASTER = "test-master"
KEY_A = derive_node_key(MASTER, "VS-A")
KEY_B = derive_node_key(MASTER, "VS-B")
NOW = 1_760_000_000.0


class Clock:
    def __init__(self, t: float = NOW) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


# ---------------------------------------------------------------- envelopes

def test_round_trip():
    raw = seal(KEY_A, "VS-A", 10, "heartbeat", {"output_kw": 480.0}, ts=NOW)
    assert peek_node(raw) == "VS-A"
    env = open_envelope(raw, KEY_A, last_seq=9, now=NOW)
    assert (env.node, env.seq, env.kind, env.body) == ("VS-A", 10, "heartbeat", {"output_kw": 480.0})


def test_per_node_keys_differ_and_are_deterministic():
    assert KEY_A != KEY_B
    assert derive_node_key(MASTER, "VS-A") == KEY_A
    assert derive_node_key("other-master", "VS-A") != KEY_A


def test_message_signed_with_another_nodes_key_is_rejected():
    # VS-B cannot speak as VS-A: the hub checks VS-A's claim with VS-A's key.
    forged = seal(KEY_B, "VS-A", 10, "ioc", {"kind": "id_tag"}, ts=NOW)
    with pytest.raises(ProtocolError, match="signature"):
        open_envelope(forged, KEY_A, last_seq=0, now=NOW)


def test_tampered_body_is_rejected():
    raw = json.loads(seal(KEY_A, "VS-A", 10, "heartbeat", {"output_kw": 480.0}, ts=NOW))
    raw["body"]["output_kw"] = 1.0
    with pytest.raises(ProtocolError, match="signature"):
        open_envelope(json.dumps(raw), KEY_A, last_seq=0, now=NOW)


def test_replayed_sequence_is_rejected():
    raw = seal(KEY_A, "VS-A", 10, "heartbeat", {}, ts=NOW)
    open_envelope(raw, KEY_A, last_seq=9, now=NOW)
    with pytest.raises(ProtocolError, match="replayed"):
        open_envelope(raw, KEY_A, last_seq=10, now=NOW)


def test_stale_timestamp_is_rejected():
    raw = seal(KEY_A, "VS-A", 10, "heartbeat", {}, ts=NOW - 600)
    with pytest.raises(ProtocolError, match="timestamp"):
        open_envelope(raw, KEY_A, last_seq=0, now=NOW)


def test_unsigned_and_non_json_messages_are_rejected():
    with pytest.raises(ProtocolError):
        open_envelope('{"node": "VS-A", "seq": 1}', KEY_A, last_seq=0, now=NOW)
    with pytest.raises(ProtocolError):
        open_envelope("not json", KEY_A, last_seq=0, now=NOW)


def test_unexpected_kind_is_rejected():
    raw = seal(KEY_A, "VS-A", 10, "welcome", {}, ts=NOW)
    with pytest.raises(ProtocolError, match="kind"):
        open_envelope(raw, KEY_A, last_seq=0, now=NOW, allowed_kinds={"heartbeat"})


def test_seq_counter_rises_even_if_the_clock_does_not():
    clock = Clock()
    seq = SeqCounter(clock)
    a, b, c = seq.next(), seq.next(), seq.next()
    assert a < b < c
    assert a == int(NOW * 1000)


def test_card_hash_is_stable_salted_and_hides_the_card():
    salt = derive_tag_salt(MASTER)
    h = hash_tag(salt, "TAG-CP-03")
    assert h == hash_tag(salt, "TAG-CP-03")
    assert h != hash_tag(derive_tag_salt("other"), "TAG-CP-03")
    assert "CP-03" not in h and len(h) == 20


# ---------------------------------------------------------------- indicators

def test_indicator_expires_after_ttl():
    clock = Clock()
    store = IntelStore(clock)
    store.add_ioc(new_indicator("id_tag", "h1", "cloned", "high", "VS-A", now=clock.t, ttl_sec=60))
    assert store.blocked_reason("id_tag", "h1") is not None
    clock.t += 61
    assert store.blocked_reason("id_tag", "h1") is None
    assert store.active_iocs() == []


def test_repeat_indicator_merges_and_counts_hits():
    clock = Clock()
    store = IntelStore(clock)
    store.add_ioc(new_indicator("charge_point", "CP-03", "R7", "high", "VS-A", now=clock.t, ttl_sec=60))
    clock.t += 30
    merged = store.add_ioc(new_indicator("charge_point", "CP-03", "R6", "high", "VS-A", now=clock.t, ttl_sec=60))
    assert merged.hits == 2 and merged.expires == clock.t + 60
    assert len(store.active_iocs()) == 1


def test_node_ignores_its_own_indicators():
    store = IntelStore(Clock())
    store.add_ioc(new_indicator("charge_point", "CP-03", "meter fraud", "high", "VS-A", now=NOW))
    assert store.blocked_reason("charge_point", "CP-03", exclude_source="VS-A") is None
    assert "VS-A" in store.blocked_reason("charge_point", "CP-03", exclude_source="VS-B")


def test_unknown_indicator_kind_is_refused():
    with pytest.raises(ValueError):
        new_indicator("ip", "1.2.3.4", "x", "low", "VS-A", now=NOW)


# ---------------------------------------------------------------- sessions / clones

def test_remote_owner_sees_other_sites_only():
    store = IntelStore(Clock())
    store.set_session("VS-A", "h1", "CP-03", live=True)
    assert store.remote_owner("h1", exclude_node="VS-A") is None
    assert store.remote_owner("h1", exclude_node="VS-B") == ("VS-A", "CP-03")


def test_ending_a_session_on_one_site_keeps_the_other_sites_claim():
    store = IntelStore(Clock())
    store.set_session("VS-A", "h1", "CP-03", live=True)
    store.set_session("VS-B", "h1", "CP-02", live=True)
    store.set_session("VS-B", "h1", "CP-02", live=False)
    assert store.remote_owner("h1", exclude_node="VS-B") == ("VS-A", "CP-03")


def test_clear_node_drops_all_its_claims():
    store = IntelStore(Clock())
    store.set_session("VS-A", "h1", "CP-01", live=True)
    store.set_session("VS-A", "h2", "CP-02", live=True)
    store.clear_node("VS-A")
    assert store.sessions() == []


def test_hub_correlation_flags_same_card_at_two_sites():
    store = IntelStore(Clock())
    assert correlate_session(store, "VS-A", "h1", "CP-03", live=True, now=NOW) is None
    ioc = correlate_session(store, "VS-B", "h1", "CHN-CP-02", live=True, now=NOW)
    assert ioc is not None and ioc.kind == "id_tag" and ioc.value == "h1"
    assert "VS-A/CP-03" in ioc.reason and "VS-B/CHN-CP-02" in ioc.reason


def test_hub_correlation_same_site_reclaim_is_not_a_clone():
    store = IntelStore(Clock())
    correlate_session(store, "VS-A", "h1", "CP-03", live=True, now=NOW)
    assert correlate_session(store, "VS-A", "h1", "CP-03", live=True, now=NOW) is None


def test_hub_correlation_sequential_use_is_not_a_clone():
    # Charge at site A, finish, then drive to site B: legitimate.
    store = IntelStore(Clock())
    correlate_session(store, "VS-A", "h1", "CP-03", live=True, now=NOW)
    correlate_session(store, "VS-A", "h1", "CP-03", live=False, now=NOW)
    assert correlate_session(store, "VS-B", "h1", "CHN-CP-02", live=True, now=NOW) is None
