"""Fit the Tier-2 IsolationForest + benchmark it.

    python -m proxy.train_tier2 --source twin          # the shipped model (VoltSentry fleet)
    python -m proxy.train_tier2 --data <path> [--name ACN-Caltech] [--capacity 75]
        [--hz 1] [--start-frac 0.2] [--seed 42] [--out proxy/models/tier2.pkl]

--source twin fits on the fleet's own honest charging curve (ml_engine.fleet_baseline),
calibrates the score scale on held-out full honest sessions, and benchmarks it on
more full sessions and on both data attacks, all generated the way the live proxy
sees them (see twin_session_vectors). --data does the same job for a real dataset.

Loads sessions, splits deterministically, fits on honest train vectors (augmented
with the synthetic CC-CV baseline for a stable forest), saves the model, and writes
`proxy/tier2_report.md` with dataset provenance, an EDA summary, an explicit
real-vs-synthesised section, and the benchmark table (forest vs a CUSUM baseline).
Every number is computed from the run (honest-stats rule).
"""

from __future__ import annotations

import argparse
import random
import statistics
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from proxy import datasets, rules
from proxy.ml_engine import (
    FLEET_BASELINE_N,
    FLEET_RESIDUAL_STD_KWH,
    HONEST_WORST_SCORE,
    ML_ALERT_THRESHOLD,
    calibrate,
    compute_session_features,
    cusum_residual,
    fit_tier2,
    fleet_baseline,
    generate_baseline,
)
from shared.charging import PACK_CAPACITY_KWH, cc_cv_power_kw

DEFAULT_OUT = Path(__file__).resolve().parent / "models" / "tier2.pkl"
REPORT_PATH = Path(__file__).resolve().parent / "tier2_report.md"
WARMUP = 2                      # samples 1-2 are not scored (0-based: skip index < 2)
CUSUM_K, CUSUM_H = 0.5, 5.0     # slack / threshold for the residual CUSUM baseline


def split_sessions(sessions, seed: int, test_frac: float = 0.3):
    """Deterministic train/test split: sort by id, seeded permutation."""
    ordered = sorted(sessions, key=lambda s: s.session_id)
    rng = np.random.default_rng(seed)
    perm = rng.permutation(len(ordered))
    n_test = max(1, int(round(len(ordered) * test_frac)))
    test_idx = set(perm[:n_test].tolist())
    train = [ordered[i] for i in range(len(ordered)) if i not in test_idx]
    test = [ordered[i] for i in range(len(ordered)) if i in test_idx]
    return train, test


def _first_fire(model, vectors, from_index: int = 0):
    """First index >= max(from_index, WARMUP) whose score crosses the threshold."""
    start = max(from_index, WARMUP)
    for i in range(start, len(vectors)):
        if model.score(list(vectors[i])) > ML_ALERT_THRESHOLD:
            return i
    return None


def run_benchmark(sessions, seed: int = 42, start_frac: float = 0.2, hz: float = 1.0) -> dict:
    """Fit on train, evaluate on test; return the summary metric dict."""
    train, test = split_sessions(sessions, seed)

    honest_train = [v for s in train for v in datasets.session_to_vectors(s, seed=seed, hz=hz)]
    X = np.vstack([np.asarray(honest_train, dtype=float), generate_baseline()])
    model = fit_tier2(X, seed=seed)

    # false positives on untouched real test sessions
    fp = 0
    for s in test:
        vecs = datasets.session_to_vectors(s, seed=seed, hz=hz)
        if _first_fire(model, vecs, 0) is not None:
            fp += 1
    fpr = fp / len(test) if test else 0.0

    dt = 1.0 / hz
    recalls: dict = {}
    latencies: list = []
    cusum_recalls: dict = {}
    cusum_latencies: list = []
    for kind in ("meter_spoof", "subtle_drift"):
        hits = 0
        clat: list = []
        chits = 0
        for s in test:
            series = datasets.build_series(s, kind, start_frac=start_frac, seed=seed, hz=hz)
            fire = _first_fire(model, series.vectors, series.start_index)
            if fire is not None:
                hits += 1
                latencies.append((fire - series.start_index) * dt)
            alarm, _ = cusum_residual(series.residual, k=CUSUM_K, h=CUSUM_H)
            if alarm is not None and alarm >= series.start_index:
                chits += 1
                clat.append((alarm - series.start_index) * dt)
        recalls[kind] = hits / len(test) if test else 0.0
        cusum_recalls[kind] = chits / len(test) if test else 0.0
        cusum_latencies.extend(clat)

    def _p90(xs):
        if not xs:
            return 0.0
        return float(np.percentile(np.asarray(xs, dtype=float), 90))

    return {
        "n_train": len(train),
        "n_test": len(test),
        "false_positive_rate": round(fpr, 4),
        "recall_meter_spoof": round(recalls["meter_spoof"], 4),
        "recall_subtle_drift": round(recalls["subtle_drift"], 4),
        "latency_median_sec": round(statistics.median(latencies), 2) if latencies else 0.0,
        "latency_p90_sec": round(_p90(latencies), 2),
        "cusum_recall_meter_spoof": round(cusum_recalls["meter_spoof"], 4),
        "cusum_recall_subtle_drift": round(cusum_recalls["subtle_drift"], 4),
        "cusum_latency_median_sec": round(statistics.median(cusum_latencies), 2) if cusum_latencies else 0.0,
    }


def _eda(sessions) -> dict:
    energies = [s.energy_kwh for s in sessions]
    durations = [s.duration_sec / 60.0 for s in sessions]  # minutes
    peaks = []
    for s in sessions:
        series = datasets.build_series(s, "clean", seed=42)
        peaks.append(float(series.true_power.max()))
    connects = [s.connect_ts for s in sessions if s.connect_ts is not None]
    date_range = ("n/a", "n/a")
    span_days = 1
    if connects:
        lo = datetime.fromtimestamp(min(connects), tz=timezone.utc)
        hi = datetime.fromtimestamp(max(connects), tz=timezone.utc)
        date_range = (lo.strftime("%Y-%m-%d"), hi.strftime("%Y-%m-%d"))
        span_days = max(1, (hi.date() - lo.date()).days + 1)
    return {
        "count": len(sessions),
        "date_range": date_range,
        "sessions_per_day": round(len(sessions) / span_days, 2),
        "kwh_mean": round(statistics.mean(energies), 2),
        "kwh_min": round(min(energies), 2),
        "kwh_max": round(max(energies), 2),
        "dur_mean_min": round(statistics.mean(durations), 1),
        "dur_min": round(min(durations), 1),
        "dur_max": round(max(durations), 1),
        "peak_mean_kw": round(statistics.mean(peaks), 1),
        "peak_max_kw": round(max(peaks), 1),
    }


def write_report(path, data_path, name, sessions, bench, hz, capacity, seed, out_path) -> None:
    eda = _eda(sessions)
    any_real_power = any(s.power_profile for s in sessions)
    any_real_soc = any(s.soc_profile for s in sessions)
    cols = sorted({k for s in sessions for k in s.provenance.keys()}) or ["(defaults)"]
    lo, hi = eda["date_range"]

    md = f"""# VoltSentry Tier-2 training report

*Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} — every figure below is computed from this run.*

## Dataset

- **Name:** {name}
- **File:** `{data_path}`
- **Sessions loaded:** {eda['count']}  (train {bench['n_train']} / test {bench['n_test']})
- **Date range actually used:** {lo} → {hi}  (~{eda['sessions_per_day']} sessions/day)
- **Provenance fields detected:** {', '.join(cols)}
- **Sampling:** {hz:g} Hz · **assumed pack capacity:** {capacity:g} kWh · **seed:** {seed}

## EDA summary

| metric | value |
| :-- | :-- |
| delivered energy (kWh) | mean {eda['kwh_mean']}, min {eda['kwh_min']}, max {eda['kwh_max']} |
| session duration (min) | mean {eda['dur_mean_min']}, min {eda['dur_min']}, max {eda['dur_max']} |
| peak power (kW) | mean {eda['peak_mean_kw']}, max {eda['peak_max_kw']} |

## What is real vs synthesised / approximated

- **Delivered energy + duration:** real, taken from the dataset.
- **Per-timestep power:** {'real profile (dataset carries one)' if any_real_power else 'CC-CV shape **synthesized** and scaled so its integral equals delivered energy — energy + duration are real, only the within-session shape is assumed.'}
- **SoC:** {'real series from the dataset.' if any_real_soc else 'approximated as 100 · cumulative_energy / capacity (nominal {} kWh) — **approximated**.'.format(int(capacity))}
- **Energy-residual honest baseline:** modelled as additive noise ~ N(0, {datasets.RESIDUAL_NOISE_STD}) on the register — an **assumed** noise model.
- **Attacks:** `meter_spoof` (reports 5.0 kW) and `subtle_drift` (×0.98^k per tick) are synthesised to match `simulator/twin.py` exactly; the register always accumulates true energy.

## Model

- `IsolationForest(n_estimators=100, contamination=0.02, random_state={seed})`, fitted on honest train
  vectors augmented with a 1000-vector synthetic CC-CV baseline for forest stability.
- Score normalisation pinned at fit time (CONTEXT.md §5.B); alert threshold `ml_score > {ML_ALERT_THRESHOLD}`.
- Warm-up: the first two samples of a session are not scored.
- Saved to `{out_path}`.

## Benchmark (test set)

| metric | Isolation Forest | CUSUM (residual) |
| :-- | :-- | :-- |
| false-positive rate (honest sessions) | {bench['false_positive_rate']} | — |
| recall — meter_spoof | {bench['recall_meter_spoof']} | {bench['cusum_recall_meter_spoof']} |
| recall — subtle_drift | {bench['recall_subtle_drift']} | {bench['cusum_recall_subtle_drift']} |
| detection latency — median (s) | {bench['latency_median_sec']} | {bench['cusum_latency_median_sec']} |
| detection latency — p90 (s) | {bench['latency_p90_sec']} | — |

CUSUM (`k={CUSUM_K}`, `h={CUSUM_H}`) is an **offline** comparison on the same residual signal; it is
not wired into the live feed (the dashboard schema is frozen and carries no CUSUM statistic).

**Benchmark caveats (honest-stats).** This capture holds {eda['count']} sessions, so the held-out
test set is n={bench['n_test']} and the false-positive rate is coarse-grained (one honest session
flips it by {round(1.0 / max(bench['n_test'], 1), 2)}). The **shipped** model in `tier2.pkl` is
fitted on all {eda['count']} honest sessions plus the synthetic baseline, so the live proxy is not
scored against stations it never saw; a larger real corpus (e.g. full ACN-Data) would tighten every
figure above.

## Reproduce

```bash
python -m proxy.train_tier2 --data {data_path} --name "{name}" --capacity {capacity:g} --seed {seed}
```
"""
    Path(path).write_text(md, encoding="utf-8")


# ---- the VoltSentry fleet (--source twin) ---------------------------------
# Arrival SoCs for the benchmark: the fleet's own starting points
# (FleetManager: 15 + 4.5 i mod 35), the next car after a full one (15 %), and a
# sweep so a car arriving already above 80 % is covered too.
TWIN_ARRIVALS = [15.0 + (i * 4.5) % 35.0 for i in range(1, 9)] + [15.0] + [float(s) for s in range(5, 100, 5)]
TWIN_ATTACK_SOCS = (25.0, 50.0, 70.0, 85.0)
TWIN_CALIBRATION_SESSIONS = 30


def twin_session_vectors(
    arrival_soc: float,
    seed: int,
    attack: str | None = None,
    attack_soc: float | None = None,
    max_after_sec: float = 120.0,
) -> dict:
    """One session on the fleet's curve, as the live proxy sees it.

    The car charges from arrival to 100 % (or until max_after_sec after an
    attack begins). The twin ticks every 1.020-1.032 s (its real loop period) and
    its register counts that real time; each MeterValues reaches the proxy a few
    ms later, now and then 100-300 ms late; power and SoC are sent to 0.1 and the
    register to 10 Wh, exactly as simulator/twin.py formats them. Features come
    from the production compute_session_features. Attacks match the twin:
    subtle_drift reports power x 0.98^k, meter_spoof reports 5 kW, and the
    register always counts the true energy.
    """
    rng = random.Random(seed)
    soc, energy, t = arrival_soc, 0.0, 0.0
    prev_ts = prev_p = anchor = prev_res = None
    integral, count, k = 0.0, 0, 0
    vectors, arrivals, residuals = [], [], []
    attack_index = attack_t = r6_after = None
    while soc < 100.0:
        dt = rng.uniform(1.020, 1.032)
        t += dt
        power = cc_cv_power_kw(soc)                       # simulator.battery.Battery.tick
        energy += power * dt / 3600.0
        soc = min(100.0, soc + power * dt / 3600.0 / PACK_CAPACITY_KWH * 100.0)
        reported = power
        if attack and attack_t is None and soc >= attack_soc:
            attack_t, attack_index = t, len(vectors)
        if attack_t is not None:
            k += 1
            reported = 5.0 if attack == "meter_spoof" else power * (0.98 ** k)
        late = 0.004 + rng.expovariate(100.0) + (rng.uniform(0.1, 0.3) if rng.random() < 0.01 else 0.0)
        ts = t + late
        rep = round(reported, 1)
        f = compute_session_features(
            power_kw=rep, soc=round(soc, 1), energy_register_kwh=round(energy, 2), ts=ts,
            prev_ts=prev_ts, prev_power_kw=prev_p, energy_integral_kwh=integral,
            session_start_ts=0.0, sample_count=count, energy_register_start_kwh=anchor,
            prev_energy_residual_kwh=prev_res,
        )
        prev_ts, prev_p, integral, count = ts, rep, f.energy_integral_kwh, f.sample_count
        anchor, prev_res = f.energy_register_start_kwh, f.energy_residual_kwh
        vectors.append(f.as_vector())
        arrivals.append(t)
        residuals.append(f.energy_residual_kwh)
        if attack_t is not None:
            if r6_after is None and rules.check_meter_fraud(f.energy_residual_kwh, f.residual_rate_kwh_per_sec):
                r6_after = t - attack_t
            if t - attack_t > max_after_sec:
                break
    return {
        "vectors": np.asarray(vectors, dtype=float),
        "t": np.asarray(arrivals),
        "residual": np.asarray(residuals),
        "attack_index": attack_index,
        "attack_t": attack_t,
        "r6_after_sec": r6_after,
    }


def _scores(model, X) -> np.ndarray:
    if len(X) == 0:
        return np.zeros(0)
    raw = model.clf.decision_function(np.asarray(X, dtype=float))
    return np.clip((model._d_max - raw) / model._span, 0.0, 1.0)


def train_twin(seed: int = 42):
    """Fit on the fleet baseline, calibrate on held-out honest sessions. Returns (model, calibration info)."""
    model = fit_tier2(fleet_baseline(seed=seed), seed=seed)
    rng = random.Random(7)
    cal = [twin_session_vectors(rng.uniform(3.0, 97.0), seed=1000 + j)["vectors"][WARMUP:]
           for j in range(TWIN_CALIBRATION_SESSIONS)]
    calibrated = calibrate(model, np.vstack(cal))
    return calibrated, {"sessions": TWIN_CALIBRATION_SESSIONS, "samples": int(sum(len(c) for c in cal))}


def benchmark_twin(model) -> dict:
    """Honest false alarms over full sessions, and how fast each attack is caught."""
    worst, over, samples, fp_sessions = 0.0, 0, 0, 0
    longest = 0.0
    for i, arrival in enumerate(TWIN_ARRIVALS):
        run = twin_session_vectors(arrival, seed=i)
        s = _scores(model, run["vectors"][WARMUP:])
        worst = max(worst, float(s.max()))
        n_over = int((s > ML_ALERT_THRESHOLD).sum())
        over += n_over
        samples += len(s)
        fp_sessions += n_over > 0
        longest = max(longest, float(run["t"][-1]))
    attacks: dict = {}
    for kind in ("subtle_drift", "meter_spoof"):
        for at in TWIN_ATTACK_SOCS:
            ml, r6, total = [], [], 0
            for i, arrival in enumerate(TWIN_ARRIVALS):
                if arrival >= at:
                    continue
                total += 1
                run = twin_session_vectors(arrival, seed=i, attack=kind, attack_soc=at)
                s = _scores(model, run["vectors"])
                first = next((j for j in range(max(run["attack_index"], WARMUP), len(s)) if s[j] > ML_ALERT_THRESHOLD), None)
                if first is not None:
                    ml.append(float(run["t"][first] - run["attack_t"]))
                if run["r6_after_sec"] is not None:
                    r6.append(run["r6_after_sec"])
            attacks[(kind, at)] = {
                "sessions": total,
                "ml_caught": len(ml),
                "ml_median_sec": round(statistics.median(ml), 1) if ml else None,
                "ml_max_sec": round(max(ml), 1) if ml else None,
                "r6_caught": len(r6),
                "r6_median_sec": round(statistics.median(r6), 1) if r6 else None,
            }
    return {
        "honest_sessions": len(TWIN_ARRIVALS),
        "honest_samples": samples,
        "honest_worst_score": round(worst, 3),
        "honest_samples_over": over,
        "honest_sessions_with_false_alarm": fp_sessions,
        "longest_session_min": round(longest / 60.0, 1),
        "attacks": attacks,
    }


def write_twin_report(path, bench: dict, cal: dict, seed: int, out_path) -> None:
    rows = []
    for (kind, at), r in bench["attacks"].items():
        ml = f"{r['ml_caught']}/{r['sessions']}" + (f" · median {r['ml_median_sec']:g} s, max {r['ml_max_sec']:g} s" if r["ml_median_sec"] is not None else "")
        r6 = f"{r['r6_caught']}/{r['sessions']}" + (f" · median {r['r6_median_sec']:g} s" if r["r6_median_sec"] is not None else "")
        rows.append(f"| `{kind}` | {at:g} % | {ml} | {r6} |")
    md = f"""# VoltSentry Tier-2 training report

*Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} — every figure below is computed from this run.*

## What the model is fitted on

- **Source:** the VoltSentry fleet's own honest charging curve (`shared/charging.py`, the curve
  `simulator/battery.py` charges every car on): constant power to 78 % SoC, an eased hand-off to
  58 kW by 80 %, then the constant-voltage taper to 100 %.
- **Training set:** `ml_engine.fleet_baseline()` — {FLEET_BASELINE_N} honest vectors. SoC uniform, with
  extra weight on the 77.5–80.5 % hand-off and the top 92–100 %; power and dp/dt on the curve with
  noise; session age uniform over 0–9000 s; energy residual ~ N(0, {FLEET_RESIDUAL_STD_KWH}) kWh.
- **Why not the previous model:** it was fitted on eight 3-minute captures, all at full power. It had
  never seen the CV taper or a session longer than a few minutes, so every honest charger scored over
  0.65 once its car passed 80 %.

## Score scale (calibrated)

- `IsolationForest(n_estimators=100, contamination=0.02, random_state={seed})`; score formula unchanged
  (CONTEXT.md §5.B): `clip((d_max - d) / (d_max - d_min), 0, 1)`, alert when `> {ML_ALERT_THRESHOLD}`.
- `d_max` is fixed at fit time as before. `d_min` is anchored on **{cal['sessions']} held-out full honest
  sessions** ({cal['samples']} scored samples): their most unusual sample scores **{HONEST_WORST_SCORE}**. The
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

**Honest charging** — {bench['honest_sessions']} full sessions (the fleet's arrival SoCs plus 5–95 %),
{bench['honest_samples']} scored samples, longest {bench['longest_session_min']:g} min:

| metric | value |
| :-- | :-- |
| sessions with any score over {ML_ALERT_THRESHOLD} | {bench['honest_sessions_with_false_alarm']} / {bench['honest_sessions']} |
| samples over {ML_ALERT_THRESHOLD} | {bench['honest_samples_over']} |
| highest honest score | {bench['honest_worst_score']} |

**Attacks** — started when the car reaches the given SoC, on every benchmark session that arrives
below it. Time is from the first falsified reading.

| attack | starts at | ML score > {ML_ALERT_THRESHOLD} | R6 quarantine |
| :-- | :-- | :-- | :-- |
{chr(10).join(rows)}

**Limits.** Above 80 % the car takes at most 58 kW and less every minute. A drift that starts there
stays inside the honest taper and neither the model nor R6 catches it; a spoof there is still caught by
R7, which checks power against volts × amps. Below 80 %, where nearly all the energy (and the money)
is delivered, both attacks are caught. These numbers describe the simulated fleet; a real fleet's
curves and meters need their own data (`--data`).

## Reproduce

```bash
python -m proxy.train_tier2 --source twin
```
"""
    Path(path).write_text(md, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Fit + benchmark the VoltSentry Tier-2 model")
    ap.add_argument("--source", choices=["twin"], help="fit the shipped model on the VoltSentry fleet's own curve")
    ap.add_argument("--data", help="path to .csv/.json/.jsonl dataset")
    ap.add_argument("--name", default="EV charging dataset")
    ap.add_argument("--capacity", type=float, default=datasets.DEFAULT_CAPACITY_KWH)
    ap.add_argument("--hz", type=float, default=datasets.DEFAULT_HZ)
    ap.add_argument("--start-frac", type=float, default=datasets.DEFAULT_START_FRAC)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()
    if bool(args.source) == bool(args.data):
        ap.error("give exactly one of --source twin or --data <path>")

    if args.source == "twin":
        model, cal = train_twin(seed=args.seed)
        model.save(args.out)
        print(f"[train] model saved to {args.out}", flush=True)
        bench = benchmark_twin(model)
        write_twin_report(REPORT_PATH, bench, cal, args.seed, args.out)
        print(f"[train] report written to {REPORT_PATH}", flush=True)
        print(f"\n=== honest: {bench['honest_sessions_with_false_alarm']}/{bench['honest_sessions']} sessions with a false alarm, "
              f"worst score {bench['honest_worst_score']}, {bench['honest_samples']} samples", flush=True)
        for (kind, at), r in bench["attacks"].items():
            print(f"  {kind:12s} at {at:4g}%: ML {r['ml_caught']}/{r['sessions']} median {r['ml_median_sec']} s | "
                  f"R6 {r['r6_caught']}/{r['sessions']} median {r['r6_median_sec']} s", flush=True)
        return

    print(f"[train] loading {args.data}", flush=True)
    sessions = datasets.load_sessions(args.data, capacity=args.capacity)
    print(f"[train] {len(sessions)} sessions loaded", flush=True)

    bench = run_benchmark(sessions, seed=args.seed, start_frac=args.start_frac, hz=args.hz)

    # Fit the shipping model on ALL honest sessions (+baseline) and save it.
    honest = [v for s in sessions for v in datasets.session_to_vectors(s, seed=args.seed, hz=args.hz)]
    X = np.vstack([np.asarray(honest, dtype=float), generate_baseline()])
    model = fit_tier2(X, seed=args.seed)
    model.save(args.out)
    print(f"[train] model saved to {args.out}", flush=True)

    write_report(REPORT_PATH, args.data, args.name, sessions, bench, args.hz, args.capacity, args.seed, args.out)
    print(f"[train] report written to {REPORT_PATH}", flush=True)

    print("\n=== Tier-2 benchmark ===", flush=True)
    for key, val in bench.items():
        print(f"  {key:28s} {val}", flush=True)


if __name__ == "__main__":
    main()
