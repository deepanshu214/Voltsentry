# CONTEXT.md — VoltSentry System Specification (v2, hardened; v3 additions in §10)

> **v2 changelog:** fixes 11 contract gaps found in v1 review. Every change is marked `[FIX-n]`.
> This file is the **binding spec**. The plan argues from it. If plan and spec disagree, spec wins.

---

## 1. Hackathon Core Strategy: Cost & Time Efficiency

VoltSentry is engineered for a **48-hour hackathon build (CodeToCreate, VIT Vellore)**. Every
architectural decision optimizes for **zero financial cost** and **maximum speed to a working demo**.

### Cost Efficiency ($0 Total Spend)
* **Software-Defined Cyber-Physical Twin:** replaces $10,000+ of physical chargers, relays and power
  meters with a discrete-time Python physics loop on `localhost`.
* **Zero paid API dependencies:** local `scikit-learn` instead of cloud anomaly services;
  pre-rendered static `.mp3` clips instead of live TTS calls; JSON-lines files and in-memory state
  instead of a hosted database.
* **100% localhost execution:** no cloud infrastructure.

### Time Efficiency (48-hour execution)
* **Contract-first schema lock:** `shared/schemas.py` and the wire envelope in §6 are **frozen at
  Hour 1**. Three developers build against the contract in parallel, never against each other's
  running code.
* **Dual-tier pragmatism:** Tier-1 deterministic rules cover the loud attacks at zero latency.
  Tier-2 `IsolationForest` fits in <0.2 s at proxy boot — no labelling, no training loop.
* **Fake-data-first frontend:** the dashboard is built against a recorded fixture file
  (`dashboard/fixtures/replay.jsonl`) from Hour 1, so Person 3 is never blocked on the proxy.
  `[FIX-1]`
* **Deterministic demo loop:** the Red-Team CLI drives every attack on a keypress. No live sensors,
  no flaky hardware, no timing races in front of judges.

---

## 2. What Is VoltSentry?

VoltSentry is a software-defined, inline cybersecurity reverse proxy and threat-detection platform
for EV fast-charging networks.

It is a transparent security guard sitting at the transport layer between charging stations
(OCPP clients) and the Central Management System (CSMS server). It intercepts every OCPP 1.6-J
WebSocket frame in real time, validates the payload, enforces session state transitions, and runs a
dual-tier detection engine (deterministic FSM + physics rules, plus an Isolation Forest behavioural
model) to detect and quarantine attacks before they reach the backend or destabilise the grid.

---

## 3. Domain Background

### The problem
Public EV charging runs on OCPP 1.6-J over WebSockets. Chargers are physically exposed, often on
weakly authenticated connections, making them soft cyber-physical edge nodes on critical energy
infrastructure.

* **Cloud CSMS platforms audit after the fact** — batch analysis minutes or hours after ingestion,
  long after an active attack has done its damage.
* **Hardware retrofits are economically dead** — you cannot ship a firewall chip to millions of
  deployed stations.
* **VoltSentry's approach:** pure software at the transport layer, sub-millisecond added latency,
  zero hardware modification, drop-in in front of an existing CSMS.

### Threat vectors mitigated
1. **Meter spoofing / billing fraud** — under-reporting delivered energy so a session bills near
   zero while real power flows.
2. **Coordinated grid oscillation** — synchronised rapid start/stop of high-power chargers across a
   cluster to induce load swings that trip a local distribution transformer.
3. **Session shadowing / hijacking** — parallel WebSocket handshakes reusing a live
   `chargePointId` to displace a legitimate charger or seize its session.
4. **Subtle zero-day data drift** — slow compounding falsification that stays inside every hard
   threshold band, invisible to rule engines.

---

## 4. Architecture

### 4.1 Process & port matrix `[FIX-2]`

| Process | Owner | Listens | Talks to |
| :--- | :--- | :--- | :--- |
| Mock CSMS | P2 | `ws://localhost:9000/ocpp/{cpid}` | — |
| VoltSentry proxy — OCPP ingress | P1 | `ws://localhost:8000/ocpp/{cpid}` | CSMS `:9000` |
| VoltSentry proxy — dashboard fanout | P1 | `ws://localhost:8100/feed` | browser |
| Charger fleet twin | P2 | — (client) | proxy `:8000` |
| Twin control channel | P2 | `ws://localhost:9100/control` | — |
| Federation hub — nodes (v3) | — | `ws://localhost:7700/node` | VoltSentry proxies dial in |
| Federation hub — dashboard (v3) | — | `ws://localhost:7701/network`, `/healthz` | browser |
| NOC dashboard | P3 | `http://localhost:3000` | proxy `:8100` |

**`[FIX-2]` — dashboard transport.** v1 specified Socket.io on the frontend against a raw
`websockets` Python server. Those protocols are not compatible. The dashboard feed is now a **plain
WebSocket** on a **separate port (8100)**, consumed by the browser's native `WebSocket` API. No
Socket.io, no `python-socketio`, no path-routing bugs on 8000.

**`[FIX-3]` — station identity.** v1 never said how the proxy knows which station a socket belongs
to. Per OCPP 1.6-J the charge point ID lives in the **URL path**: a client connects to
`ws://localhost:8000/ocpp/CP-04`. The proxy parses `cpid` from the path at handshake, before any
frame arrives, and opens the matching upstream socket to `ws://localhost:9000/ocpp/CP-04`.

### 4.2 Data flow

```
twin (CP-01..CP-08) ──ws:8000──▶ PROXY ──ws:9000──▶ mock CSMS
                                   │
                        rules.py + ml_engine.py
                                   │
                                   ├── ws:8100 ──▶ Next.js dashboard
                                   └── logs/incidents.jsonl

attack.py ──ws:9100──▶ twin control channel  [FIX-4]
```

**`[FIX-4]` — the attack CLI cannot MITM a socket it does not own.** This was v1's most serious
hole. `attack.py` is a separate process; it has no access to the twin↔proxy socket to "rewrite
outgoing meter values." Corrected design: **`attack.py` is a remote control for the twin.** It sends
an `AttackTrigger` over the twin's control channel (`:9100`); the twin flips the named station into
a compromised behaviour mode and starts emitting malicious frames itself. The proxy sees genuinely
hostile traffic on the wire. Same demo, ~3 hours cheaper, and architecturally honest.

`session_shadow` is the exception — it opens its own second socket directly to `:8000` with an
already-active `chargePointId`, which is exactly the real attack.

---

## 5. Feature Set

### A. Inline transport proxy (`/proxy`)
* **Transparent reverse proxy** — accepts on `:8000`, forwards clean traffic to `:9000` unmodified.
  Bidirectional relay: CSMS→charger frames pass through untouched.
* **OCPP 1.6-J frame handling `[FIX-5]`** — the wire format is a JSON **array**, not an object:
  * `CALL`: `[2, "<uniqueId>", "<Action>", {payload}]`
  * `CALLRESULT`: `[3, "<uniqueId>", {payload}]`
  * `CALLERROR`: `[4, "<uniqueId>", "<code>", "<desc>", {details}]`
  A `CALLRESULT` **does not carry the action name**. The proxy keeps a
  `pending: dict[uniqueId -> action]` map per connection so it can correlate
  `StartTransaction` → its returned `transactionId`. Without this map the session FSM cannot work.
* **Active quarantine `[FIX-6]`** — on a Tier-1 block the proxy (a) drops the offending frame,
  (b) sends `[2, "<uuid>", "ChangeAvailability", {"connectorId": 1, "type": "Inoperative"}]` **to the
  charger** (ChangeAvailability is a CSMS→CP call, so the proxy speaks it downstream), (c) waits for
  the twin's `CALLRESULT`, (d) closes the socket with code `4001`. The twin **must** handle inbound
  CALLs and reply — v1 never specified this and the quarantine would have hung.
* **Un-quarantine `[FIX-7]`** — the demo fires four attacks back to back. A severed station has no
  target for attack #2. `attack.py reset` clears proxy quarantine state and tells the twin to
  reconnect all stations to clean mode. Non-negotiable for the live demo.
* **Forensic logging** — one JSON object per line to `logs/incidents.jsonl`.

### B. Dual-tier detection engine

**Tier-1 — deterministic rules (`proxy/rules.py`)**

| Rule | Check | Trip condition |
| :--- | :--- | :--- |
| `R1_STATE_ORDER` | FSM per `cpid` | `MeterValues`/`StopTransaction` with no live `Authorize`→`StartTransaction` |
| `R2_PHYSICS` | CC-CV envelope | `power_kw > 150.0`, or `power_kw > 60.0` while `soc > 80.0` (CV taper), or `power_kw < 0` |
| `R3_OSCILLATION` | sliding window | ≥ **6** Start/Stop transitions fleet-wide in a **10 s** window `[FIX-8]` |
| `R4_SESSION_UNIQUE` | connection registry | second handshake for a `cpid` with a live session |
| `R5_TXN_INTEGRITY` | txn map | `MeterValues.transactionId` unknown or belongs to another `cpid` |
| `R6_METER_FRAUD` | energy residual | residual ≥ 0.25 kWh **and** climbing ≥ 0.025 kWh/s |
| `R7_ELECTRICAL` (v3) | cross-measurand | `abs(V × I − P) > max(2 kW, 10 %)` on the DC outlet readings |
| `R8_TAG_CLONE` (v3) | card registry | `idTag` already in a live session on another bay, here or at another site |
| `R9_THREAT_INTEL` (v3) | federation | card or charge point on another site's indicator list |

`[FIX-8]` — v1's `>0.5 Hz` was untestable: a frequency needs a window, and "fleet-wide" contradicted
a per-station attack. Restated as a countable predicate over a named window so it can be unit-tested
with a fake clock.

**Tier-2 — behavioural ML (`proxy/ml_engine.py`)**

* **Model:** `IsolationForest(n_estimators=100, contamination=0.02, random_state=42)`.
* **Feature vector (5-dim) `[FIX-9]`:**
  `[power_kw, soc, dp_dt, duration_sec, energy_residual_kwh]`
* **`[FIX-9]` — v1's 4-feature vector could not detect `subtle_drift`, its own headline attack.**
  A slow drift that stays inside the normal band is by construction *not* a point outlier; the model
  would have stayed silent in front of the judges. Fixed by adding a session-cumulative feature the
  proxy can compute from the wire alone:
  `energy_residual_kwh = reported_Energy.Active.Import.Register_kWh − ∫ reported_power_kw dt`
  Honest sessions hold this near zero (baseline: `N(0, 0.05)`; v3 fleet model: `N(0, 0.015)`, see
  below). Any drift between the reported
  instantaneous power and the accumulating energy register makes it climb monotonically, and the
  forest fires. This is the single highest-risk fix in v2.
* **`dp_dt` cold start `[FIX-10]`** — undefined on the first `MeterValues` of a session. Contract:
  `dp_dt = 0.0` for sample 1, and **samples 1–2 of a session are not scored** (`ml_score = 0.0`).
  Without this every session start throws a false positive on camera.
* **Score normalisation `[FIX-11]`** — `decision_function` returns *higher = more normal* on an
  unbounded raw scale, so "0.65" was meaningless across processes. Pinned at fit time:

  ```
  d      = clf.decision_function(X_train)
  d_max  = d.max()
  d_min  = percentile(d, 0.5)
  score(x) = clip((d_max - clf.decision_function(x)) / (d_max - d_min), 0.0, 1.0)
  ```
  Deterministic given `random_state=42`. **Alert threshold: `ml_score > 0.65`.**

* **v3 — fleet baseline and calibrated anchor.** The shipped model (`python -m proxy.train_tier2
  --source twin`) is fitted on `ml_engine.fleet_baseline()`: 6000 honest vectors on the fleet's own
  curve (`shared/charging.py`: CC to 78 %, hand-off to 58 kW by 80 %, CV taper), with extra weight on
  the hand-off and the top 92-100 %, session age 0-9000 s and residual `N(0, 0.015)` kWh (the twin's
  register is sent to 10 Wh). The score formula above is unchanged; only the low anchor moves:
  `d_min` is set so the most unusual sample in 30 held-out full honest sessions scores **0.5**
  (`ml_engine.calibrate`). With the 0.5th-percentile anchor the rarest few percent of honest
  training samples scored above 0.65 by construction, and a charger reporting once a second always
  reaches them in a long session: every honest bay false-alarmed once its car passed 80 %.
  Benchmark and limits: `proxy/tier2_report.md`.

### C. Cyber-physical twin (`/simulator`)
* **CC-CV battery engine** — discrete-time loop, 1 Hz tick (v3: the register counts the real time
  between ticks, ~1.03 s, as a real meter does): constant-current to 80% SoC, then
  exponential taper to 100%. Tracks pack voltage, current, SoC, cumulative kWh (v3: 192-cell 800 V-class pack; V and I
  derived from the power curve so P = V × I, see §10.1).
* **Fleet** — 8 stations `CP-01`..`CP-08` by default, staggered session starts.
* **Compromise modes** — each station holds a `mode` flag driven by the control channel:
  `clean | meter_spoof | oscillate | drift`.
* **Inbound CALL handling `[FIX-6]`** — the twin answers `ChangeAvailability` and `Reset` with a
  `CALLRESULT` and applies the state change.
* **Transformer monitor** — shared site transformer (v3: 1250 kVA for 8 × 120 kW, measured in kVA;
  the hackathon build's 500 kVA could not feed eight fast chargers, see §10.1); tracks aggregate load and headroom, emitted
  on the dashboard feed as its own event `[FIX-12]` (v1 computed it in the twin where the dashboard
  could never see it).

### D. Red-Team CLI (`/cli/attack.py`)
Single-keypress exploits over the twin control channel:

| Key | Attack | Effect | Expected detection |
| :--- | :--- | :--- | :--- |
| `1` | `meter_spoof` | station reports 5 kW while drawing 120 kW | R7 (first frame) + R6 + ML |
| `2` | `fleet_oscillate` | every station stops and starts together, once a second (v3: phased on the wall clock, full power while on) | R3 |
| `3` | `session_shadow` | duplicate handshake on a live `cpid` | R4 |
| `4` | `subtle_drift` | −2% compounding power under-report per tick | ML first (score past 0.65 in ~10 s), then R6 (~44 s) |
| `5` | `tag_clone` (v3) | a bay presents a card already charging elsewhere | R8 at that site, R9 at the card's site |
| `r` | `reset` | clear quarantine, all stations back to clean | — |

### E. NOC dashboard (`/dashboard`)
* Next.js 14 (app router), Tailwind, Recharts, **native `WebSocket`** — no Socket.io.
* Station status grid, live power chart, SoC bars, transformer load gauge.
* Threat badges: 🔴 `RULE VIOLATION` (Tier-1, with `rule_id`), 🟡 `ML ANOMALY 0.84` (Tier-2).
* Pre-rendered `.mp3` alert on the first high-severity event (rate-limited to one per 10 s, and
  gated behind a user click so browser autoplay policy doesn't silently block it during the demo).
* **Reconnect with backoff** — the socket *will* drop when a laptop sleeps or the proxy restarts.
  Auto-reconnect every 2 s. `[FIX-13]`
* One-click JSON export of the captured event log.

---

## 6. Frozen Contract

### 6.1 `shared/schemas.py`

```python
"""VoltSentry shared data contract. FROZEN AT HOUR 1.
Any change requires all three owners to agree in person."""

from enum import Enum
from typing import Literal, Optional
from pydantic import BaseModel, Field


class StationStatus(str, Enum):
    AVAILABLE   = "Available"
    PREPARING   = "Preparing"
    CHARGING    = "Charging"
    FINISHING   = "Finishing"
    QUARANTINED = "Quarantined"
    OFFLINE     = "Offline"


class AttackType(str, Enum):
    METER_SPOOF    = "meter_spoof"
    FLEET_OSCILLATE = "fleet_oscillate"
    SESSION_SHADOW = "session_shadow"
    SUBTLE_DRIFT   = "subtle_drift"
    RESET          = "reset"


# ---------- dashboard feed events (ws://localhost:8100/feed) ----------
# Every frame on the feed is one of these three, discriminated by `event`.

class TelemetryEvent(BaseModel):
    event: Literal["telemetry"] = "telemetry"
    station_id: str                      # "CP-04"  (== OCPP chargePointId)
    transaction_id: Optional[int] = None
    ts: float                            # unix epoch seconds, float
    power_kw: float
    soc: float = Field(ge=0.0, le=100.0)
    dp_dt: float                         # kW per second, 0.0 on first sample
    duration_sec: float
    energy_register_kwh: float           # meter's cumulative register
    energy_residual_kwh: float           # register - integral(power)
    status: StationStatus
    ml_score: float = Field(default=0.0, ge=0.0, le=1.0)


class ThreatEvent(BaseModel):
    event: Literal["threat"] = "threat"
    station_id: str
    ts: float
    tier: Literal[1, 2]
    rule_id: Optional[str] = None        # "R2_PHYSICS" for tier 1
    ml_score: Optional[float] = None     # for tier 2
    severity: Literal["low", "medium", "high"]
    reason: str                          # human-readable, shown on the badge
    action_taken: Literal["logged", "frame_dropped", "quarantined"]
    raw_frame: Optional[str] = None      # forensic copy of the offending frame


class GridEvent(BaseModel):
    event: Literal["grid"] = "grid"
    ts: float
    total_load_kw: float
    transformer_capacity_kva: float = 500.0
    headroom_pct: float
    active_stations: int


# ---------- control channel (ws://localhost:9100/control) ----------

class AttackTrigger(BaseModel):
    attack_type: AttackType
    station_id: Optional[str] = None     # None => fleet-wide
    params: dict = Field(default_factory=dict)


class ControlAck(BaseModel):
    ok: bool
    detail: str = ""
```

### 6.2 OCPP payload shapes the proxy must parse `[FIX-14]`

v1 never pinned the `MeterValues` shape, which is deeply nested and the #1 place two developers
silently disagree. Frozen:

```json
[2, "a3f1", "MeterValues", {
  "connectorId": 1,
  "transactionId": 1041,
  "meterValue": [{
    "timestamp": "2026-09-06T15:04:05Z",
    "sampledValue": [
      {"value": "120.4", "measurand": "Power.Active.Import",         "unit": "kW"},
      {"value": "18.72", "measurand": "Energy.Active.Import.Register","unit": "kWh"},
      {"value": "64.0",  "measurand": "SoC",                          "unit": "Percent"}
    ]
  }]
}]
```

Extraction contract: `power_kw` from `measurand == "Power.Active.Import"` **in kW**,
`energy_register_kwh` from `Energy.Active.Import.Register` **in kWh**, `soc` from `SoC`.
Values are **strings** on the wire (OCPP requirement) and cast float-side by the proxy.
Timestamps on the OCPP wire are ISO-8601 `Z` strings; the `ts` float in §6.1 is the **proxy's**
receive clock, not the charger's claim. `[FIX-15]`

**v3 — units and location are read, not assumed.** Power and energy are normalised from `W`/`Wh`
or `kW`/`kWh`; OCPP defaults apply (measurand `Energy.Active.Import.Register`, unit `Wh`,
location `Outlet`); `SignedData` and non-numeric values are skipped. `power_kw` is the **Outlet**
(DC) reading. The twin also reports `Voltage` and `Current.Import` at the Outlet, and
`Power.Active.Import`, per-phase `Voltage` (`L1-L2`, `L2-L3`, `L3-L1`) and `Frequency` at the Inlet.

---

## 7. Repository & Ownership

```text
voltsentry/
├── CONTEXT.md                    # this file — spec, frozen
├── requirements.txt              # pinned: websockets, pydantic, scikit-learn, numpy, rich, click
├── shared/
│   ├── schemas.py                # hour-1 contract; since then optional fields only
│   └── electrical.py             # v3: site supply model (kV, kVA, V, A, Hz)
├── federation/                   # v3: hub, node client, signed protocol, intel store, keys
├── proxy/                        # PERSON 1
│   ├── main.py                   # asyncio relay :8000 -> :9000, fanout :8100
│   ├── ocpp.py                   # frame parse/serialise, pending-call map
│   ├── rules.py                  # R1..R9
│   ├── ml_engine.py              # IsolationForest + pinned normalisation
│   └── state.py                  # per-station FSM, txn registry, quarantine set
├── simulator/                    # PERSON 2
│   ├── battery.py                # pure CC-CV maths, no I/O — unit-testable
│   ├── twin.py                   # fleet client + compromise modes + control :9100
│   └── csms.py                   # mock CSMS :9000
├── cli/                          # PERSON 2
│   └── attack.py                 # rich/click TUI
├── dashboard/                    # PERSON 3
│   ├── app/
│   ├── lib/useFeed.ts            # WS client + reconnect
│   ├── fixtures/replay.jsonl     # recorded feed, unblocks P3 from hour 1
│   └── public/audio/alert.mp3
├── tests/
│   ├── test_rules.py
│   ├── test_ml.py
│   └── test_battery.py
└── logs/incidents.jsonl
```

---

## 8. Run Order

```bash
pip install -r requirements.txt

# T1
python -m simulator.csms          # :9000
# T2
python -m proxy.main              # :8000 ingress, :8100 feed
# T3
python -m simulator.twin          # fleet -> :8000, control :9100
# T4
python -m cli.attack              # TUI
# T5
cd dashboard && npm run dev       # :3000
```

Start order matters: CSMS → proxy → twin. The proxy dials upstream at handshake; if the CSMS is
down, every charger connection fails at accept.

---

## 9. Demo Script (4 minutes, rehearsed)

| t | Action | What the judge sees |
| :--- | :--- | :--- |
| 0:00 | dashboard on screen, fleet charging | 8 green stations, CC-CV curves rising, transformer at ~62% |
| 0:30 | one line on why OCPP is exposed | — |
| 1:00 | press `1` — meter_spoof | CP-03 goes red, `R7_ELECTRICAL` (5 kW reported vs V × I ≈ 120 kW), frame dropped, station quarantined, audio alert |
| 1:40 | press `r`, then `2` — oscillate | transformer load swings between ~82 % and 0 % every second, `R3_OSCILLATION` fires fleet-wide |
| 2:20 | press `r`, then `3` — session_shadow | shadow socket rejected at handshake, `R4_SESSION_UNIQUE` |
| 3:00 | press `r`, then `4` — subtle_drift | **no rule fires at first**; ML score passes 0.65 ~10 s in (measured), yellow badge; R6 quarantines ~35 s later |
| 3:40 | export forensic log | JSON downloads |

The `subtle_drift` beat is the pitch. It is the one attack a rule engine cannot catch, and it is why
Tier-2 exists. Rehearse it twice before judging.

---

## 10. v3 — post-hackathon: real units and a multi-site network

### 10.1 Electrical model (`shared/electrical.py`)

```
11 kV feeder -> 11 kV / 433 V transformer -> 415 V 3-phase bus (240 V L-N) -> 120 kW DC bays
```

* Bay draw at full output: 120 / 0.95 + 0.15 = 126.5 kW = 127.7 kVA at PF 0.99.
* 8 bays: 1022 kVA -> **1250 kVA** transformer (smallest standard rating with no overload).
* Bus voltage sags with loading: `433 × (1 − 0.035 × loading)` -> ~421 V at 82 %.
* Line current `I = S / (√3 × V)`; frequency is the grid's, a mean-reverting walk in 49.90–50.05 Hz.
* `GridEvent.total_load_kw` is the supply-side real power; `headroom_pct` is against the kVA rating.
  Optional fields carry `output_kw`, `total_load_kva`, `loading_pct`, `lt_voltage_v`, `lt_current_a`,
  `frequency_hz`. `TelemetryEvent` adds `dc_voltage_v`, `dc_current_a`, `ac_voltage_v`, `frequency_hz`.

### 10.2 Federation (`federation/`)

* One VoltSentry node per site; each dials **out** to the hub (`--hub-url`).
* Envelope `{v, node, seq, ts, kind, body, sig}`; `sig` = HMAC-SHA256 with the node's key
  `HMAC(master, "voltsentry-node:" + node_id)`. Rising `seq`, ±120 s `ts` window.
* Node -> hub: `hello`, `heartbeat`, `threat`, `ioc`, `session`. Hub -> node: `welcome`, `roster`,
  `threat`, `ioc`, `session`, `session_reset`.
* Cards travel as `HMAC(tag_salt, idTag)`; the salt comes in `welcome`.
* Card checks read the node's local replica, so detection never waits on the hub.

### 10.3 Enforcement mode (`--mode enforce | monitor`)

* `enforce` (default): quarantine, `Blocked` replies and handshake refusals as specified above.
* `monitor`: every rule and Tier-2 run; `ThreatEvent.action_taken` is `logged` and
  `recommended_action` carries what enforce would have done. No frame is dropped or answered,
  no charger quarantined or refused (except R4's duplicate-connection refusal), and the node
  publishes no IoCs. `GridEvent.enforcement` and the heartbeat carry the mode.

### 10.4 Defence counters (`GridEvent.defences`, optional)

* What the node has done since it started: frames inspected / forwarded / held back from the CSMS,
  quarantines, cards answered `Blocked`, and handshakes refused (duplicate session R4 4001,
  blocklist R9 4003, oversize frame 1009, no `/ocpp/{cpid}` 1008); threats recorded.
* How it is set up: ingress TLS, bind address, frame limit (64 KB), federation state, dev key.
* The dashboard's **Defences** tab draws them: chargers -> site network firewall -> VoltSentry
  (the application firewall) -> CSMS, and one card per safeguard.
