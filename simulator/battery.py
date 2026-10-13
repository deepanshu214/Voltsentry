import math

# The honest charging curve (CC to 78 %, eased hand-off to 58 kW by 80 %, CV
# taper) lives in shared/charging.py, where the Tier-2 trainer reads it too.
from shared.charging import (  # noqa: E402
    CC_END_SOC,
    CV_ENTRY_KW,
    CV_KNEE_SOC,
    CV_TAPER_K,
    PACK_CAPACITY_KWH,
    cc_cv_power_kw,
)

# Pack electrical model. An 800 V-class pack (192 NMC cells in series) is what
# makes 120 kW plausible on a standard CCS2 cable: ~170 A at ~700 V, inside the
# 200 A an uncooled cable carries. The same 120 kW into a 400 V pack would need
# 300 A. Voltage and current are derived from the power curve above, so the
# power the rules and the ML model see is unchanged.
CELLS_IN_SERIES = 192
MAX_CELL_V = 4.20
PACK_RESISTANCE_OHM = 0.08
# Open-circuit cell voltage against SoC, a typical NMC curve.
_OCV_TABLE = (
    (0.0, 3.30), (10.0, 3.55), (20.0, 3.62), (30.0, 3.67), (40.0, 3.72), (50.0, 3.77),
    (60.0, 3.84), (70.0, 3.92), (80.0, 4.00), (90.0, 4.08), (100.0, 4.18),
)


def open_circuit_voltage_v(soc: float) -> float:
    soc = min(100.0, max(0.0, soc))
    for (s0, v0), (s1, v1) in zip(_OCV_TABLE, _OCV_TABLE[1:]):
        if soc <= s1:
            return CELLS_IN_SERIES * (v0 + (v1 - v0) * (soc - s0) / (s1 - s0))
    return CELLS_IN_SERIES * _OCV_TABLE[-1][1]


def terminal_voltage_current(power_kw: float, soc: float) -> tuple[float, float]:
    """(V, A) at the pack terminals while charging at power_kw.

    Charging pushes the terminal above open-circuit by I*R, and P = V*I, so
    I solves R*I^2 + OCV*I - P = 0.
    """
    ocv = open_circuit_voltage_v(soc)
    p_w = max(0.0, power_kw) * 1000.0
    if p_w == 0.0:
        return ocv, 0.0
    r = PACK_RESISTANCE_OHM
    current = (-ocv + math.sqrt(ocv * ocv + 4.0 * r * p_w)) / (2.0 * r)
    return ocv + current * r, current


class Battery:
    """
    Pure CC-CV (Constant Current / Constant Voltage) EV Battery model.
    Zero I/O.
    """

    def __init__(
        self,
        capacity_kwh: float = PACK_CAPACITY_KWH,
        max_power_kw: float = 120.0,
        initial_soc: float = 20.0,
        target_soc: float = 100.0,
    ) -> None:
        self.capacity_kwh = capacity_kwh
        self.max_power_kw = min(max_power_kw, 150.0)  # Hard physics cap at 150 kW
        self.soc = min(100.0, max(0.0, initial_soc))
        self.target_soc = min(100.0, max(0.0, target_soc))
        self.energy_kwh = 0.0
        # Terminal voltage / current during the most recent tick.
        self.voltage_v, self.current_a = terminal_voltage_current(0.0, self.soc)

    @property
    def power_kw(self) -> float:
        """Calculate instant charging power based on CC-CV curve."""
        return cc_cv_power_kw(self.soc, self.max_power_kw, self.target_soc)

    def tick(self, dt: float) -> tuple[float, float, float]:
        """
        Advance discrete time by dt seconds.
        Returns: (power_kw, soc, energy_kwh)
        """
        if dt <= 0.0 or self.soc >= 100.0:
            self.voltage_v, self.current_a = terminal_voltage_current(self.power_kw, self.soc)
            return self.power_kw, self.soc, self.energy_kwh

        current_power = self.power_kw
        self.voltage_v, self.current_a = terminal_voltage_current(current_power, self.soc)
        # Energy delivered during this tick (kWh)
        energy_added = current_power * (dt / 3600.0)
        self.energy_kwh += energy_added

        # Calculate SoC increase (%)
        delta_soc = (energy_added / self.capacity_kwh) * 100.0
        self.soc = min(100.0, self.soc + delta_soc)

        return current_power, self.soc, self.energy_kwh
