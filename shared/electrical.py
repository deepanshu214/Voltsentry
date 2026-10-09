"""Site electrical model — the one place the hub's supply chain is defined.

Used by the twin (which simulates the site) and the proxy (which turns charger
reports into a transformer picture), so the two can never disagree on units.

Supply chain of a DC fast-charging hub on an Indian distribution network:

    11 kV HT feeder --> 11 kV / 433 V transformer --> 415 V three-phase LT bus --> chargers

* 415 V is line-to-line. Each phase measured to neutral is 415 / sqrt(3) = 240 V,
  the same order as the ~230 V single-phase supply a house gets. A house uses
  one phase; a 120 kW charger needs all three.
* Transformer ratings are in **kVA** (apparent power), not volts and not kW.
  kVA = kW / power factor, because heating in the windings follows current,
  whatever the phase angle.
* Line current on the three-phase bus: I = S / (sqrt(3) * V_LL).
* A DC charger converts AC to DC at ~95 % efficiency, so the transformer
  supplies more than the cars receive: input_kW = output_kW / efficiency.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

SQRT3 = math.sqrt(3.0)

HV_SUPPLY_KV = 11.0
LT_NOMINAL_LL_V = 415.0
LT_NO_LOAD_LL_V = 433.0          # transformer secondary at no load
GRID_NOMINAL_HZ = 50.0
GRID_BAND_HZ = (49.90, 50.05)    # Indian grid operating band (IEGC)

# Voltage lost between no load and full load across the transformer impedance
# plus the LT busbar. ~3.5 % keeps the bus near 415-420 V at rated load.
LT_REGULATION_AT_FULL_LOAD = 0.035

CHARGER_RATED_KW = 120.0         # DC output rating per bay
CHARGER_MAX_DC_A = 200.0         # uncooled CCS2 cable limit
CHARGER_EFFICIENCY = 0.95        # AC -> DC conversion
CHARGER_POWER_FACTOR = 0.99      # active front-end rectifier
CHARGER_STANDBY_KW = 0.15        # controller, fans, display while charging

# Common distribution-transformer ratings in Indian practice. Above the largest,
# sites run several units in parallel.
STANDARD_TRANSFORMER_KVA = (250, 315, 400, 500, 630, 1000, 1250, 1600, 2000, 2500)


def ac_input_kw(dc_output_kw: float) -> float:
    """Active power the charger draws from the LT bus to deliver dc_output_kw."""
    return max(0.0, dc_output_kw) / CHARGER_EFFICIENCY + CHARGER_STANDBY_KW


def apparent_kva(input_kw: float, power_factor: float = CHARGER_POWER_FACTOR) -> float:
    return input_kw / power_factor


def line_current_a(kva: float, v_ll: float = LT_NOMINAL_LL_V) -> float:
    """Per-phase line current on a balanced three-phase bus."""
    return kva * 1000.0 / (SQRT3 * v_ll)


def phase_to_neutral_v(v_ll: float) -> float:
    return v_ll / SQRT3


def lt_bus_voltage(loading_fraction: float) -> float:
    """Line-to-line bus voltage at a given transformer loading (1.0 = rated kVA)."""
    loading = min(max(loading_fraction, 0.0), 1.5)
    return LT_NO_LOAD_LL_V * (1.0 - LT_REGULATION_AT_FULL_LOAD * loading)


def bay_peak_kva(rated_kw: float = CHARGER_RATED_KW) -> float:
    """Apparent power one bay pulls at full DC output."""
    return apparent_kva(ac_input_kw(rated_kw))


@dataclass(frozen=True)
class TransformerSelection:
    units: int
    unit_kva: int

    @property
    def total_kva(self) -> int:
        return self.units * self.unit_kva


def select_transformer(required_kva: float) -> TransformerSelection:
    """Smallest standard rating that covers required_kva with no overload.

    Past the largest standard unit, the same unit is paralleled.
    """
    for rating in STANDARD_TRANSFORMER_KVA:
        if rating >= required_kva:
            return TransformerSelection(1, rating)
    biggest = STANDARD_TRANSFORMER_KVA[-1]
    return TransformerSelection(math.ceil(required_kva / biggest), biggest)


def transformer_for_fleet(bays: int, rated_kw: float = CHARGER_RATED_KW) -> TransformerSelection:
    """Size the site transformer so every bay can run flat out at once."""
    return select_transformer(bays * bay_peak_kva(rated_kw))


# The live site: 8 bays of 120 kW. 8 x 127.8 kVA = 1022 kVA -> a 1250 kVA unit.
SITE_BAYS = 8
SITE_TRANSFORMER_KVA = float(transformer_for_fleet(SITE_BAYS).total_kva)
