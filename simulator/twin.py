import argparse
import asyncio
import json
import random
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


class StationTwin:
    def __init__(
        self,
        cpid: str,
        base_url: str,
        initial_soc: float = 20.0,
        start_delay: float = 0.0,
        site: Optional[SiteModel] = None,
    ) -> None:
        self.cpid = cpid
        self.site = site
        # RFID card presented at Authorize. tag_clone swaps in someone else's.
        self.id_tag_override: Optional[str] = None
        # What the bay really draws (kW DC), whatever it reports. Feeds the site model.
        self.actual_power_kw = 0.0
        # Small fixed phase imbalance, distinct per bay, as a real supply has.
        crc = zlib.crc32(cpid.encode())
        self.phase_offsets_v = tuple(((crc >> (8 * k)) % 13 - 6) * 0.1 for k in range(3))
        self.target_url = f"{base_url}/ocpp/{cpid}"
        self.initial_soc = initial_soc
        self.start_delay = start_delay
        self.mode = "clean"
        self.drift_ticks = 0
        self.is_quarantined = False
        self.battery = Battery(initial_soc=initial_soc, max_power_kw=120.0)
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.transaction_id: Optional[int] = None
        self.pending_responses: Dict[str, asyncio.Future] = {}
        self.prefix = f"[twin] [{self.cpid}]"
        self.running = True
        self.receiver_task: Optional[asyncio.Task] = None

    @property
    def id_tag(self) -> str:
        return self.id_tag_override or f"TAG-{self.cpid}"

    def meter_payload(self, actual_power: float, soc: float, energy_kwh: float) -> tuple[dict, float]:
        """Build one MeterValues payload; returns (payload, reported_power_kw).

        Outlet (DC, to the car): power, energy register, voltage, current.
        Inlet (AC, from the site bus): power drawn, the three line-to-line
        voltages, frequency. SoC is the EV's own reading.

        The two meter attacks differ in care. meter_spoof rewrites only the
        power figure, so V x I contradicts it (R7). drift scales power,
        current and supply power together, so every reading agrees with every
        other one except the energy register, which only the ML residual sees.
        """
        dc_v, dc_a = self.battery.voltage_v, self.battery.current_a
        reported_power = actual_power
        supply_kw = el.ac_input_kw(actual_power)
        if self.mode == "meter_spoof":
            reported_power = 5.0
        elif self.mode == "drift":
            self.drift_ticks += 1
            factor = 0.98 ** self.drift_ticks
            reported_power = actual_power * factor
            dc_a = dc_a * factor
            supply_kw = el.ac_input_kw(reported_power)

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
            "connectorId": 1,
            "transactionId": self.transaction_id,
            "meterValue": [{"timestamp": ts, "sampledValue": sampled}],
        }
        return payload, reported_power

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
                self.actual_power_kw = 0.0  # an Inoperative charger stops delivering at once
                print(f"{self.prefix} Station quarantined (Inoperative)", flush=True)
            else:
                self.is_quarantined = False
                print(f"{self.prefix} Station restored (Operative)", flush=True)
            res = [3, msg_id, {"status": "Accepted"}]
            await self.ws.send(json.dumps(res))
        elif action == "Reset":
            print(f"{self.prefix} Station Reset received", flush=True)
            self.mode = "clean"
            self.drift_ticks = 0
            self.is_quarantined = False
            self.id_tag_override = None
            self.battery = Battery(initial_soc=self.initial_soc, max_power_kw=120.0)
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

    async def run(self) -> None:
        """Main lifecycle loop for this charger station."""
        if self.start_delay > 0:
            await asyncio.sleep(self.start_delay)

        while self.running:
            try:
                print(f"{self.prefix} Connecting to {self.target_url}...", flush=True)
                async with websockets.connect(self.target_url) as ws:
                    self.ws = ws
                    self.receiver_task = asyncio.create_task(self._receiver_loop())

                    # 1. BootNotification
                    boot_res = await self.call(
                        "BootNotification",
                        {"chargePointVendor": "VoltSentry", "chargePointModel": "Twin-v1"},
                    )
                    print(f"{self.prefix} BootNotification accepted", flush=True)

                    # 2. Authorize. A real charger only starts on an Accepted card.
                    while True:
                        auth_res = await self.call("Authorize", {"idTag": self.id_tag})
                        auth_status = (auth_res.get("idTagInfo") or {}).get("status")
                        if auth_status == "Accepted":
                            break
                        print(f"{self.prefix} Card {self.id_tag} refused ({auth_status}); not charging", flush=True)
                        await asyncio.sleep(AUTH_RETRY_SEC)
                    print(f"{self.prefix} Authorized ({self.id_tag})", flush=True)

                    # 3. StartTransaction
                    start_res = await self.call(
                        "StartTransaction",
                        {
                            "connectorId": 1,
                            "idTag": self.id_tag,
                            "meterStart": 0,
                            "timestamp": iso_now(),
                        },
                    )
                    self.transaction_id = start_res.get("transactionId", random.randint(1000, 9999))
                    print(f"{self.prefix} Transaction started (id: {self.transaction_id})", flush=True)

                    step = 0
                    while self.running:
                        if self.is_quarantined:
                            self.actual_power_kw = 0.0
                            await asyncio.sleep(1.0)
                            continue

                        step += 1

                        if self.mode == "oscillate":
                            self.actual_power_kw = 0.0
                            # Rapid Start/Stop cycle at 1 Hz
                            print(f"{self.prefix} [OSCILLATE] Rapid StopTransaction", flush=True)
                            await self.call(
                                "StopTransaction",
                                {
                                    "transactionId": self.transaction_id,
                                    "meterStop": int(self.battery.energy_kwh * 1000),
                                    "timestamp": iso_now(),
                                    "idTag": self.id_tag,
                                },
                            )
                            await asyncio.sleep(0.5)
                            print(f"{self.prefix} [OSCILLATE] Rapid StartTransaction", flush=True)
                            start_res = await self.call(
                                "StartTransaction",
                                {
                                    "connectorId": 1,
                                    "idTag": self.id_tag,
                                    "meterStart": int(self.battery.energy_kwh * 1000),
                                    "timestamp": iso_now(),
                                },
                            )
                            self.transaction_id = start_res.get("transactionId", self.transaction_id + 1)
                            await asyncio.sleep(0.5)
                            continue

                        # Normal CC-CV physics tick
                        actual_power, soc, energy_kwh = self.battery.tick(dt=1.0)
                        self.actual_power_kw = actual_power
                        meter_payload, reported_power = self.meter_payload(actual_power, soc, energy_kwh)

                        await self.call("MeterValues", meter_payload)

                        mode_tag = f" [{self.mode.upper()}]" if self.mode != "clean" else ""
                        print(
                            f"{self.prefix}{mode_tag} Pwr: {reported_power:.1f} kW (actual: {actual_power:.1f}) "
                            f"| {self.battery.voltage_v:.0f} V {self.battery.current_a:.0f} A "
                            f"| SoC: {soc:.1f}% | Energy: {energy_kwh:.2f} kWh",
                            flush=True,
                        )

                        if soc >= 100.0:
                            print(f"{self.prefix} Battery full (100%). Stopping transaction.", flush=True)
                            await self.call(
                                "StopTransaction",
                                {
                                    "transactionId": self.transaction_id,
                                    "meterStop": int(energy_kwh * 1000),
                                    "timestamp": iso_now(),
                                    "idTag": self.id_tag,
                                },
                            )
                            self.actual_power_kw = 0.0
                            # Reset battery for next car after brief pause
                            await asyncio.sleep(5.0)
                            self.battery = Battery(initial_soc=15.0, max_power_kw=120.0)
                            break

                        await asyncio.sleep(1.0)

            except (websockets.exceptions.ConnectionClosed, ConnectionRefusedError, OSError) as e:
                self.actual_power_kw = 0.0  # no socket, no session, no power
                if self.is_quarantined:
                    print(f"{self.prefix} Quarantined: waiting for reset signal before reconnecting...", flush=True)
                    while self.is_quarantined and self.running:
                        await asyncio.sleep(1.0)
                    print(f"{self.prefix} Quarantine cleared! Reconnecting to {self.target_url}...", flush=True)
                else:
                    print(f"{self.prefix} Connection lost ({e}), reconnecting in 2s...", flush=True)
                    await asyncio.sleep(2.0)
            except Exception as e:
                self.actual_power_kw = 0.0
                print(f"{self.prefix} Error: {e}, restarting session in 2s...", flush=True)
                await asyncio.sleep(2.0)
            finally:
                self.actual_power_kw = 0.0
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
            self.stations[cpid] = StationTwin(cpid, base_url, initial_soc, start_delay, site=self.site)

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
            target.id_tag_override = cloned
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
                s.id_tag_override = None
                # Hand back a battery that can actually charge again. Without
                # this, reset reconnects stations at whatever SoC they had
                # reached -- usually past the 80% knee -- so the fleet lands
                # straight back in the CV phase and `r` looks like a no-op.
                s.battery = Battery(initial_soc=s.initial_soc, max_power_kw=120.0)
                if loop is not None:
                    loop.create_task(s.force_reconnect())
            return ControlAck(ok=True, detail="All stations reset to clean mode")

        return ControlAck(ok=False, detail=f"Unhandled attack type: {trigger.attack_type}")

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
    a = p.parse_args()

    base_url = a.target or f"ws://localhost:{a.target_port or 8000}"
    station_count = a.stations or a.station_count or 8
    fleet = FleetManager(base_url, station_count=station_count, prefix=a.prefix,
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
