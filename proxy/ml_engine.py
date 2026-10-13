"""Tier-2 behavioural model (CONTEXT.md §5.B).

Two layers, both pure logic (no I/O):

  * the **feature vector** — derivation of the frozen 5-dim session vector from
    raw MeterValues (`compute_session_features`);
  * the **IsolationForest scorer** — a synthetic CC-CV baseline, the pinned
    forest, and the pinned score normalisation (`Tier2Model`, `generate_baseline`).

Feature order is frozen and never reordered (CONTEXT.md / AGENTS.md):
    [power_kw, soc, dp_dt, duration_sec, energy_residual_kwh]
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np
from sklearn.ensemble import IsolationForest

from shared.charging import CC_END_SOC, CV_KNEE_SOC, cc_cv_power_kw, honest_dp_dt

FEATURE_ORDER = ["power_kw", "soc", "dp_dt", "duration_sec", "energy_residual_kwh"]

# Pinned model hyperparameters (CONTEXT.md §5.B / AGENTS.md). The seed is
# load-bearing for demo reproducibility — never remove it.
N_ESTIMATORS = 100
CONTAMINATION = 0.02
RANDOM_STATE = 42
BASELINE_N = 1000

# CV taper knee — the SoC at which honest CC-CV charging leaves constant current
# (mirrors rules.py's R2 envelope; kept local to avoid a rules<-state<-ml cycle).
CV_TAPER_SOC = 80.0

# Alert threshold on the normalised score (CONTEXT.md §5.B).
ML_ALERT_THRESHOLD = 0.65

# Fleet baseline (fleet_baseline): honest charging on the twin's own curve.
FLEET_BASELINE_N = 6000
FLEET_POWER_STD_KW = 2.0
FLEET_DPDT_STD = 0.5               # kW/s
# Honest residual on the twin's meters: the register is sent to 10 Wh and a
# reading can arrive a few hundred ms late, so |residual| stays under ~0.01
# kWh (measured live: max 0.008). 0.015 leaves room above that.
FLEET_RESIDUAL_STD_KWH = 0.015
# A full session from 5 % to 100 % takes just over two hours on this curve.
FLEET_DURATION_MAX_SEC = 9000.0
# The curve's two short transitions, sampled more densely so an honest charger
# passing through them is not a rarity: the CC->CV hand-off and the last few
# percent, where power is a few kW and the session is old.
FLEET_HANDOFF_SOC = (CC_END_SOC - 0.5, CV_KNEE_SOC + 0.5)
FLEET_TOP_SOC = (92.0, 100.0)
FLEET_FOCUS_SHARE = 0.15           # of the baseline, for each of the two bands

# Calibrated scale (calibrate): the most unusual honest sample in a held-out
# set of full sessions scores this, leaving a margin under the 0.65 alert.
HONEST_WORST_SCORE = 0.5


@dataclass
class SessionFeatures:
    """One scored sample's features plus the accumulators to carry forward."""

    power_kw: float
    soc: float
    dp_dt: float
    duration_sec: float
    energy_residual_kwh: float
    # carried forward into the next sample (not part of the ML vector)
    energy_integral_kwh: float
    sample_count: int
    # register reading when the proxy started watching this session; the
    # residual is measured relative to it (see compute_session_features).
    energy_register_start_kwh: float = 0.0
    # how fast the residual is climbing right now, kWh/s. Not part of the ML
    # vector -- R6 reads it (rules.check_meter_fraud).
    residual_rate_kwh_per_sec: float = 0.0

    def as_vector(self) -> list[float]:
        """The 5-dim vector in the frozen FEATURE_ORDER."""
        return [self.power_kw, self.soc, self.dp_dt, self.duration_sec, self.energy_residual_kwh]


def compute_session_features(
    *,
    power_kw: float,
    soc: float,
    energy_register_kwh: float,
    ts: float,
    prev_ts: Optional[float],
    prev_power_kw: Optional[float],
    energy_integral_kwh: float,
    session_start_ts: Optional[float],
    sample_count: int,
    energy_register_start_kwh: Optional[float] = None,
    prev_energy_residual_kwh: Optional[float] = None,
) -> SessionFeatures:
    """Derive the feature vector for one MeterValues sample. Pure.

    * dp_dt is 0.0 on the first sample of a session (prev_ts is None) — FIX-10.
    * energy_residual = reported register - integral(reported power dt) — FIX-9.
      The integral accumulates the *reported* instantaneous power, so when a
      station under-reports power while its register keeps climbing on the truth,
      the residual grows monotonically and the forest fires.
    """
    if prev_ts is None:
        dt = 0.0
        dp_dt = 0.0
    else:
        dt = max(0.0, ts - prev_ts)
        dp_dt = 0.0 if dt == 0.0 else (power_kw - (prev_power_kw or 0.0)) / dt

    # The anchor is established once, on the first sample of a session, and
    # threaded forward by the caller thereafter. A caller that does not track it
    # gets 0.0, i.e. the plain FIX-9 definition.
    if energy_register_start_kwh is not None:
        anchor = energy_register_start_kwh
    elif prev_ts is None:
        anchor = energy_register_kwh
    else:
        anchor = 0.0
    new_integral = energy_integral_kwh + power_kw * (dt / 3600.0)
    residual = (energy_register_kwh - anchor) - new_integral
    duration = 0.0 if session_start_ts is None else max(0.0, ts - session_start_ts)

    # Current climb rate of the residual, for R6. Measured per sample rather
    # than as residual/duration, so a station that behaves honestly and only
    # later starts under-reporting is not hidden by its own good history.
    if prev_energy_residual_kwh is None or dt <= 0.0:
        residual_rate = 0.0
    else:
        residual_rate = (residual - prev_energy_residual_kwh) / dt

    return SessionFeatures(
        power_kw=power_kw,
        soc=soc,
        dp_dt=dp_dt,
        duration_sec=duration,
        energy_residual_kwh=residual,
        energy_integral_kwh=new_integral,
        sample_count=sample_count + 1,
        energy_register_start_kwh=anchor,
        residual_rate_kwh_per_sec=residual_rate,
    )


def generate_baseline(n: int = BASELINE_N, seed: int = RANDOM_STATE) -> np.ndarray:
    """Synthesize `n` honest CC-CV feature vectors (CONTEXT.md §5.B, plan H11).

    No labelled data and no training loop: the baseline *is* the physics. A clean
    session holds constant current (~120 kW) to 80% SoC, then tapers exponentially
    toward 100%, with dp_dt near zero and the energy residual on its N(0, 0.05)
    baseline (FIX-9). Deterministic given `seed` so the fitted forest — and the
    "0.65" threshold — mean the same thing every boot.

    Returns an (n, 5) array in the frozen FEATURE_ORDER.
    """
    rng = np.random.default_rng(seed)
    soc = rng.uniform(5.0, 100.0, n)

    # Power: flat CC below the CV knee, exponential taper above it.
    cc = soc <= CV_TAPER_SOC
    power = np.where(
        cc,
        120.0 + rng.normal(0.0, 2.0, n),
        120.0 * np.exp(-(soc - CV_TAPER_SOC) / 7.0) + rng.normal(0.0, 1.0, n),
    )
    power = np.clip(power, 0.0, None)

    # dp_dt: ~0 during CC, gently negative during the taper.
    dp_dt = np.where(cc, rng.normal(0.0, 0.3, n), rng.normal(-0.4, 0.3, n))

    duration = rng.uniform(0.0, 3600.0, n)
    residual = rng.normal(0.0, 0.05, n)

    return np.column_stack([power, soc, dp_dt, duration, residual])


class Tier2Model:
    """IsolationForest scorer with the pinned normalisation (CONTEXT.md §5.B).

    A plain container: the fitted forest plus the two normalisation anchors
    frozen at fit time. Build one with `fit_tier2(X)`; persist with `save` /
    `load` (joblib). `decision_function` returns *higher = more normal* on an
    unbounded raw scale, so a bare threshold means nothing across processes.
    Normalisation is pinned at fit time:

        d      = clf.decision_function(X_train)
        d_max  = d.max()
        d_min  = percentile(d, 0.5)
        score  = clip((d_max - clf.decision_function(x)) / (d_max - d_min), 0, 1)

    Deterministic given random_state=42. Alert when score > 0.65.
    """

    def __init__(self, clf: IsolationForest, d_max: float, d_min: float) -> None:
        self.clf = clf
        self._d_max = float(d_max)
        self._d_min = float(d_min)
        # Degenerate training set (all-equal scores) — avoid a zero-width divide.
        self._span = max(self._d_max - self._d_min, 1e-9)

    def score(self, vector: Sequence[float]) -> float:
        """Normalised anomaly score in [0, 1]; > 0.65 is an alert."""
        raw = float(self.clf.decision_function([list(vector)])[0])
        return float(np.clip((self._d_max - raw) / self._span, 0.0, 1.0))

    def save(self, path) -> None:
        from pathlib import Path

        import joblib

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({"clf": self.clf, "d_max": self._d_max, "d_min": self._d_min}, path)

    @classmethod
    def load(cls, path) -> "Tier2Model":
        import joblib

        blob = joblib.load(path)
        return cls(blob["clf"], blob["d_max"], blob["d_min"])


def fit_tier2(X: np.ndarray, seed: int = RANDOM_STATE) -> Tier2Model:
    """Fit the pinned IsolationForest on X and freeze its score normalisation.

    X is an (n, 5) array of honest feature vectors in the frozen FEATURE_ORDER
    (real dataset vectors, optionally augmented with `generate_baseline()`).
    """
    X = np.asarray(X, dtype=float)
    clf = IsolationForest(
        n_estimators=N_ESTIMATORS,
        contamination=CONTAMINATION,
        random_state=seed,
    ).fit(X)
    d = clf.decision_function(X)
    return Tier2Model(clf, float(d.max()), float(np.percentile(d, 0.5)))


def fleet_baseline(n: int = FLEET_BASELINE_N, seed: int = RANDOM_STATE) -> np.ndarray:
    """Honest feature vectors on the fleet's own charging curve (shared/charging.py).

    The generic `generate_baseline` assumes a taper from 120 kW at 80 % and
    sessions under an hour. The twin steps down to 58 kW by 80 % and a car
    charged to 100 % stays on the plug for up to ~two hours, so on that baseline
    every honest charger looked anomalous once its car passed 80 %.

    SoC is drawn uniformly plus extra weight on the hand-off and the top few
    percent; power and dp_dt follow the curve with noise; duration is uniform
    over a full session; the residual is the honest meter noise.
    Deterministic given `seed`. Returns an (n, 5) array in FEATURE_ORDER.
    """
    rng = np.random.default_rng(seed)
    n_focus = int(n * FLEET_FOCUS_SHARE)
    soc = np.concatenate([
        rng.uniform(0.0, 100.0, n - 2 * n_focus),
        rng.uniform(*FLEET_HANDOFF_SOC, n_focus),
        rng.uniform(*FLEET_TOP_SOC, n_focus),
    ])
    power = np.array([cc_cv_power_kw(s) for s in soc]) + rng.normal(0.0, FLEET_POWER_STD_KW, n)
    power = np.clip(power, 0.0, None)
    dp_dt = np.array([honest_dp_dt(s) for s in soc]) + rng.normal(0.0, FLEET_DPDT_STD, n)
    duration = rng.uniform(0.0, FLEET_DURATION_MAX_SEC, n)
    residual = rng.normal(0.0, FLEET_RESIDUAL_STD_KWH, n)
    return np.column_stack([power, soc, dp_dt, duration, residual])


def calibrate(model: Tier2Model, honest_vectors, worst_score: float = HONEST_WORST_SCORE) -> Tier2Model:
    """Re-anchor the score scale on honest sessions the forest was not fitted on.

    The fit-time anchor (0.5th percentile of the training scores) puts the
    rarest few percent of honest training samples above 0.65 by construction.
    A charger reports once a second, so over a long session it always reaches
    those rare stretches and raises false alarms. Here d_max stays as fitted
    and d_min is set so the most unusual honest sample in `honest_vectors`
    scores `worst_score`. The score formula itself is unchanged.
    """
    X = np.asarray(honest_vectors, dtype=float)
    worst_d = float(model.clf.decision_function(X).min())
    span = (model._d_max - worst_d) / worst_score
    return Tier2Model(model.clf, model._d_max, model._d_max - span)


def cusum_residual(
    residual_series: Sequence[float], k: float = 0.5, h: float = 5.0
) -> tuple[Optional[int], list[float]]:
    """One-sided CUSUM on the energy-residual series (offline comparison only).

    NOT wired into the live feed — the dashboard schema is frozen and carries no
    CUSUM statistic. This exists purely so the benchmark can compare the forest
    against a classic change detector on the same residual signal.

        S[0] = 0
        S[t] = max(0, S[t-1] + (r[t] - k))
        alarm when S[t] > h

    Returns (first alarm index or None, the S series).
    """
    s = 0.0
    series: list[float] = []
    alarm: Optional[int] = None
    for i, r in enumerate(residual_series):
        s = max(0.0, s + (float(r) - k))
        series.append(s)
        if alarm is None and s > h:
            alarm = i
    return alarm, series
