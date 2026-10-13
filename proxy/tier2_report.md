# VoltSentry Tier-2 training report

*Generated 2026-10-12 10:55 UTC — every figure below is computed from this run.*

## What the model is fitted on

- **Source:** the VoltSentry fleet's own honest charging curve (`shared/charging.py`, the curve
  `simulator/battery.py` charges every car on): constant power to 78 % SoC, an eased hand-off to
  58 kW by 80 %, then the constant-voltage taper to 100 %.
- **Training set:** `ml_engine.fleet_baseline()` — 6000 honest vectors. SoC uniform, with
  extra weight on the 77.5–80.5 % hand-off and the top 92–100 %; power and dp/dt on the curve with
  noise; session age uniform over 0–9000 s; energy residual ~ N(0, 0.015) kWh.
- **Why not the previous model:** it was fitted on eight 3-minute captures, all at full power. It had
  never seen the CV taper or a session longer than a few minutes, so every honest charger scored over
  0.65 once its car passed 80 %.

## Score scale (calibrated)

- `IsolationForest(n_estimators=100, contamination=0.02, random_state=42)`; score formula unchanged
  (CONTEXT.md §5.B): `clip((d_max - d) / (d_max - d_min), 0, 1)`, alert when `> 0.65`.
- `d_max` is fixed at fit time as before. `d_min` is anchored on **30 held-out full honest
  sessions** (196429 scored samples): their most unusual sample scores **0.5**. The
  fit-time anchor (0.5th percentile) put the rarest few percent of honest samples over 0.65 by
  construction, and a charger reporting once a second always reaches them in a long session.
- Warm-up: the first two samples of a session are not scored.

## How sessions are generated (honest and attacked)

Each session runs a car from its arrival SoC to 100 % as the live proxy sees it: the twin ticks every
1.020–1.032 s and its register counts that real time; each reading reaches the proxy a few ms late, 1 %
of them 100–300 ms late; power and SoC are sent to 0.1 and the register to 10 Wh, as
`simulator/twin.py` formats them. Features come from the production `compute_session_features`.
Attacks match the twin: `subtle_drift` reports power × 0.98^k, `meter_spoof` reports 5 kW, and the
register always counts the true energy.

## Benchmark

**Honest charging** — 28 full sessions (the fleet's arrival SoCs plus 5–95 %),
181292 scored samples, longest 127.2 min:

| metric | value |
| :-- | :-- |
| sessions with any score over 0.65 | 0 / 28 |
| samples over 0.65 | 0 |
| highest honest score | 0.502 |

**Attacks** — started when the car reaches the given SoC, on every benchmark session that arrives
below it. Time is from the first falsified reading.

| attack | starts at | ML score > 0.65 | R6 quarantine |
| :-- | :-- | :-- | :-- |
| `subtle_drift` | 25 % | 8/8 · median 7.2 s, max 8.2 s | 8/8 · median 44.1 s |
| `subtle_drift` | 50 % | 18/18 · median 8.2 s, max 9.2 s | 18/18 · median 43.1 s |
| `subtle_drift` | 70 % | 22/22 · median 8.2 s, max 10.2 s | 22/22 · median 43.6 s |
| `subtle_drift` | 85 % | 0/25 | 0/25 |
| `meter_spoof` | 25 % | 8/8 · median 0 s, max 0 s | 8/8 · median 7.2 s |
| `meter_spoof` | 50 % | 18/18 · median 0 s, max 0 s | 18/18 · median 7.2 s |
| `meter_spoof` | 70 % | 22/22 · median 0 s, max 0 s | 22/22 · median 7.2 s |
| `meter_spoof` | 85 % | 0/25 | 0/25 |

**Limits.** Above 80 % the car takes at most 58 kW and less every minute. A drift that starts there
stays inside the honest taper and neither the model nor R6 catches it; a spoof there is still caught by
R7, which checks power against volts × amps. Below 80 %, where nearly all the energy (and the money)
is delivered, both attacks are caught. These numbers describe the simulated fleet; a real fleet's
curves and meters need their own data (`--data`).

## Reproduce

```bash
python -m proxy.train_tier2 --source twin
```
