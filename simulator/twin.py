import argparse
import asyncio
import json
import random
import time
import uuid
import zlib
from datetime import datetime, timezone
from typing import Dict, Optional
import websockets

from shared import electrical as el
from shared.schemas import AttackTrigger, AttackType, ControlAck
from simulator.battery import Battery

# A charger told its card is not Accepted waits this long before trying again.
AUTH_RETRY_SEC = 10.0
# How long the operator takes to hand a driver a replacement for a blocked card.
CARD_REISSUE_SEC = 3.0
# Grid oscillation: every bay switches on the same wall-clock second, so the
# site's load swings between full power and nothing once a second.
OSCILLATE_SWITCH_SEC = 1.0


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SiteModel:
    """The physical site every bay shares: one transformer on the national grid.

    * LT bus voltage sags with transformer loading (shared.electrical), so a
      fully loaded site reads ~418 V line-to-line and an idle one ~433 V.
    * Frequency belongs to the whole interconnected grid, not to this site: one
      1.25 MVA hub cannot move it. It is modelled as a mean-reverting walk
      inside the Indian operating band, independent of this site's load.
    """

    def __init__(self, transformer_kva: float = el.SITE_TRANSFORMER_KVA, seed: int = 7) -> None:
        self.transformer_kva = transformer_kva
        self.stations: Dict[str, "StationTwin"] = {}
        self.frequency_hz = el.GRID_NOMINAL_HZ
        self._rng = random.Random(seed)

    def load_kva(self) -> float:
        return sum(
            el.apparent_kva(el.ac_input_kw(s.actual_power_kw))
            for s in self.stations.values()
            if s.actual_power_kw > 0.0
        )

    def lt_voltage_v(self) -> float:
        return el.lt_bus_voltage(self.load_kva() / self.transformer_kva)

    def step_frequency(self) -> None:
        lo, hi = el.GRID_BAND_HZ
        hz = self.frequency_hz + 0.2 * (el.GRID_NOMINAL_HZ - self.frequency_hz) + self._rng.gauss(0.0, 0.006)
        self.frequency_hz = min(hi, max(lo, hz))

    async def run(self) -> None:
        while True:
            self.step_frequency()
            await asyncio.sleep(1.0)


class ConnectorTwin:
    """One plug of a charger and the car currently on it."""

    def __init__(self, cpid: str, connector_id: int, initial_soc: float, start_delay: float, max_kw: float) -> None:
        self.cpid = cpid
        self.connector_id = connector_id
        self.initial_soc = initial_soc
        self.start_delay = start_delay        # when the first car plugs in
        self.max_kw = max_kw
        self.battery = Battery(initial_soc=initial_soc, max_power_kw=max_kw)
        self.transaction_id: Optional[int] = None
        self.id_tag_override: Optional[str] = None
        self.actual_power_kw = 0.0            # what this car really draws, whatever is reported
        self.charging = False                 # a transaction is live on this plug
        self.drift_ticks = 0
        self.card_issue = 0                   # replacements issued for this plug's card
        self.last_tick: Optional[float] = None  # monotonic time of the last physics tick

    @property
    def label(self) -> str:
        return self.cpid if self.connector_id == 1 else f"{self.cpid}/{self.connector_id}"

    @property
    def own_card(self) -> str:
        # OCPP 1.6 idTags are at most 20 characters.
        base = f"TAG-{self.cpid}" if self.connector_id == 1 else f"TAG-{self.cpid}-{self.connector_id}"
        return base if self.card_issue == 0 else f"{base}-R{self.card_issue}"

    @property
    def id_tag(self) -> str:
        return self.id_tag_override or self.own_card

    def reissue_card(self) -> str:
        """Replace this plug's card after the network blocked it; the old card stays blocked."""
        self.card_issue += 1
        return self.own_card

    def elapsed(self) -> float:
        """Seconds since this plug's last physics tick.

        The meter has to count real time, as a real one does. A loop that sleeps
        one second and then waits for the CSMS takes ~1.03 s a turn; counting it
        as exactly 1 s let the energy register fall 2-3 % behind the power it
        reported, which looks like meter fraud on an honest charger.
        """
        now = time.monotonic()
        dt = 1.0 if self.last_tick is None else now - self.last_tick
        self.last_tick = now
        return dt

    def new_car(self, soc: float) -> None:
        self.battery = Battery(initial_soc=soc, max_power_kw=self.max_kw)
        self.transaction_id = None
        self.charging = False
        self.actual_power_kw = 0.0
        self.drift_ticks = 0
        self.last_tick = None


# A shared power cabinet moves power between its plugs gradually, as real load
# balancing does: a car never jumps from 120 kW to 60 kW in one second.
SHARE_RAMP_KW_PER_SEC = 1.0
# Lowest allocation a plug ramps up from when its car starts.
SHARE_FLOOR_KW = 1.0
# A finished car leaves and the next one plugs in after this long.
NEXT_CAR_SEC = 8.0


class StationTwin:
    """A charger: one OCPP connection, one power cabinet, one or more connectors.

    Connector 1 keeps the attribute names of the single-plug twin (battery,
    transaction_id, id_tag, meter_payload ...), so a one-connector charger
    behaves exactly as before. With more connectors, each plug runs its own car
    and session over the same socket, and the cabinet's rated power is shared
    between the cars that are charging.
    """

    def __init__(
        self,
        cpid: str,
        base_url: str,
        initial_soc: float = 20.0,
        start_delay: float = 0.0,
        site: Optional[SiteModel] = None,
        connectors: int = 1,
        cabinet_kw: float = el.CHARGER_RATED_KW,
    ) -> None:
        self.cpid = cpid
        self.site = site
        self.cabinet_kw = cabinet_kw
        # Small fixed phase imbalance, distinct per bay, as a real supply has.
        crc = zlib.crc32(cpid.encode())
        self.phase_offsets_v = tuple(((crc >> (8 * k)) % 13 - 6) * 0.1 for k in range(3))
        self.target_url = f"{base_url}/ocpp/{cpid}"
        self.initial_soc = initial_soc
        self.start_delay = start_delay
        self.mode = "clean"
        self.is_quarantined = False
        self.connectors: Dict[int, ConnectorTwin] = {}
        for cid in range(1, max(1, connectors) + 1):
            # Later plugs get a different car (SoC) that arrives a little later,
            # so the cabinet visibly re-shares its power.
            soc = initial_soc if cid == 1 else 15.0 + (initial_soc + 23.0 * (cid - 1)) % 45.0
            delay = 0.0 if cid == 1 else 25.0 * (cid - 1)
            self.connectors[cid] = ConnectorTwin(cpid, cid, soc, delay, cabinet_kw)
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.pending_responses: Dict[str, asyncio.Future] = {}
        self.prefix = f"[twin] [{self.cpid}]"
        self.running = True
        self.receiver_task: Optional[asyncio.Task] = None

    # ---- connector 1 view, kept for the single-plug API ---------------------
    @property
    def plug(self) -> ConnectorTwin:
        return self.connectors[1]

    @property
    def battery(self) -> Battery:
        return self.plug.battery

    @battery.setter
    def battery(self, value: Battery) -> None:
        self.plug.battery = value

    @property
    def transaction_id(self) -> Optional[int]:
        return self.plug.transaction_id

    @transaction_id.setter
    def transaction_id(self, value: Optional[int]) -> None:
        self.plug.transaction_id = value

    @property
    def id_tag_override(self) -> Optional[str]:
        return self.plug.id_tag_override

    @id_tag_override.setter
    def id_tag_override(self, value: Optional[str]) -> None:
        self.plug.id_tag_override = value

    @property
    def id_tag(self) -> str:
        return self.plug.id_tag

    @property
    def drift_ticks(self) -> int:
        return self.plug.drift_ticks

    @drift_ticks.setter
    def drift_ticks(self, value: int) -> None:
        for c in self.connectors.values():
            c.drift_ticks = value

    @property
    def actual_power_kw(self) -> float:
        """What the whole charger draws (DC), across its plugs. Feeds the site model."""
        return sum(c.actual_power_kw for c in self.connectors.values())

    @actual_power_kw.setter
    def actual_power_kw(self, value: float) -> None:
        self.plug.actual_power_kw = value

    def stop_drawing(self) -> None:
        for c in self.connectors.values():
            c.actual_power_kw = 0.0

    # ---- power sharing --------------------------------------------------------
    def allocate(self, conn: ConnectorTwin, dt: float = 1.0) -> None:
        """Move this plug's power limit toward its fair share of the cabinet."""
        active = sum(1 for c in self.connectors.values() if c.charging) or 1
        share = self.cabinet_kw / active
        limit = conn.battery.max_power_kw
        step = SHARE_RAMP_KW_PER_SEC * dt
        if limit < share:
            limit = min(share, limit + step)
        else:
            limit = max(share, limit - step)
        conn.battery.max_power_kw = max(SHARE_FLOOR_KW, limit)

    # ---- meter ----------------------------------------------------------------
    def meter_payload(self, actual_power: float, soc: float, energy_kwh: float) -> tuple[dict, float]:
        """Connector 1's MeterValues; see connector_payload."""
        return self.connector_payload(self.plug, actual_power, soc, energy_kwh)

    def connector_payload(
        self, conn: ConnectorTwin, actual_power: float, soc: float, energy_kwh: float
    ) -> tuple[dict, float]:
        """Build one MeterValues payload for a plug; returns (payload, reported_power_kw).

        Outlet (DC, to the car): power, energy register, voltage, current.
        Inlet (AC, from the site bus): this plug's share of the power drawn, the
        three line-to-line voltages, frequency. SoC is the EV's own reading.

        The two meter attacks differ in care. meter_spoof rewrites only the
        power figure, so V x I contradicts it (R7). drift scales power,
        current and supply power together, so every reading agrees with every
        other one except the energy register, which only the ML residual sees.
        """
        dc_v, dc_a = conn.battery.voltage_v, conn.battery.current_a
        standby_share = el.CHARGER_STANDBY_KW / len(self.connectors)
        reported_power = actual_power
        supply_kw = max(0.0, actual_power) / el.CHARGER_EFFICIENCY + standby_share
        if self.mode == "meter_spoof":
            reported_power = 5.0
        elif self.mode == "drift":
            conn.drift_ticks += 1
            factor = 0.98 ** conn.drift_ticks
            reported_power = actual_power * factor
            dc_a = dc_a * factor
            supply_kw = max(0.0, reported_power) / el.CHARGER_EFFICIENCY + standby_share

        bus_v = self.site.lt_voltage_v() if self.site else el.LT_NOMINAL_LL_V
        hz = self.site.frequency_hz if self.site else el.GRID_NOMINAL_HZ
        ts = iso_now()
        sampled = [
            {"value": f"{reported_power:.1f}", "measurand": "Power.Active.Import", "unit": "kW", "location": "Outlet"},
            {"value": f"{energy_kwh:.2f}", "measurand": "Energy.Active.Import.Register", "unit": "kWh", "location": "Outlet"},
            {"value": f"{soc:.1f}", "measurand": "SoC", "unit": "Percent", "location": "EV"},
            {"value": f"{dc_v:.1f}", "measurand": "Voltage", "unit": "V", "location": "Outlet"},
            {"value": f"{dc_a:.1f}", "measurand": "Current.Import", "unit": "A", "location": "Outlet"},
            {"value": f"{supply_kw:.1f}", "measurand": "Power.Active.Import", "unit": "kW", "location": "Inlet"},
        ]
        for phase, offset in zip(("L1-L2", "L2-L3", "L3-L1"), self.phase_offsets_v):
            sampled.append({"value": f"{bus_v + offset:.1f}", "measurand": "Voltage", "unit": "V",
                            "location": "Inlet", "phase": phase})
        sampled.append({"value": f"{hz:.3f}", "measurand": "Frequency", "location": "Inlet"})
        payload = {
            "connectorId": conn.connector_id,
            "transactionId": conn.transaction_id,
            "meterValue": [{"timestamp": ts, "sampledValue": sampled}],
        }
        return payload, reported_power

    # ---- OCPP plumbing ----------------------------------------------------------
    async def call(self, action: str, payload: dict) -> dict:
        """Send an OCPP CALL and wait for matching CALLRESULT."""
        if not self.ws:
            raise RuntimeError("WebSocket is not connected")
        msg_id = str(uuid.uuid4())[:8]
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self.pending_responses[msg_id] = fut
        frame = [2, msg_id, action, payload]
        await self.ws.send(json.dumps(frame))
        return await asyncio.wait_for(fut, timeout=10.0)

    async def force_reconnect(self) -> None:
        """Drop the socket so run() reconnects and replays the full session FSM.

        Flipping mode back to 'clean' is not enough: after an oscillate attack the
        twin still holds a transactionId the proxy has already torn down, so every
        subsequent MeterValues trips R1 forever. CONTEXT.md [FIX-7] requires reset
        to reconnect the station, which re-runs Boot -> Authorize -> StartTransaction
        and resynchronises both sides.
        """
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass

    async def _handle_inbound_call(self, msg_id: str, action: str, payload: dict) -> None:
        """Handle server-initiated CALLs (ChangeAvailability, Reset per [FIX-6])."""
        print(f"{self.prefix} Inbound CALL: {action}", flush=True)
        if action == "ChangeAvailability":
            avail_type = payload.get("type", "Inoperative")
            if avail_type == "Inoperative":
                self.is_quarantined = True
                self.stop_drawing()  # an Inoperative charger stops delivering at once, on every plug
                print(f"{self.prefix} Station quarantined (Inoperative)", flush=True)
            else:
                self.is_quarantined = False
                print(f"{self.prefix} Station restored (Operative)", flush=True)
            res = [3, msg_id, {"status": "Accepted"}]
            await self.ws.send(json.dumps(res))
        elif action == "Reset":
            print(f"{self.prefix} Station Reset received", flush=True)
            self.mode = "clean"
            self.is_quarantined = False
            for c in self.connectors.values():
                c.id_tag_override = None
                c.new_car(c.initial_soc)
            res = [3, msg_id, {"status": "Accepted"}]
            await self.ws.send(json.dumps(res))
        else:
            res = [3, msg_id, {"status": "Accepted"}]
            await self.ws.send(json.dumps(res))

    async def _receiver_loop(self) -> None:
        """Listen for incoming frames (CALLRESULT, CALLERROR, or inbound CALLs)."""
        try:
            async for message in self.ws:
                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    continue

                if not isinstance(data, list) or len(data) < 3:
                    continue

                msg_type = data[0]
                msg_id = data[1]

                if msg_type == 2:
                    # Inbound CALL from server
                    action = data[2]
                    payload = data[3] if len(data) > 3 else {}
                    await self._handle_inbound_call(msg_id, action, payload)
                elif msg_type == 3:
                    # CALLRESULT
                    payload = data[2] if len(data) > 2 else {}
                    if msg_id in self.pending_responses:
                        fut = self.pending_responses.pop(msg_id)
                        if not fut.done():
                            fut.set_result(payload)
                elif msg_type == 4:
                    # CALLERROR
                    if msg_id in self.pending_responses:
                        fut = self.pending_responses.pop(msg_id)
                        if not fut.done():
                            fut.set_exception(RuntimeError(f"OCPP Error: {data}"))
        except (websockets.exceptions.ConnectionClosed, asyncio.CancelledError):
            pass

    # ---- one plug's charging sessions ------------------------------------------
    async def _start(self, conn: ConnectorTwin, prefix: str) -> None:
        """Authorize (only an Accepted card charges), then StartTransaction."""
        while True:
            auth_res = await self.call("Authorize", {"idTag": conn.id_tag})
            auth_status = (auth_res.get("idTagInfo") or {}).get("status")
            if auth_status == "Accepted":
                break
            if auth_status == "Blocked" and conn.id_tag_override is None:
                # The driver's own card is blocked, so a copy of it was used
                # somewhere. The copy stays blocked; the driver gets a new card.
                old = conn.id_tag
                print(f"{prefix} Card {old} blocked; operator issues replacement {conn.reissue_card()}", flush=True)
                await asyncio.sleep(CARD_REISSUE_SEC)
                continue
            print(f"{prefix} Card {conn.id_tag} refused ({auth_status}); not charging", flush=True)
            await asyncio.sleep(AUTH_RETRY_SEC)
        print(f"{prefix} Authorized ({conn.id_tag})", flush=True)
        start_res = await self.call(
            "StartTransaction",
            {
                "connectorId": conn.connector_id,
                "idTag": conn.id_tag,
                "meterStart": int(conn.battery.energy_kwh * 1000),
                "timestamp": iso_now(),
            },
        )
        conn.transaction_id = start_res.get("transactionId", random.randint(1000, 9999))
        # A car joining a shared cabinet starts low and ramps up as the others ramp down.
        if len(self.connectors) > 1:
            conn.battery.max_power_kw = SHARE_FLOOR_KW
        conn.charging = True
        conn.last_tick = None
        print(f"{prefix} Transaction started (id: {conn.transaction_id})", flush=True)

    async def _stop(self, conn: ConnectorTwin, energy_kwh: float) -> None:
        await self.call(
            "StopTransaction",
            {
                "transactionId": conn.transaction_id,
                "meterStop": int(energy_kwh * 1000),
                "timestamp": iso_now(),
                "idTag": conn.id_tag,
            },
        )
        conn.charging = False
        conn.actual_power_kw = 0.0

    async def _oscillate_switch(self, conn: ConnectorTwin, prefix: str) -> None:
        """One second of a grid-oscillation attack: on during odd seconds, off during even.

        The state comes from the wall clock, not from a toggle, so every bay is in
        step however late it noticed the attack: the whole site switches together
        and its load swings between full power and nothing. That swing is what
        stresses the transformer. While on, the car really draws power and the
        meter reports it honestly; only the start/stop pattern is malicious.
        """
        now = time.time()
        boundary = (now // OSCILLATE_SWITCH_SEC + 1) * OSCILLATE_SWITCH_SEC
        await asyncio.sleep(boundary - now)
        on = int(round(boundary / OSCILLATE_SWITCH_SEC)) % 2 == 1
        conn.last_tick = None
        if on == conn.charging:
            return
        if conn.charging:
            print(f"{prefix} [OSCILLATE] StopTransaction", flush=True)
            await self._stop(conn, conn.battery.energy_kwh)
            return
        print(f"{prefix} [OSCILLATE] StartTransaction", flush=True)
        start_res = await self.call(
            "StartTransaction",
            {
                "connectorId": conn.connector_id,
                "idTag": conn.id_tag,
                "meterStart": int(conn.battery.energy_kwh * 1000),
                "timestamp": iso_now(),
            },
        )
        conn.transaction_id = start_res.get("transactionId", (conn.transaction_id or 0) + 1)
        conn.charging = True
        # Full power until the next switch, reported as it happens.
        actual_power, soc, energy_kwh = conn.battery.tick(dt=OSCILLATE_SWITCH_SEC)
        conn.actual_power_kw = actual_power
        meter_payload, _ = self.connector_payload(conn, actual_power, soc, energy_kwh)
        await self.call("MeterValues", meter_payload)

    async def _connector_loop(self, conn: ConnectorTwin) -> None:
        prefix = f"[twin] [{conn.label}]"
        if conn.start_delay > 0:
            await asyncio.sleep(conn.start_delay)
            conn.start_delay = 0.0  # after a reconnect the car is already plugged in
        while self.running:
            await self._start(conn, prefix)
            while self.running:
                if self.is_quarantined:
                    conn.actual_power_kw = 0.0
                    conn.last_tick = None
                    await asyncio.sleep(1.0)
                    continue

                if self.mode == "oscillate":
                    await self._oscillate_switch(conn, prefix)
                    continue

                if not conn.charging:
                    break  # an oscillation left this plug stopped: start a fresh session

                # Normal CC-CV physics tick, within this plug's share of the cabinet,
                # over the real time since the last one.
                dt = conn.elapsed()
                self.allocate(conn, dt)
                actual_power, soc, energy_kwh = conn.battery.tick(dt=dt)
                conn.actual_power_kw = actual_power
                meter_payload, reported_power = self.connector_payload(conn, actual_power, soc, energy_kwh)

                await self.call("MeterValues", meter_payload)

                mode_tag = f" [{self.mode.upper()}]" if self.mode != "clean" else ""
                print(
                    f"{prefix}{mode_tag} Pwr: {reported_power:.1f} kW (actual: {actual_power:.1f}) "
                    f"| {conn.battery.voltage_v:.0f} V {conn.battery.current_a:.0f} A "
                    f"| SoC: {soc:.1f}% | Energy: {energy_kwh:.2f} kWh",
                    flush=True,
                )

                if soc >= 100.0:
                    print(f"{prefix} Battery full (100%). Stopping transaction.", flush=True)
                    await self._stop(conn, energy_kwh)
                    # The car leaves; the next one plugs in after a short while.
                    await asyncio.sleep(NEXT_CAR_SEC)
                    conn.new_car(15.0)
                    break

                await asyncio.sleep(1.0)

    async def run(self) -> None:
        """Main lifecycle: one socket, one Boot, a session loop per plug."""
        if self.start_delay > 0:
            await asyncio.sleep(self.start_delay)

        while self.running:
            tasks: list[asyncio.Task] = []
            try:
                print(f"{self.prefix} Connecting to {self.target_url}...", flush=True)
                async with websockets.connect(self.target_url) as ws:
                    self.ws = ws
                    self.receiver_task = asyncio.create_task(self._receiver_loop())

                    await self.call(
                        "BootNotification",
                        {"chargePointVendor": "VoltSentry", "chargePointModel": "Twin-v1"},
                    )
                    print(f"{self.prefix} BootNotification accepted", flush=True)

                    tasks = [asyncio.create_task(self._connector_loop(c)) for c in self.connectors.values()]
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
                    for t in done:
                        if t.exception() is not None:
                            raise t.exception()

            except (websockets.exceptions.ConnectionClosed, ConnectionRefusedError, OSError) as e:
                self.stop_drawing()  # no socket, no session, no power
                if self.is_quarantined:
                    print(f"{self.prefix} Quarantined: waiting for reset signal before reconnecting...", flush=True)
                    while self.is_quarantined and self.running:
                        await asyncio.sleep(1.0)
                    print(f"{self.prefix} Quarantine cleared! Reconnecting to {self.target_url}...", flush=True)
                else:
                    print(f"{self.prefix} Connection lost ({e}), reconnecting in 2s...", flush=True)
                    await asyncio.sleep(2.0)
            except Exception as e:
                self.stop_drawing()
                print(f"{self.prefix} Error: {e}, restarting session in 2s...", flush=True)
                await asyncio.sleep(2.0)
            finally:
                for t in tasks:
                    t.cancel()
                for c in self.connectors.values():
                    c.charging = False
                    c.transaction_id = None
                self.stop_drawing()
                if self.receiver_task and not self.receiver_task.done():
                    self.receiver_task.cancel()


class FleetManager:
    def __init__(
        self,
        base_url: str,
        station_count: int = 8,
        prefix: str = "CP",
        transformer_kva: Optional[float] = None,
        seed: int = 7,
        connectors: int = 1,
    ) -> None:
        self.base_url = base_url
        if transformer_kva is None:
            transformer_kva = float(el.transformer_for_fleet(station_count).total_kva)
        self.site = SiteModel(transformer_kva=transformer_kva, seed=seed)
        self.stations: Dict[str, StationTwin] = self.site.stations
        for i in range(1, station_count + 1):
            cpid = f"{prefix}-{i:02d}"
            # Stagger initial SoC and start delays for realism
            initial_soc = 15.0 + (i * 4.5) % 35.0  # 15% - 50%
            start_delay = (i - 1) * 0.4            # 0s, 0.4s, 0.8s...
            self.stations[cpid] = StationTwin(cpid, base_url, initial_soc, start_delay, site=self.site,
                                              connectors=connectors)

    def apply_trigger(self, trigger: AttackTrigger) -> ControlAck:
        print(f"[twin] [control] Applying {trigger.attack_type.value} trigger (target: {trigger.station_id})", flush=True)

        if trigger.attack_type == AttackType.METER_SPOOF:
            target_id = trigger.station_id or "CP-03"
            target = self.stations.get(target_id)
            if target:
                target.mode = "meter_spoof"
                return ControlAck(ok=True, detail=f"Station {target_id} mode set to meter_spoof")
            return ControlAck(ok=False, detail=f"Station {target_id} not found")

        elif trigger.attack_type == AttackType.FLEET_OSCILLATE:
            if trigger.station_id:
                target = self.stations.get(trigger.station_id)
                if target:
                    target.mode = "oscillate"
                    return ControlAck(ok=True, detail=f"Station {trigger.station_id} oscillating")
                return ControlAck(ok=False, detail=f"Station {trigger.station_id} not found")
            for s in self.stations.values():
                s.mode = "oscillate"
            return ControlAck(ok=True, detail="Fleet oscillation enabled across all stations")

        elif trigger.attack_type == AttackType.SUBTLE_DRIFT:
            target_id = trigger.station_id or "CP-02"
            target = self.stations.get(target_id)
            if target:
                target.mode = "drift"
                target.drift_ticks = 0
                return ControlAck(ok=True, detail=f"Station {target_id} mode set to subtle_drift")
            return ControlAck(ok=False, detail=f"Station {target_id} not found")

        elif trigger.attack_type == AttackType.TAG_CLONE:
            target_id = trigger.station_id or next(iter(self.stations))
            target = self.stations.get(target_id)
            cloned = trigger.params.get("id_tag")
            if target is None:
                return ControlAck(ok=False, detail=f"Station {target_id} not found")
            if not isinstance(cloned, str) or not cloned:
                return ControlAck(ok=False, detail="tag_clone needs params.id_tag (the card to clone)")
            plug = target.connectors.get(int(trigger.params.get("connector", 1)), target.plug)
            plug.id_tag_override = cloned
            try:
                asyncio.get_running_loop().create_task(target.force_reconnect())
            except RuntimeError:
                pass
            return ControlAck(ok=True, detail=f"Station {target_id} will present cloned card {cloned}")

        elif trigger.attack_type == AttackType.RESET:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            for s in self.stations.values():
                s.mode = "clean"
                s.drift_ticks = 0
                s.is_quarantined = False
                for c in s.connectors.values():
                    c.id_tag_override = None
                # Hand back a battery that can actually charge again. Without
                # this, reset reconnects stations at whatever SoC they had
                # reached -- usually past the 80% knee -- so the fleet lands
                # straight back in the CV phase and `r` looks like a no-op.
                for c in s.connectors.values():
                    c.new_car(c.initial_soc)
                if loop is not None:
                    loop.create_task(s.force_reconnect())
            return ControlAck(ok=True, detail=f"All {len(self.stations)} stations reset to clean mode")

        return ControlAck(ok=False, detail=f"Unhandled attack type: {trigger.attack_type}")

    def get_fleet_summary(self) -> dict:
        """Return an aggregated snapshot of fleet charging states and quarantine status."""
        total_power = sum(s.battery.power_kw for s in self.stations.values())
        avg_soc = (
            sum(s.battery.soc for s in self.stations.values()) / len(self.stations)
            if self.stations
            else 0.0
        )
        quarantined = [s.cpid for s in self.stations.values() if s.is_quarantined]
        modes = {s.cpid: s.mode for s in self.stations.values() if s.mode != "clean"}
        return {
            "total_stations": len(self.stations),
            "total_power_kw": round(total_power, 2),
            "avg_soc": round(avg_soc, 2),
            "quarantined_count": len(quarantined),
            "quarantined_stations": quarantined,
            "active_attacks": modes,
        }

    async def control_handler(self, websocket) -> None:
        """Handle incoming control commands on ws://localhost:9100/control."""
        print("[twin] [control] Client connected to control channel", flush=True)
        try:
            async for message in websocket:
                try:
                    data = json.loads(message)
                    trigger = AttackTrigger.model_validate(data)
                    ack = self.apply_trigger(trigger)
                except Exception as e:
                    ack = ControlAck(ok=False, detail=f"Error processing trigger: {e}")
                await websocket.send(ack.model_dump_json())
        except websockets.exceptions.ConnectionClosed:
            print("[twin] [control] Control client disconnected", flush=True)


async def main() -> None:
    p = argparse.ArgumentParser(description="VoltSentry charger fleet twin (one site)")
    p.add_argument("target_port", nargs="?", type=int, help="legacy: proxy port on localhost")
    p.add_argument("station_count", nargs="?", type=int, help="legacy: number of bays")
    p.add_argument("--target", help="proxy base URL, e.g. ws://localhost:8001 (default ws://localhost:8000)")
    p.add_argument("--stations", type=int, help="number of bays (default 8)")
    p.add_argument("--prefix", default="CP", help="charge point id prefix, e.g. CHN -> CHN-01")
    p.add_argument("--control-host", default="localhost")
    p.add_argument("--control-port", type=int, default=9100)
    p.add_argument("--transformer-kva", type=float, help="site transformer (default: sized for the fleet)")
    p.add_argument("--seed", type=int, default=7, help="grid-frequency walk seed")
    p.add_argument("--connectors", type=int, default=1, help="plugs per charger, sharing its 120 kW cabinet")
    a = p.parse_args()

    base_url = a.target or f"ws://localhost:{a.target_port or 8000}"
    station_count = a.stations or a.station_count or 8
    fleet = FleetManager(base_url, station_count=station_count, prefix=a.prefix, connectors=a.connectors,
                         transformer_kva=a.transformer_kva, seed=a.seed)
    site = fleet.site
    print(f"[twin] Initializing fleet of {station_count} stations ({a.prefix}-01..) targeting {base_url}...", flush=True)
    print(f"[twin] Site: {el.HV_SUPPLY_KV:.0f} kV -> {site.transformer_kva:.0f} kVA transformer -> "
          f"{el.LT_NOMINAL_LL_V:.0f} V 3-phase; bays rated {el.CHARGER_RATED_KW:.0f} kW DC", flush=True)

    control_server = await websockets.serve(fleet.control_handler, a.control_host, a.control_port)
    print(f"[twin] Control channel listening on ws://{a.control_host}:{a.control_port}/control", flush=True)

    tasks = [asyncio.create_task(site.run())]
    tasks += [asyncio.create_task(station.run()) for station in fleet.stations.values()]

    try:
        await asyncio.gather(*tasks)
    finally:
        control_server.close()
        await control_server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
