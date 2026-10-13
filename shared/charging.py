"""The honest DC charging curve — power against state of charge.

Used by the twin (simulator/battery.py charges every car on it) and by the
Tier-2 trainer (proxy/ml_engine.fleet_baseline learns what honest charging
looks like from it), so the two can never disagree about what "normal" is.

Shape: constant power to 78 % SoC, an eased step down to the CV entry power by
80 %, then an exponential constant-voltage taper toward 100 %.
"""

from __future__ import annotations

import math

# Nominal pack the simulated cars carry.
PACK_CAPACITY_KWH = 75.0
MAX_POWER_KW = 120.0

# Power the CV phase enters at, in kW. The proxy's R2 rule (CONTEXT.md 5.B)
# treats anything over 60 kW above 80% SoC as a physics violation, and
# CONTEXT.md 5.C pins constant current all the way to the 80% knee. Those two
# together mean the CC->CV transition has to be a step down rather than a
# continuation from max power: an exponential decaying from 120 kW sits above
# 60 kW until ~88.7% SoC, so every honest station in the fleet quarantined
# itself on a false positive as it crossed 80%.
CV_ENTRY_KW = 58.0
CV_TAPER_K = 0.15

# Where the hand-off into CV begins. Power has to be under CV_ENTRY_KW by the
# time SoC crosses 80, and doing that in one tick spikes dp_dt hard enough for
# the Tier-2 forest to score it 0.81 -- a false ML badge on every clean station
# at the knee. Easing it in over the last couple of percent keeps dp_dt in the
# same range as the honest taper the model was fitted on.
CC_END_SOC = 78.0
CV_KNEE_SOC = 80.0


def cc_cv_power_kw(soc: float, max_power_kw: float = MAX_POWER_KW, target_soc: float = 100.0) -> float:
    """Charging power (kW) an honest charger delivers to a car at this SoC."""
    if soc >= target_soc or soc >= 100.0:
        return 0.0

    cv_entry = min(max_power_kw, CV_ENTRY_KW)

    if soc <= CC_END_SOC:
        # Constant Current / Constant Power phase
        return max_power_kw

    if soc <= CV_KNEE_SOC:
        # Hand-off: decay from max power to the CV entry power so that the
        # station is already under R2's 60 kW ceiling when it crosses 80%.
        k = math.log(max_power_kw / cv_entry) / (CV_KNEE_SOC - CC_END_SOC)
        return max_power_kw * math.exp(-k * (soc - CC_END_SOC))

    # Constant Voltage exponential taper, decaying toward zero by 100%.
    return cv_entry * math.exp(-CV_TAPER_K * (soc - CV_KNEE_SOC))


def honest_dp_dt(soc: float, capacity_kwh: float = PACK_CAPACITY_KWH, max_power_kw: float = MAX_POWER_KW) -> float:
    """How fast honest power changes at this SoC, in kW per second.

    dP/dt = dP/dSoC x dSoC/dt, and the pack fills at dSoC/dt = P / (capacity x 36) % per second.
    """
    p = cc_cv_power_kw(soc, max_power_kw)
    if p <= 0.0:
        return 0.0
    h = 0.01
    dp_dsoc = (cc_cv_power_kw(min(soc + h, 100.0), max_power_kw) - cc_cv_power_kw(max(soc - h, 0.0), max_power_kw)) / (2 * h)
    return dp_dsoc * p / (capacity_kwh * 36.0)
