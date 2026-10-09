"use client";

/**
 * useGridFrequency — the supply frequency the chargers measure.
 *
 * Each charger reports `Frequency` at its inlet in MeterValues; the proxy
 * averages fresh reports into GridEvent.frequency_hz. Nothing here models or
 * invents a value: when the feed carries no frequency (an older recording, or
 * chargers that do not report it) hz is null and the UI shows a dash.
 *
 * Frequency belongs to the whole interconnected grid. A single charging site
 * cannot move it, so it is not derived from this site's load.
 */

import type { GridEvent } from "./types";
import { GRID_BAND_HZ, GRID_NOMINAL_HZ } from "./electrical";

export interface GridFrequency {
  /** Measured frequency in Hz, or null when not reported. */
  hz: number | null;
  /** Signed deviation from 50.00 Hz, or null. */
  deviation: number | null;
  /** True when outside the grid operating band (49.90–50.05 Hz). */
  strained: boolean;
}

export function useGridFrequency(grid: GridEvent | null): GridFrequency {
  const hz = grid?.frequency_hz ?? null;
  if (hz == null) return { hz: null, deviation: null, strained: false };
  return {
    hz,
    deviation: hz - GRID_NOMINAL_HZ,
    strained: hz < GRID_BAND_HZ[0] || hz > GRID_BAND_HZ[1],
  };
}
