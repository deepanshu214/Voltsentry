# VoltSentry

**An inline OCPP 1.6-J security proxy for EV charging networks**, plus a simulated
charger fleet, a red-team CLI, a multi-site federation hub and a live NOC dashboard.
Runs on one laptop as a demo, or as one VoltSentry node per charging site joined into
a network. No cloud services and no paid dependencies.

VoltSentry sits transparently on the transport layer between charge points (OCPP
clients) and the Central Management System (CSMS). It relays clean traffic untouched,
runs a dual-tier detection engine over every WebSocket frame, and quarantines
compromised stations before they reach the backend or destabilise the site's supply.

> The binding spec is [`CONTEXT.md`](CONTEXT.md); folder ownership and working rules
> are in [`AGENTS.md`](AGENTS.md).

---

## Units and the electrical supply chain

Every electrical figure in VoltSentry is in its real unit, and the unit is in the field
name (`_kw`, `_kva`, `_kwh`, `_v`, `_a`, `_hz`). One model, in
[`shared/electrical.py`](shared/electrical.py), is used by both the simulator and the
proxy, so they cannot disagree.

```
11 kV HT feeder ──► 11 kV / 433 V transformer ──► 415 V three-phase LT bus ──► 120 kW DC chargers ──► car
                    rated in kVA                   415 V line-to-line           AC in, DC out (η 95 %)
                                                   = 240 V line-to-neutral
```

- **Why not 220/230 V?** A house takes one phase: about 230 V between that phase and
  neutral. A 120 kW charger needs all three phases. On a 415 V three-phase bus each
  phase to neutral is 415 / √3 ≈ 240 V, the same order as a home supply. The dashboard
  shows the measured bus voltage both ways (e.g. *420.7 V L-L · 243 V L-N*).
- **What is the kVA number?** It is the transformer's rating: apparent power, not a
  voltage. kVA = kW ÷ power factor. Windings heat with current, whatever the phase
  angle, so transformers are rated in kVA.
- **Sizing.** One bay at full output draws 120 kW ÷ 0.95 + 0.15 kW standby = 126.5 kW
  from the bus, which is 127.7 kVA at PF 0.99. Eight bays need 1022 kVA, so the site
  uses a **1250 kVA** transformer, the next standard rating. (The hackathon build
  assumed 500 kVA, which eight fast chargers would have overloaded by 2×.) The
  six-bay demo site gets 1000 kVA.
- **Line current** on the bus: I = S ÷ (√3 × V). For 1022 kVA at the measured 420.7 V
  that is 1403 A per phase.
- **Battery side.** The simulated cars have an 800 V-class pack (192 NMC cells). At
  120 kW and ~700 V the cable carries ~170 A, inside the 200 A limit of an uncooled
  CCS2 cable. The same power into a 400 V pack would need 300 A.
- **Frequency** belongs to the whole national grid, not to one site. The chargers
  report it at their inlet. In the simulator it is a mean-reverting walk inside the
  Indian operating band (49.90–50.05 Hz), independent of the site's load.

The proxy reads units off the wire. A charger reporting `120000 W` / `18720 Wh` is
read as 120 kW / 18.72 kWh, OCPP's defaults are honoured (measurand
`Energy.Active.Import.Register`, unit `Wh`, location `Outlet`), and signed or
non-numeric readings are skipped instead of tearing down the connection.

---

## Architecture

### One site

| Process | Command | Listens | Talks to |
| :--- | :--- | :--- | :--- |
| Mock CSMS | `python -m simulator.csms` | `:9000` | — |
| VoltSentry proxy | `python -m proxy.main` | `:8000` ingress, `:8100` feed + `/healthz` | CSMS `:9000` |
| Charger fleet twin | `python -m simulator.twin` | control `:9100` | proxy `:8000` |
| Red-team CLI | `python -m cli.attack` | — | twin `:9100` |
| NOC dashboard | `npm run dev` (in `dashboard/`) | `:3000` | proxy `:8100`, hub `:7701` |

Chargers connect to `ws://HOST:8000/ocpp/{cpid}`; the proxy takes the station id from
the URL path and dials the matching upstream CSMS socket.

### A network of sites

```
                      ┌──────────────── central CSMS :9000 ─────────────────┐
                      │                                                      │
   Vellore site       │                         Chennai site                 │
   8 x 120 kW ─► VoltSentry node VS-VELLORE      6 x 120 kW ─► VoltSentry node VS-CHENNAI
                 :8000 / feed :8100                            :8001 / feed :8101
                        │  outbound, signed                           │
                        └──────────►  federation hub  ◄───────────────┘
                                      :7700 nodes, :7701 dashboard feed
```

Each site runs its own VoltSentry node next to its chargers; every node dials **out**
to the hub, so a site behind NAT needs no open inbound port. The hub never carries
charger traffic: it authenticates nodes, relays threat intelligence, and correlates
card sessions across sites.

---

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cd dashboard && npm install && cd ..
```

**Two-site network, one command** (CSMS, hub, two VoltSentry nodes, two fleets):

```bash
python scripts/run_network.py          # concise console; full logs in logs/<process>.log
# or start Chennai as a new site that only reports: python scripts/run_network.py --monitor chennai
cd dashboard && npm run dev            # http://localhost:3000 -> NOC -> Multi-Site Network
```

**Single site, one process per terminal** (start CSMS, then the proxy, then the twin;
the proxy dials upstream at handshake):

```bash
python -m simulator.csms
python -m proxy.main
python -m simulator.twin
python -m cli.attack                   # red-team TUI
cd dashboard && npm run dev
```

The dashboard opens in **Demo Replay** mode, which plays a 125 s recording of the real
stack (normal charging, a meter spoof, a drift attack). Switch to **Live Charger
Stream** to read the running proxy.

---

## Attacks and what catches them

Timings measured on the running stack (the bundled replay is that recording).

| CLI | Attack | Detection |
| :--- | :--- | :--- |
| `-a 1 -s CP-03` | **Meter spoof**: reports 5 kW while drawing ~120 kW | `R7_ELECTRICAL` on the first spoofed frame (*reported 5.0 kW but 718 V × 167 A = 120.0 kW*), quarantined |
| `-a 2` | **Fleet oscillation**: every bay stops and starts together once a second, so the transformer swings between 82 % load and none | `R3_OSCILLATION` at ~1.5 s, one fleet-wide alert |
| `-a 3 -s CP-01` | **Session shadow**: duplicate handshake for a live station | `R4_SESSION_UNIQUE`, socket closed 4001 |
| `-a 4 -s CP-02` | **Subtle drift**: −2 %/tick under-report, power and current scaled together | Tier-2 ML 0.75 at +9.7 s (no rule fires), then `R6_METER_FRAUD` quarantine at +43.9 s |
| `-a 5 -s CP-06 -t TAG-CP-07` | **Card clone, same site**: a card already charging on another bay | `R8_TAG_CLONE`: Authorize answered `Blocked` by the proxy, never reaches the CSMS |
| `-a 5 -s CHN-02 -t TAG-CP-05 --control-url ws://localhost:9101/control` | **Card clone, across sites**: a Vellore card presented in Chennai | `R8_TAG_CLONE` in Chennai; Vellore raises `R9_THREAT_INTEL` on the bay using the card |
| `-a 3 -s CP-03 --proxy-url ws://localhost:8001/ocpp` | **A charge point Vellore quarantined (within 15 min) tries to connect in Chennai** | `R9_THREAT_INTEL`, handshake refused 4003 |
| `-a r` | Reset a site | — |

Honest bays raise nothing: in a 25-minute run that took all eight cars through the 80 % hand-off
into the CV taper, no alarm fired and the highest ML score was 0.44.

---

## Detection engine

**Tier 1 — deterministic rules** ([`proxy/rules.py`](proxy/rules.py)), pure functions:

| Rule | Trips when | Action |
| :--- | :--- | :--- |
| `R1_STATE_ORDER` | `MeterValues`/`StopTransaction` with no live `Authorize → StartTransaction` | log |
| `R2_PHYSICS` | power > 150 kW, or > 60 kW above 80 % SoC (CV taper), or negative | quarantine |
| `R3_OSCILLATION` | ≥ 6 start/stop transitions fleet-wide in a 10 s window | log |
| `R4_SESSION_UNIQUE` | a second handshake for a station with a live socket | refuse socket |
| `R5_TXN_INTEGRITY` | `MeterValues.transactionId` unknown or owned by another station | quarantine |
| `R6_METER_FRAUD` | energy register pulling away from reported power faster than any honest session | quarantine |
| `R7_ELECTRICAL` | reported power ≠ reported DC voltage × current (beyond 2 kW / 10 %) | quarantine |
| `R8_TAG_CLONE` | an RFID card already in a live session, at this site or another | block Authorize |
| `R9_THREAT_INTEL` | a card or charge point another site reported as compromised | block / refuse |

Quarantine drops the offending frame, sends `ChangeAvailability{Inoperative}`
downstream, publishes the station status to the dashboard, and closes the socket (4001)
on the ack.

**Tier 2 — behavioural ML** ([`proxy/ml_engine.py`](proxy/ml_engine.py)): a 5-dimensional
session feature vector `[power_kw, soc, dp_dt, duration_sec, energy_residual_kwh]` scored
by `IsolationForest(n_estimators=100, contamination=0.02, random_state=42)`. The model is
loaded from `proxy/models/tier2.pkl`, trained by `python -m proxy.train_tier2 --source twin`
on the fleet's own honest charging curve ([`shared/charging.py`](shared/charging.py)): full
sessions from constant power through the 78-80 % hand-off and the slow taper to 100 %, up to
2.5 hours. Its score scale is calibrated on 30 held-out full honest sessions so the most
unusual honest reading scores 0.5; on 28 more, none crossed 0.65, while drift is caught in a
median 7-8 s when it starts below 80 % SoC (benchmark and limits in
[`proxy/tier2_report.md`](proxy/tier2_report.md); `--data <file>` trains on a real dataset
instead). The alert line is `ml_score > 0.65`. The first two samples of a session are not
scored.

**Cost.** Tier-1 on a full MeterValues frame (11 sampled values, ~1 KB, JSON parse
included) measured **16.3 µs mean, 16.0 µs median, 19.0 µs p99** over 200,000 frames.
A minimal three-value frame takes ~7 µs.

---

## Federation (multi-site)

| | |
| :--- | :--- |
| **Authentication** | Every message is HMAC-SHA256 signed. Each node has its own key, derived from the hub's master secret (`python -m federation.keys NODE_ID`), so one leaked key cannot speak for another site. |
| **Replay protection** | Strictly rising sequence numbers (seeded from the clock, so they survive restarts) and a ±120 s timestamp window. Verified: wrong key, impersonation, replayed hello and stale timestamp are all refused (4401). |
| **Encryption** | Optional TLS on the node link (`--tls-cert/--tls-key` on the hub, `--hub-ca` on the node) and on OCPP ingress. Verified with TLS 1.3. |
| **What is shared** | Threats (without raw frames), indicators of compromise with a TTL (charge points: 15 min; cards: 1 h), and live card sessions. |
| **Privacy** | Card numbers never leave a site. Nodes share `HMAC(salt, idTag)`; the salt is issued by the hub after authentication. |
| **Resilience** | Detection never waits on the hub: card checks read a local replica. Verified: with the hub stopped, a site still quarantined a meter spoof; the indicator it raised was queued and delivered when the hub returned, and both nodes re-announced their sessions. |

The hub serves `ws://HOST:7701/network` (1 Hz snapshot) and `http://HOST:7701/healthz`;
every proxy serves `http://HOST:8100/healthz`.

---

## Deploying beyond a laptop

Every proxy setting has a flag and an environment variable (`python -m proxy.main --help`):

| Flag | Env | Default |
| :--- | :--- | :--- |
| `--node-id`, `--site-name`, `--region`, `--lat`, `--lon` | `VOLTSENTRY_NODE_ID`, … | `VS-VELLORE`, Vellore Campus Hub |
| `--bind` | `VOLTSENTRY_BIND` | `localhost` (use `0.0.0.0` for chargers on the LAN) |
| `--ingress-port`, `--feed-port` | `VOLTSENTRY_INGRESS_PORT`, `VOLTSENTRY_FEED_PORT` | `8000`, `8100` |
| `--csms-url` | `VOLTSENTRY_CSMS_URL` | `ws://localhost:9000` |
| `--bays`, `--transformer-kva` | `VOLTSENTRY_BAYS`, `VOLTSENTRY_TRANSFORMER_KVA` | `8`, sized for the bays |
| `--hub-url`, `--fed-key`, `--hub-ca` | `VOLTSENTRY_HUB_URL`, `VOLTSENTRY_FED_KEY`, `VOLTSENTRY_HUB_CA` | standalone |
| `--tls-cert`, `--tls-key` | `VOLTSENTRY_TLS_CERT`, `VOLTSENTRY_TLS_KEY` | plain `ws://` |
| `--mode` | `VOLTSENTRY_MODE` | `enforce` (`monitor` reports everything, blocks nothing) |

Hub: `python -m federation.hub --master-secret … --tls-cert … --tls-key …` (or
`VOLTSENTRY_FED_MASTER`). The hub warns while it runs on the development secret or
without TLS, and a node warns while it uses a development key. OCPP frames over 64 KB
are refused (close 1009).

### Rolling out to a new site: monitor first

Start every new site with `--mode monitor`. Every rule and the ML model still run, and
every threat is reported, but nothing is blocked: frames reach the CSMS, cards are never
answered `Blocked`, and no charger is quarantined or refused. Each threat records what
enforce mode would have done (`recommended_action`), in the dashboard ("monitor · would
quarantine"), the forensic log and the network feed, so an operator can count false
positives on that site's own meters before switching to `--mode enforce`.

A monitor-mode node still shares its threats and card sessions, but publishes no
blocklist entries, so its unproven detections cannot make other sites block anyone. The
hub's own cross-site card check still runs, and enforcing sites act on what it finds.
One rule acts in both modes: a second connection for a charge point that is already
connected is refused, because the proxy follows one session per charge point. The
dashboard shows a monitor banner, `/healthz` reports `"mode"`, and the Multi-Site Network
tab shows each site's mode.

### Known limits

- The fleet, the cars and the CSMS are simulated; no real charger hardware has been on
  the wire.
- A meter drift that starts above 80 % SoC, where the car takes at most 58 kW and less every
  minute, stays inside the honest taper: neither Tier-2 nor R6 catches it. A spoof there is
  still caught by R7 (power against volts × amps).
- Transformer kVA is computed from each charger's reported supply-side power with the
  charger's specified power factor (0.99); it is not a revenue meter on the transformer.
- Card checks act on `Authorize`. A charger that starts on a locally cached card without
  sending `Authorize` is only seen at `StartTransaction`.
- One hub instance, no high availability. Sites keep protecting themselves while it is
  down.
- The dashboard feeds (`:8100`, `:7701`) are read-only and unauthenticated: keep them on
  localhost or behind an authenticating reverse proxy.

---

## Dashboard

- **NOC console** — station grid with DC volts and amps per bay, live fleet power chart,
  a site-transformer panel (kVA load against the kVA rating, real and delivered power,
  measured bus voltage line-to-line and line-to-neutral, line current, measured
  frequency), threat timeline, and a header strip of measured values only. Fleet scaling
  to 4–48 bays synthesises extra bays with the same battery physics, always badged SIM,
  and re-sizes the transformer for them.
- **Multi-Site Network** — every VoltSentry node from the hub's feed: link status and
  heartbeat age, each site's transformer, load, voltage and frequency, the shared
  indicators with their expiry, the network-wide threat feed, and the hub's
  authentication counters.
- **Defences** — where the firewall sits (chargers → site network firewall → VoltSentry →
  CSMS) with live counts of messages inspected, forwarded and held back, then one card per
  safeguard with its live state: connections refused (duplicate, blocklisted, oversized,
  malformed), quarantines, cloned cards blocked, TLS, signed and replay-proof site links, the
  shared blocklist, the mode and the forensic log. Safeguards not built yet are shown as planned.
- **3D digital twin**, **network topology** with red-team injection, a procedural 3D
  **landing hero**, a **login** gate, and a **`/globe`** of real charging hubs from
  [OpenChargeMap](https://openchargemap.org) (optional key: copy
  `dashboard/.env.local.example` to `.env.local`).

---

## Tests

```bash
pytest -q
```

184 tests: OCPP parsing and unit normalisation, the electrical model, the battery pack,
every Tier-1 rule, the ML engine, the twin's meter output read back through the proxy's
parser, the detection pipeline end to end, the federation protocol (signatures, replay,
key derivation, TTLs, clone correlation), and a hub with two nodes over real sockets.

---

## Repository layout

```
proxy/        asyncio relay + detection engine (rules, ml_engine, ocpp, state, pipeline)
federation/   hub, node client, signed protocol, shared intel store, key tool
simulator/    mock CSMS, cyber-physical charger twin, CC-CV battery + pack model
cli/          red-team attack TUI and phone remote
dashboard/    Next.js 14 NOC dashboard, landing, globe
shared/       data contract (schemas.py) and site electrical model (electrical.py)
scripts/      run_network.py: a two-site network on one machine
tests/        pytest suites
```

---

## Tech stack

| Layer | Technology |
| :--- | :--- |
| Proxy, federation & detection | Python 3.11+, asyncio, websockets, scikit-learn; stdlib `hmac`/`ssl` |
| Dashboard | Next.js 14 (App Router), TypeScript, Tailwind CSS |
| 3D / WebGL | Three.js, @react-three/fiber, @react-three/drei |
| Audio | Web Audio API (procedural synthesis, zero audio files) |
| Protocol | OCPP 1.6-J over WebSocket (JSON) |

---

## Hackathon

Built for **Code2Create** — a 48-hour hackathon by ACM-VIT, Vellore.
500+ idea submissions → 200+ teams shortlisted → top 18 after two review rounds.

**Team:** Deepanshu Agarwal · Shubham Krishna · Krish Jairath
