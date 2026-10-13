"""The proxy's defence counters (GridEvent.defences), driven through proxy.main.

The relay and the handshake are fed stand-in sockets, so each counter is checked
against the frames that should move it.
"""

import asyncio
import json

import pytest
import websockets
from websockets.frames import Close

from proxy import main, pipeline, rules
from proxy.state import ProxyState


class FakeCharger:
    """Yields the given frames as a charger socket would, records what it is sent."""

    def __init__(self, frames, path="/ocpp/CP-01", fail_with=None):
        self.frames = list(frames)
        self.path = path
        self.fail_with = fail_with
        self.sent = []
        self.closed = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.frames:
            return self.frames.pop(0)
        if self.fail_with is not None:
            raise self.fail_with
        raise StopAsyncIteration

    async def send(self, raw):
        self.sent.append(raw)

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)


class FakeCsms:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(raw)


def _call(uid, action, payload):
    return json.dumps([2, uid, action, payload])


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(main, "DEFENCES", main.DefenceCounters())
    monkeypatch.setattr(main, "STATE", ProxyState())
    monkeypatch.setattr(main, "OSCILLATION", rules.OscillationDetector())
    monkeypatch.setattr(main, "FED", None)
    monkeypatch.setattr(main, "FED_DEV_KEY", False)
    monkeypatch.setattr(main, "CFG", main.parse_config([]))
    monkeypatch.setattr(pipeline, "THROTTLE", rules.ThreatThrottle())
    monkeypatch.setattr(pipeline, "FEDERATION", None)
    monkeypatch.setattr(pipeline, "ENFORCE", True)
    monkeypatch.setattr(pipeline, "_log_incident", lambda event: None)

    async def quiet(event):
        if event.__class__.__name__ == "ThreatEvent":
            main.DEFENCES.threats_recorded += 1

    monkeypatch.setattr(main, "emit", quiet)


@pytest.mark.asyncio
async def test_clean_frames_are_inspected_and_forwarded():
    charger, csms = FakeCharger([_call("b1", "BootNotification", {}), _call("h1", "Heartbeat", {})]), FakeCsms()
    await main._relay_upstream(charger, csms, "CP-01")
    d = main.DEFENCES
    assert (d.frames_inspected, d.frames_forwarded, d.frames_dropped) == (2, 2, 0)
    assert len(csms.sent) == 2


@pytest.mark.asyncio
async def test_a_cloned_card_is_answered_blocked_and_counted():
    main.STATE.apply_start_transaction("CP-02", 1001, connector_id=1, id_tag="TAG-CP-02")
    charger, csms = FakeCharger([_call("a1", "Authorize", {"idTag": "TAG-CP-02"})]), FakeCsms()
    await main._relay_upstream(charger, csms, "CP-01")
    d = main.DEFENCES
    assert (d.cards_blocked, d.frames_dropped, d.frames_forwarded) == (1, 1, 0)
    assert csms.sent == [] and "Blocked" in charger.sent[0]
    assert d.threats_recorded == 1


@pytest.mark.asyncio
async def test_a_forged_meter_reading_is_dropped_and_the_charger_quarantined(monkeypatch):
    monkeypatch.setattr(main, "QUARANTINE_ACK_TIMEOUT_SEC", 0.0)
    main.STATE.apply_start_transaction("CP-01", 1001, connector_id=1, id_tag="TAG-CP-01")
    spoof = {"connectorId": 1, "transactionId": 1001, "meterValue": [{"timestamp": "t", "sampledValue": [
        {"value": "5.0", "measurand": "Power.Active.Import", "unit": "kW"},
        {"value": "0.050", "measurand": "Energy.Active.Import.Register", "unit": "kWh"},
        {"value": "40.0", "measurand": "SoC", "unit": "Percent"},
        {"value": "717.0", "measurand": "Voltage", "unit": "V"},
        {"value": "167.0", "measurand": "Current.Import", "unit": "A"},
    ]}]}
    charger, csms = FakeCharger([_call("m1", "MeterValues", spoof)]), FakeCsms()
    await main._relay_upstream(charger, csms, "CP-01")
    d = main.DEFENCES
    assert (d.quarantines, d.frames_dropped, d.frames_forwarded) == (1, 1, 0)
    assert "ChangeAvailability" in charger.sent[0]
    await asyncio.sleep(0.01)                          # the ack deadline closes it
    assert charger.closed == (4001, "quarantined")


@pytest.mark.asyncio
async def test_an_oversize_frame_is_counted_when_the_connection_is_refused():
    too_big = websockets.ConnectionClosedError(None, Close(1009, "frame too big"))
    charger = FakeCharger([], fail_with=too_big)
    with pytest.raises(websockets.ConnectionClosed):
        await main._pump_upstream(charger, FakeCsms(), "CP-01")
    assert main.DEFENCES.oversize_frames_refused == 1


@pytest.mark.asyncio
async def test_handshake_refusals_are_counted():
    bad = FakeCharger([], path="/not-ocpp")
    await main._handle_charger(bad)
    assert bad.closed[0] == 1008 and main.DEFENCES.malformed_connections_refused == 1

    main.STATE.register_connection("CP-01")          # CP-01 is already online
    shadow = FakeCharger([], path="/ocpp/CP-01")
    await main._handle_charger(shadow)
    assert shadow.closed[0] == 4001 and main.DEFENCES.duplicate_sessions_refused == 1


def test_grid_event_carries_the_counters_and_the_set_up():
    main.DEFENCES.frames_inspected = 7
    stats = main.grid_event().defences
    assert stats.frames_inspected == 7
    assert stats.ingress_tls is False and stats.bind == "localhost"
    assert stats.max_frame_kb == 64 and stats.federation == "standalone"
