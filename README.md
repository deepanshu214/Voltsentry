# VoltSentry

**An inline OCPP 1.6-J security proxy for EV charging networks**, plus a simulated
charger fleet, a red-team CLI, and a live NOC dashboard. Localhost-only, zero cloud,
zero paid dependencies — built for the CodeToCreate hackathon (VIT Vellore).

VoltSentry sits transparently on the transport layer between charge points (OCPP
clients) and the Central Management System (CSMS). It relays clean traffic untouched
and runs a dual-tier detection engine over every WebSocket frame, quarantining
compromised stations before they reach the backend or destabilise the grid.

> The binding spec is [`CONTEXT.md`](CONTEXT.md); the shared agent constitution and
> folder-ownership rules are in [`AGENTS.md`](AGENTS.md).

---

## Architecture at a glance

| Process | Command | Listens | Talks to |
| :--- | :--- | :--- | :--- |
| Mock CSMS | `python -m simulator.csms` | `:9000` | — |
| VoltSentry proxy | `python -m proxy.main` | `:8000` ingress, `:8100` feed | CSMS `:9000` |
| Charger fleet twin | `python -m simulator.twin` | control `:9100` | proxy `:8000` |
| Red-team CLI | `python -m cli.attack` | — | twin `:9100` |
| NOC dashboard | `npm run dev` (in `dashboard/`) | `:3000` | proxy `:8100` |

Chargers connect to `ws://localhost:8000/ocpp/{cpid}`; the proxy parses the station id
from the URL path and dials the matching upstream CSMS socket.

---

## Prerequisites

- **Python 3.11+**
- **Node.js 18+** (for the dashboard)

---

## Setup

```bash
# Python side (proxy, simulator, CLI)
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Dashboard (Next.js)
cd dashboard
npm install
```

---

## Run order (matters)

Start **CSMS → proxy → twin**, each in its own terminal — the proxy dials upstream at
handshake, so the CSMS must be up first.

```bash
# T1
python -m simulator.csms          # :9000
# T2
python -m proxy.main              # :8000 ingress, :8100 dashboard feed
# T3
python -m simulator.twin          # fleet -> :8000, control :9100
# T4  (optional — drives the demo attacks)
python -m cli.attack              # red-team TUI
# T5
cd dashboard && npm run dev       # http://localhost:3000
```

---

## Dashboard

- **Landing hero** — procedural 3D WebGL scene (zero media files): a faceted
  icosahedron security shield surrounding an industrial reactor core, 6 EV charging
  nodes with animated energy-flow particles, and red threat particles that flash green
  on shield interception. Cursor parallax, click-to-discharge shockwaves.
- **NOC console** — SCADA-style command center with station grid, live fleet power
  chart, transformer-load gauge, threat timeline, grid frequency widget, and DEFCON
  status badge. Supports dynamic fleet scaling from 4–48 bays with synthetic
  telemetry for stations beyond the live backend (always badged as SIM).
- **3D digital twin** — WebGL charger bays that auto-layout into single or dual rows
  based on fleet size, with camera auto-framing.
- **Network topology** — scalable fleet graph with hub-and-spoke, corridor-mesh, and
  microgrid-ring layouts. CSS-animated power-flow lines and a slide-over drawer for
  per-station telemetry and red-team injection.
- **Login** — adaptive two-column security gate with holographic badge scanner,
  procedural Web Audio cues (scan/grant/deny), clearance ladder, and per-field
  validation.
- **`/globe`** — a 3D interactive globe of real EV charging hubs from
  [OpenChargeMap](https://openchargemap.org). Drag to orbit, scroll to zoom, hover a
  node for its power profile.

### OpenChargeMap API key (for the globe)

The globe works out of the box with a small bundled sample dataset. For the live global
network, add a free key:

1. Get a key at <https://openchargemap.org/site/profile/applications>.
2. In `dashboard/`, copy `.env.local.example` → `.env.local` and set
   `NEXT_PUBLIC_OPENCHARGEMAP_API_KEY=your_key`.
3. Restart `npm run dev`.

---

## Detection engine

**Tier 1 — deterministic rules** (`proxy/rules.py`), pure functions run per frame:

| Rule | Trips when |
| :--- | :--- |
| `R1_STATE_ORDER` | `MeterValues`/`StopTransaction` with no live `Authorize → StartTransaction` |
| `R2_PHYSICS` | `power > 150 kW`, or `> 60 kW` while `SoC > 80%` (CV taper), or negative power |
| `R3_OSCILLATION` | ≥ 6 start/stop transitions fleet-wide in a 10 s window |
| `R4_SESSION_UNIQUE` | a second handshake for a station that already has a live socket |
| `R5_TXN_INTEGRITY` | a `MeterValues.transactionId` that is unknown or owned by another station |
| `R6_FIRMWARE_URI` | `UpdateFirmware` with a non-HTTPS or suspicious URI |

On a forged-data rule (R2/R5) the proxy drops the frame, sends
`ChangeAvailability{Inoperative}` downstream, and closes the socket (`4001`) on the ack.

**Tier 2 — behavioural ML** (`proxy/ml_engine.py`): derives a 5-dimensional session
feature vector `[power_kw, soc, dp_dt, duration_sec, energy_residual_kwh]` and scores it
with an `IsolationForest(n_estimators=100, contamination=0.02, random_state=42)` fitted at
boot against a 1000-vector synthetic CC-CV baseline. Raw `decision_function` output is
mapped through the pinned normalisation in CONTEXT.md §5.B; the proxy alerts past
`ml_score > 0.65`. The `energy_residual` term (`register − ∫ reported_power·dt`) is the
drift tell. The first two samples of a session score `0.0` (dp_dt cold start), so a session
start never false-positives.

Scoring runs in the live proxy: every `TelemetryEvent` carries its `ml_score`, and a
crossing raises a Tier-2 `ThreatEvent` (the yellow ML badge). Verified end-to-end on the
running stack — an honest fleet peaks around `0.30`, and `subtle_drift` climbs past `0.65`
with no Tier-1 rule firing (the pinned seed makes both reproducible).

Measured Tier-1 cost is **~9 µs mean per frame** (median ~8 µs, p99 < 25 µs, over 200k
frames) — no upstream round-trip.

---

## Tests

```bash
pytest -q          # pure-logic tests for rules, ml, ocpp, battery
```

The Python detection logic (`rules.py`, `ml_engine.py`, `ocpp.py`) and the twin battery
maths are test-first; transport is verified by running the stack.

---

## Repository layout

```
proxy/        asyncio relay + detection engine (rules, ml_engine, ocpp, state, pipeline)
simulator/    mock CSMS, cyber-physical charger twin, CC-CV battery model
cli/          red-team attack TUI
dashboard/    Next.js 14 NOC dashboard + story landing + globe
shared/       frozen data contract (schemas.py)
tests/        pytest suites
```

Folder ownership and contribution rules: see [`AGENTS.md`](AGENTS.md).

---

## Tech stack

| Layer | Technology |
| :--- | :--- |
| Proxy & detection | Python 3.11, asyncio, websockets, scikit-learn |
| Dashboard | Next.js 14 (App Router), TypeScript, Tailwind CSS |
| 3D / WebGL | Three.js, @react-three/fiber, @react-three/drei |
| Audio | Web Audio API (procedural synthesis, zero audio files) |
| Protocol | OCPP 1.6-J over WebSocket (JSON) |

---

## Hackathon

Built for **Code2Create** — a 48-hour hackathon by ACM-VIT, Vellore.
500+ idea submissions → 200+ teams shortlisted → top 18 after two review rounds.

**Team:** Deepanshu Agarwal · Shubham Krishna · Krish Jairath
