"use client";

import React from "react";
import { GridEvent } from "../lib/types";
import { Icon3DTransformer } from "./3dIcons";
import * as el from "../lib/electrical";

interface TransformerGaugeProps {
  grid: GridEvent | null;
  /** Bays on this site's dashboard (live + SIM). */
  bays: number;
  /**
   * Set when the fleet is scaled past the live backend: the DC output and
   * active count of the whole (live + SIM) fleet. The transformer is then
   * sized for that many bays and every figure is modelled, not measured.
   */
  scaledOutputKw?: number;
  scaledActive?: number;
}

export function TransformerGauge({ grid, bays, scaledOutputKw, scaledActive }: TransformerGaugeProps) {
  const scaled = scaledOutputKw != null;
  const pf = grid?.power_factor ?? el.CHARGER_POWER_FACTOR;
  const eff = grid?.charger_efficiency ?? el.CHARGER_EFFICIENCY;
  const hvKv = grid?.hv_supply_kv ?? el.HV_SUPPLY_KV;
  const ltNominal = grid?.lt_nominal_v ?? el.LT_NOMINAL_LL_V;

  let ratingKva: number;
  let ratingLabel: string;
  let outputKw: number;
  let inputKw: number;
  let kva: number;
  let active: number;
  let busV: number | null;

  if (scaled) {
    const sel = el.transformerForFleet(bays);
    ratingKva = sel.totalKva;
    ratingLabel = el.transformerLabel(sel);
    outputKw = scaledOutputKw!;
    active = scaledActive ?? 0;
    inputKw = outputKw / eff + active * el.CHARGER_STANDBY_KW;
    kva = el.apparentKva(inputKw, pf);
    busV = el.ltBusVoltage(kva / ratingKva);
  } else {
    ratingKva = grid?.transformer_capacity_kva ?? el.transformerForFleet(bays).totalKva;
    ratingLabel = `${ratingKva.toFixed(0)} kVA`;
    // Recordings made before the kVA fields existed carried DC output in total_load_kw.
    const legacy = grid != null && grid.total_load_kva == null;
    outputKw = grid?.output_kw ?? (legacy ? grid!.total_load_kw : 0);
    inputKw = legacy ? outputKw / eff : grid?.total_load_kw ?? 0;
    kva = grid?.total_load_kva ?? el.apparentKva(inputKw, pf);
    active = grid?.active_stations ?? 0;
    busV = grid?.lt_voltage_v ?? null;
  }

  const loadingPct = Math.max(0, (kva / ratingKva) * 100);
  const barPct = Math.min(loadingPct, 100);
  const lineA = el.lineCurrentA(kva, busV ?? ltNominal);
  const hz = scaled ? null : grid?.frequency_hz ?? null;

  const tone =
    loadingPct > 90
      ? { bar: "bg-rose-500", text: "text-rose-400", label: "OVERLOAD RISK" }
      : loadingPct > 80
      ? { bar: "bg-amber-400", text: "text-amber-400", label: "HIGH DEMAND" }
      : { bar: "bg-emerald-400", text: "text-emerald-400", label: "NORMAL OPERATION" };

  return (
    <div className="w-full p-4 rounded-2xl glass-panel flex flex-col space-y-3">
      {/* Header: the supply chain in one line */}
      <div className="flex flex-wrap items-center justify-between gap-2 pb-2 border-b border-white/10">
        <div className="flex items-center space-x-2">
          <Icon3DTransformer className="w-6 h-6" />
          <h3 className="text-sm font-bold text-slate-200 uppercase tracking-wider font-mono">Site Transformer</h3>
        </div>
        <span className="text-[11px] font-mono text-slate-400">
          {hvKv.toFixed(0)} kV → {ltNominal.toFixed(0)} V 3φ ·{" "}
          <span className="text-slate-200 font-bold">{ratingLabel}</span>
        </span>
      </div>

      {scaled && (
        <div className="rounded-md border border-amber-500/40 bg-amber-500/10 px-2 py-1 font-mono text-[10px] uppercase tracking-wider text-amber-300">
          Includes SIM bays · transformer re-sized for {bays} bays · figures modelled
        </div>
      )}

      {/* Main readout: apparent power against the kVA rating */}
      <div className="flex items-end justify-between">
        <div>
          <div className="text-xs text-slate-400 font-semibold uppercase tracking-wider">Transformer load</div>
          <div className="text-3xl font-black font-mono text-white mt-0.5 tabular-nums">
            {kva.toFixed(0)} <span className="text-sm text-slate-400 font-normal">kVA</span>
          </div>
        </div>
        <div className="text-right font-mono">
          <div className="text-xs text-slate-400">of {ratingKva.toFixed(0)} kVA</div>
          <div className={`text-xl font-bold tabular-nums ${tone.text}`}>{loadingPct.toFixed(1)}%</div>
        </div>
      </div>

      <div className="w-full bg-black/60 rounded-full h-3 overflow-hidden p-0.5 border border-white/15">
        <div className={`h-full rounded-full transition-all duration-500 ${tone.bar}`} style={{ width: `${barPct}%` }} />
      </div>

      {/* Electrical breakdown */}
      <div className="grid grid-cols-2 gap-x-4 gap-y-2 font-mono text-xs">
        <Stat label="Real power drawn" value={`${inputKw.toFixed(0)} kW`} sub={`kVA × PF ${pf.toFixed(2)}`} />
        <Stat label="Delivered to EVs" value={`${outputKw.toFixed(0)} kW DC`} sub={`charger η ${(eff * 100).toFixed(0)}%`} />
        <Stat
          label="Bus voltage"
          value={busV != null ? `${busV.toFixed(1)} V` : "—"}
          sub={
            busV != null
              ? `L-L · ${el.phaseToNeutralV(busV).toFixed(0)} V L-N · ${scaled ? "modelled" : "measured"}`
              : "not reported"
          }
        />
        <Stat label="Line current" value={`${lineA.toFixed(0)} A`} sub="per phase" />
        <Stat
          label="Grid frequency"
          value={hz != null ? `${hz.toFixed(3)} Hz` : "—"}
          sub={hz != null ? "measured at charger inlets" : scaled ? "site-wide value only" : "not reported"}
        />
        <Stat label="Active bays" value={`${active} / ${bays}`} sub={`${el.CHARGER_RATED_KW} kW DC each`} />
      </div>

      <div className="pt-2 border-t border-white/10 flex items-center justify-between text-xs font-mono">
        <span className="text-slate-400">Status</span>
        <strong className={`${tone.text} ${loadingPct > 90 ? "animate-pulse" : ""}`}>{tone.label}</strong>
      </div>

      <p className="text-[10px] leading-relaxed text-slate-500">
        Transformers are rated in kVA (apparent power = kW ÷ power factor), not volts. A home gets ~230 V on one
        phase; this site takes {ltNominal.toFixed(0)} V across three phases, which is{" "}
        {el.phaseToNeutralV(ltNominal).toFixed(0)} V from each line to neutral.
      </p>
    </div>
  );
}

function Stat({ label, value, sub }: { label: string; value: string; sub: string }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-slate-500">{label}</div>
      <div className="text-slate-100 font-bold tabular-nums">{value}</div>
      <div className="text-[9px] text-slate-500">{sub}</div>
    </div>
  );
}
