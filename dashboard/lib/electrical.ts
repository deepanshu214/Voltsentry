/**
 * Site electrical model, mirrored from shared/electrical.py and
 * simulator/battery.py. Keep the constants in step with those two files.
 *
 *   11 kV feeder -> 11 kV / 433 V transformer -> 415 V three-phase bus -> chargers
 *
 * 415 V is line-to-line; each line to neutral is 415 / sqrt(3) = 240 V, the same
 * order as the ~230 V single-phase supply in a house. Transformers are rated in
 * kVA (apparent power): kVA = kW / power factor.
 */

export const SQRT3 = Math.sqrt(3);

export const HV_SUPPLY_KV = 11;
export const LT_NOMINAL_LL_V = 415;
export const LT_NO_LOAD_LL_V = 433;
export const LT_REGULATION_AT_FULL_LOAD = 0.035;
export const GRID_NOMINAL_HZ = 50;
export const GRID_BAND_HZ: [number, number] = [49.9, 50.05];

export const CHARGER_RATED_KW = 120;
export const CHARGER_EFFICIENCY = 0.95;
export const CHARGER_POWER_FACTOR = 0.99;
export const CHARGER_STANDBY_KW = 0.15;

export const STANDARD_TRANSFORMER_KVA = [250, 315, 400, 500, 630, 1000, 1250, 1600, 2000, 2500];

export function acInputKw(dcOutputKw: number): number {
  return Math.max(0, dcOutputKw) / CHARGER_EFFICIENCY + CHARGER_STANDBY_KW;
}

export function apparentKva(inputKw: number, pf = CHARGER_POWER_FACTOR): number {
  return inputKw / pf;
}

export function lineCurrentA(kva: number, vLL = LT_NOMINAL_LL_V): number {
  return (kva * 1000) / (SQRT3 * vLL);
}

export function phaseToNeutralV(vLL: number): number {
  return vLL / SQRT3;
}

export function ltBusVoltage(loadingFraction: number): number {
  const loading = Math.min(Math.max(loadingFraction, 0), 1.5);
  return LT_NO_LOAD_LL_V * (1 - LT_REGULATION_AT_FULL_LOAD * loading);
}

export function bayPeakKva(ratedKw = CHARGER_RATED_KW): number {
  return apparentKva(acInputKw(ratedKw));
}

export interface TransformerSelection {
  units: number;
  unitKva: number;
  totalKva: number;
}

/** Smallest standard rating that lets every bay run at full power at once. */
export function transformerForFleet(bays: number): TransformerSelection {
  const required = bays * bayPeakKva();
  for (const rating of STANDARD_TRANSFORMER_KVA) {
    if (rating >= required) return { units: 1, unitKva: rating, totalKva: rating };
  }
  const biggest = STANDARD_TRANSFORMER_KVA[STANDARD_TRANSFORMER_KVA.length - 1];
  const units = Math.ceil(required / biggest);
  return { units, unitKva: biggest, totalKva: units * biggest };
}

export function transformerLabel(sel: TransformerSelection): string {
  return sel.units === 1 ? `${sel.unitKva} kVA` : `${sel.units} × ${sel.unitKva} kVA`;
}

// ---- battery (mirrors simulator/battery.py) -------------------------------

const CV_ENTRY_KW = 58;
const CV_TAPER_K = 0.15;
const CC_END_SOC = 78;
const CV_KNEE_SOC = 80;
const CELLS_IN_SERIES = 192;
const PACK_RESISTANCE_OHM = 0.08;
const OCV_TABLE: [number, number][] = [
  [0, 3.3], [10, 3.55], [20, 3.62], [30, 3.67], [40, 3.72], [50, 3.77],
  [60, 3.84], [70, 3.92], [80, 4.0], [90, 4.08], [100, 4.18],
];

/** CC-CV charging power for a 120 kW bay at a given SoC. */
export function batteryPowerKw(soc: number, maxKw = CHARGER_RATED_KW): number {
  if (soc >= 100) return 0;
  const cvEntry = Math.min(maxKw, CV_ENTRY_KW);
  if (soc <= CC_END_SOC) return maxKw;
  if (soc <= CV_KNEE_SOC) {
    const k = Math.log(maxKw / cvEntry) / (CV_KNEE_SOC - CC_END_SOC);
    return maxKw * Math.exp(-k * (soc - CC_END_SOC));
  }
  return cvEntry * Math.exp(-CV_TAPER_K * (soc - CV_KNEE_SOC));
}

export function openCircuitV(soc: number): number {
  const s = Math.min(100, Math.max(0, soc));
  for (let i = 1; i < OCV_TABLE.length; i++) {
    const [s0, v0] = OCV_TABLE[i - 1];
    const [s1, v1] = OCV_TABLE[i];
    if (s <= s1) return CELLS_IN_SERIES * (v0 + ((v1 - v0) * (s - s0)) / (s1 - s0));
  }
  return CELLS_IN_SERIES * OCV_TABLE[OCV_TABLE.length - 1][1];
}

/** Pack terminal voltage and current while charging at powerKw (P = V * I). */
export function terminalVI(powerKw: number, soc: number): { v: number; a: number } {
  const ocv = openCircuitV(soc);
  const pW = Math.max(0, powerKw) * 1000;
  if (pW === 0) return { v: ocv, a: 0 };
  const r = PACK_RESISTANCE_OHM;
  const a = (-ocv + Math.sqrt(ocv * ocv + 4 * r * pW)) / (2 * r);
  return { v: ocv + a * r, a };
}
