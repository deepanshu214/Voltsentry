"use client";

/**
 * NetworkTopology — scalable, interactive fleet network graph.
 *
 * The 2D fleet grid is fixed at the 8 stations the twin actually runs. This
 * view answers a different question: what does the topology look like as the
 * deployment scales, and where does power flow?
 *
 * Honesty contract (matters — this is a security tool):
 *  - Stations with a live TelemetryEvent render solid and are fully
 *    interactive, including red-team injection against the real twin.
 *  - Sizes above the live fleet add PROJECTED nodes: dashed outline, explicit
 *    badge, and injection disabled. They mirror a live station's telemetry so
 *    the layout animates, and they say so in the drawer. They are never
 *    presented as real chargers.
 *  - Supply frequency is the chargers' own inlet measurement, averaged by the
 *    proxy (lib/useGridFrequency.ts); shown as a dash when not reported.
 *
 * Rendering: one SVG, no per-frame JS. Power flow is a dashed overlay stroke
 * whose dashoffset animates in CSS; velocity and thickness are bound to kW, so
 * a busy feeder visibly runs faster and heavier than an idle one.
 */

import React, { useMemo, useState } from "react";
import {
  Activity,
  AlertTriangle,
  Ban,
  Gauge,
  Network,
  RefreshCw,
  ShieldAlert,
  Waves,
  X,
  Zap,
  Radio,
} from "lucide-react";
import type { GridEvent, TelemetryEvent, ThreatEvent } from "../lib/types";
import { useGridFrequency } from "../lib/useGridFrequency";
import { useTwinControl, type AttackType, type ControlAck } from "../lib/useTwinControl";

/* ------------------------------------------------------------------ */
/* Contract                                                            */
/* ------------------------------------------------------------------ */

const ML_THRESHOLD = 0.65; // CONTEXT.md §5.B — must match the rest of the UI
const NOMINAL_KW = 150; // R2_PHYSICS ceiling, used to normalise flow speed

const VIEW_W = 1000;
const VIEW_H = 620;
const CX = VIEW_W / 2;
const CY = VIEW_H / 2;

export const FLEET_SIZES = [4, 8, 16, 32, 64] as const;
export type FleetSize = (typeof FLEET_SIZES)[number];

export type LayoutPreset = "hub" | "corridor" | "ring";

const LAYOUTS: { id: LayoutPreset; label: string; blurb: string }[] = [
  { id: "hub", label: "Hub & Spoke", blurb: "Central transformer radiating to stalls" },
  { id: "corridor", label: "Corridor Mesh", blurb: "Interconnected highway plazas" },
  { id: "ring", label: "Microgrid Ring", blurb: "Circular bus with backup feeders" },
];

type NodeState = "charging" | "available" | "anomaly" | "quarantined" | "offline";

const STATE_FILL: Record<NodeState, string> = {
  charging: "#22D3EE",
  available: "#10B981",
  anomaly: "#F59E0B",
  quarantined: "#EF4444",
  offline: "#475569",
};

interface TopoNode {
  id: string;
  x: number;
  y: number;
  live: boolean;
  /** For projected nodes, the live station whose telemetry it mirrors. */
  mirrorOf?: string;
  telemetry?: TelemetryEvent;
  state: NodeState;
  powerKw: number;
  soc: number;
  mlScore: number;
}

interface Link {
  key: string;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  /** kW carried — drives thickness + flow speed. */
  kw: number;
  /** Mesh ties are structural, not power-carrying; drawn dim and static. */
  tie?: boolean;
  state: NodeState;
}

export interface NetworkTopologyProps {
  stations: Record<string, TelemetryEvent>;
  threats: ThreatEvent[];
  grid: GridEvent | null;
  /** Station ids with a real feed (the twin's fleet). */
  liveStationIds: string[];
}

/* ------------------------------------------------------------------ */
/* Layout maths                                                        */
/* ------------------------------------------------------------------ */

/** Largest ring radius that still fits the viewBox after the x/y scaling. */
const HUB_MAX_R = 340;
const HUB_SCALE_X = 1.32;
const HUB_SCALE_Y = 0.82;

function hubPositions(n: number): { x: number; y: number }[] {
  // Plan the rings first, then spread their radii across the available space,
  // so 4 stations and 64 stations both fill the frame instead of huddling.
  const rings: number[] = [];
  let remaining = n;
  let idx = 0;
  while (remaining > 0) {
    const cap = idx === 0 ? Math.min(remaining, 8) : Math.min(remaining, 10 + idx * 6);
    rings.push(cap);
    remaining -= cap;
    idx += 1;
  }

  const out: { x: number; y: number }[] = [];
  rings.forEach((count, r) => {
    const radius =
      rings.length === 1
        ? HUB_MAX_R * 0.72
        : HUB_MAX_R * (0.42 + 0.58 * (r / (rings.length - 1)));
    for (let i = 0; i < count; i++) {
      // Odd rings are half-stepped so nodes don't line up radially.
      const offset = r % 2 ? Math.PI / count : 0;
      const a = (i / count) * Math.PI * 2 - Math.PI / 2 + offset;
      out.push({
        x: CX + Math.cos(a) * radius * HUB_SCALE_X,
        y: CY + Math.sin(a) * radius * HUB_SCALE_Y,
      });
    }
  });
  return out;
}

function corridorPositions(n: number): { x: number; y: number }[] {
  // Two carriageways of plazas along a horizontal trunk.
  const perRow = Math.ceil(n / 2);
  const left = 120;
  const right = VIEW_W - 90;
  const span = right - left;
  const out: { x: number; y: number }[] = [];
  for (let i = 0; i < n; i++) {
    const row = i % 2;
    const col = Math.floor(i / 2);
    const t = perRow === 1 ? 0.5 : col / (perRow - 1);
    // Slight stagger so the two rows read as separate plazas.
    const x = left + t * span + (row === 0 ? 0 : span / (perRow * 2.2));
    const y = row === 0 ? CY - 138 : CY + 138;
    out.push({ x, y });
  }
  return out;
}

function ringPositions(n: number): { x: number; y: number }[] {
  const out: { x: number; y: number }[] = [];
  // Radius grows with fleet size so nodes never overlap at 64.
  const rx = Math.min(430, 250 + n * 3.0);
  const ry = Math.min(258, 150 + n * 1.8);
  for (let i = 0; i < n; i++) {
    const a = (i / n) * Math.PI * 2 - Math.PI / 2;
    out.push({ x: CX + Math.cos(a) * rx, y: CY + Math.sin(a) * ry });
  }
  return out;
}

function buildLinks(nodes: TopoNode[], layout: LayoutPreset): Link[] {
  const links: Link[] = [];

  if (layout === "hub") {
    nodes.forEach((nd) => {
      links.push({
        key: `f-${nd.id}`,
        x1: CX,
        y1: CY,
        x2: nd.x,
        y2: nd.y,
        kw: nd.powerKw,
        state: nd.state,
      });
    });
    return links;
  }

  if (layout === "corridor") {
    // Trunk from the substation to each carriageway, then plaza-to-plaza ties.
    nodes.forEach((nd) => {
      links.push({
        key: `f-${nd.id}`,
        x1: CX,
        y1: CY,
        x2: nd.x,
        y2: nd.y,
        kw: nd.powerKw,
        state: nd.state,
      });
    });
    // Ties run along each row (i and i+2 share a row given the interleave).
    for (let i = 0; i + 2 < nodes.length; i++) {
      const a = nodes[i];
      const b = nodes[i + 2];
      links.push({
        key: `t-${a.id}-${b.id}`,
        x1: a.x,
        y1: a.y,
        x2: b.x,
        y2: b.y,
        kw: 0,
        tie: true,
        state: "offline",
      });
    }
    return links;
  }

  // Ring: a closed bus around the perimeter + spokes every 4th node.
  for (let i = 0; i < nodes.length; i++) {
    const a = nodes[i];
    const b = nodes[(i + 1) % nodes.length];
    links.push({
      key: `bus-${a.id}`,
      x1: a.x,
      y1: a.y,
      x2: b.x,
      y2: b.y,
      kw: Math.max(a.powerKw, b.powerKw) * 0.5,
      state: a.state === "quarantined" || b.state === "quarantined" ? "quarantined" : a.state,
    });
  }
  const step = Math.max(1, Math.floor(nodes.length / 4));
  for (let i = 0; i < nodes.length; i += step) {
    const nd = nodes[i];
    links.push({
      key: `feed-${nd.id}`,
      x1: CX,
      y1: CY,
      x2: nd.x,
      y2: nd.y,
      kw: nd.powerKw,
      state: nd.state,
    });
  }
  return links;
}

/* ------------------------------------------------------------------ */
/* Helpers                                                             */
/* ------------------------------------------------------------------ */

function deriveState(t: TelemetryEvent | undefined): NodeState {
  if (!t) return "offline";
  if (t.status === "Quarantined") return "quarantined";
  if (t.ml_score > ML_THRESHOLD) return "anomaly";
  if (t.status === "Charging") return "charging";
  if (t.status === "Offline") return "offline";
  return "available";
}

function stationLabel(i: number): string {
  return `CP-${String(i + 1).padStart(2, "0")}`;
}

/** Flow animation period: a loaded feeder cycles fast, an idle one crawls. */
function flowDuration(kw: number): number {
  const ratio = Math.min(Math.max(kw / NOMINAL_KW, 0), 1);
  return 2.8 - ratio * 2.1; // 2.8s idle → 0.7s at rated power
}

function flowWidth(kw: number): number {
  const ratio = Math.min(Math.max(kw / NOMINAL_KW, 0), 1);
  return 1 + ratio * 2.6;
}

/* ------------------------------------------------------------------ */
/* Component                                                           */
/* ------------------------------------------------------------------ */

export default function NetworkTopology({
  stations,
  threats,
  grid,
  liveStationIds,
}: NetworkTopologyProps) {
  const [fleetSize, setFleetSize] = useState<FleetSize>(8);
  const [layout, setLayout] = useState<LayoutPreset>("hub");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [busy, setBusy] = useState<AttackType | null>(null);
  const [ack, setAck] = useState<ControlAck | null>(null);

  const { hz, deviation, strained } = useGridFrequency(grid);
  const { inject, status: controlStatus } = useTwinControl();

  const liveCount = liveStationIds.length;

  const nodes = useMemo<TopoNode[]>(() => {
    const coords =
      layout === "hub"
        ? hubPositions(fleetSize)
        : layout === "corridor"
        ? corridorPositions(fleetSize)
        : ringPositions(fleetSize);

    return Array.from({ length: fleetSize }, (_, i) => {
      const id = stationLabel(i);
      const isLive = i < liveCount;
      // Projected nodes mirror a live station so the graph animates honestly.
      const mirrorId = isLive ? id : liveStationIds[i % Math.max(liveCount, 1)];
      const telemetry = stations[mirrorId];

      return {
        id,
        x: coords[i]?.x ?? CX,
        y: coords[i]?.y ?? CY,
        live: isLive,
        mirrorOf: isLive ? undefined : mirrorId,
        telemetry,
        state: deriveState(telemetry),
        powerKw: telemetry?.power_kw ?? 0,
        soc: telemetry?.soc ?? 0,
        mlScore: telemetry?.ml_score ?? 0,
      };
    });
  }, [fleetSize, layout, stations, liveStationIds, liveCount]);

  const links = useMemo(() => buildLinks(nodes, layout), [nodes, layout]);

  const aggregateKw = useMemo(
    () => nodes.reduce((sum, n) => sum + n.powerKw, 0),
    [nodes]
  );

  const counts = useMemo(() => {
    const c = { charging: 0, anomaly: 0, quarantined: 0, offline: 0, available: 0 };
    nodes.forEach((n) => {
      c[n.state] += 1;
    });
    return c;
  }, [nodes]);

  const selected = selectedId ? nodes.find((n) => n.id === selectedId) ?? null : null;
  const selectedThreat = selected
    ? threats.find((t) => t.station_id === (selected.live ? selected.id : selected.mirrorOf))
    : undefined;

  const runInjection = async (attack: AttackType) => {
    if (!selected || !selected.live) return;
    setBusy(attack);
    setAck(null);
    const result = await inject(attack, attack === "fleet_oscillate" ? null : selected.id);
    setAck(result);
    setBusy(null);
  };

  const nodeRadius = fleetSize >= 32 ? 9 : fleetSize >= 16 ? 12 : 16;
  const showLabels = fleetSize <= 16;

  return (
    <div className="space-y-4">
      {/* ── Control bar ── */}
      <div className="flex flex-col gap-3 rounded-2xl glass-panel-dense p-3 lg:flex-row lg:items-center lg:justify-between">
        {/* Fleet size */}
        <div className="flex items-center gap-2">
          <span className="font-mono text-[10px] font-bold uppercase tracking-widest text-volt-muted">
            Fleet Size
          </span>
          <div className="flex items-center gap-1 rounded-xl border border-volt-line bg-black/50 p-1">
            {FLEET_SIZES.map((size) => (
              <button
                key={size}
                onClick={() => {
                  setFleetSize(size);
                  setSelectedId(null);
                }}
                className={`min-w-[38px] rounded-lg px-2.5 py-1.5 font-mono text-xs font-bold transition-all ${
                  fleetSize === size
                    ? "bg-volt-green text-slate-950"
                    : "text-volt-muted hover:text-white"
                }`}
              >
                {size}
              </button>
            ))}
          </div>
        </div>

        {/* Layout preset */}
        <div className="flex items-center gap-2">
          <span className="font-mono text-[10px] font-bold uppercase tracking-widest text-volt-muted">
            Topology
          </span>
          <div className="flex flex-wrap items-center gap-1 rounded-xl border border-volt-line bg-black/50 p-1">
            {LAYOUTS.map((l) => (
              <button
                key={l.id}
                onClick={() => setLayout(l.id)}
                title={l.blurb}
                className={`rounded-lg px-3 py-1.5 font-mono text-[11px] font-bold transition-all ${
                  layout === l.id
                    ? "bg-volt-elevated text-slate-100 ring-1 ring-volt-line-strong"
                    : "text-volt-muted hover:text-white"
                }`}
              >
                {l.label}
              </button>
            ))}
          </div>
        </div>

        {/* Live counts */}
        <div className="flex flex-wrap items-center gap-2 font-mono text-[10px]">
          <Legend color={STATE_FILL.charging} label={`${counts.charging} charging`} />
          <Legend color={STATE_FILL.available} label={`${counts.available} idle`} />
          <Legend color={STATE_FILL.anomaly} label={`${counts.anomaly} drift`} />
          <Legend color={STATE_FILL.quarantined} label={`${counts.quarantined} quarantined`} />
        </div>
      </div>

      {/* ── Graph ── */}
      <div className="relative overflow-hidden rounded-2xl border border-volt-line bg-volt-bg bg-dot-grid">
        <svg
          viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
          className="h-[560px] w-full"
          role="img"
          aria-label={`Fleet network topology, ${fleetSize} stations, ${layout} layout`}
        >
          <defs>
            <radialGradient id="substation-glow">
              <stop offset="0%" stopColor="#10B981" stopOpacity="0.55" />
              <stop offset="100%" stopColor="#10B981" stopOpacity="0" />
            </radialGradient>
          </defs>

          {/* Links — base wire then animated power flow overlay */}
          <g>
            {links.map((l) => (
              <line
                key={`base-${l.key}`}
                x1={l.x1}
                y1={l.y1}
                x2={l.x2}
                y2={l.y2}
                stroke={l.tie ? "#1E293B" : "#243044"}
                strokeWidth={l.tie ? 1 : 1.5}
                strokeDasharray={l.tie ? "3 5" : undefined}
              />
            ))}
            {links
              .filter((l) => !l.tie && l.kw > 0.5)
              .map((l) => (
                <line
                  key={`flow-${l.key}`}
                  x1={l.x1}
                  y1={l.y1}
                  x2={l.x2}
                  y2={l.y2}
                  stroke={STATE_FILL[l.state]}
                  strokeWidth={flowWidth(l.kw)}
                  strokeLinecap="round"
                  strokeDasharray="7 17"
                  opacity={0.9}
                  className="topo-flow"
                  style={{ animationDuration: `${flowDuration(l.kw)}s` }}
                />
              ))}
          </g>

          {/* Substation */}
          <g>
            <circle cx={CX} cy={CY} r={92} fill="url(#substation-glow)" />
            <circle
              cx={CX}
              cy={CY}
              r={46}
              fill="#0F1622"
              stroke={strained ? STATE_FILL.anomaly : "#10B981"}
              strokeWidth={2}
            />
            <text
              x={CX}
              y={CY - 16}
              textAnchor="middle"
              className="fill-volt-muted font-mono"
              style={{ fontSize: 9, letterSpacing: "0.12em" }}
            >
              SUBSTATION
            </text>
            <text
              x={CX}
              y={CY + 4}
              textAnchor="middle"
              className="fill-white font-mono"
              style={{ fontSize: 17, fontWeight: 700 }}
            >
              {aggregateKw.toFixed(0)} kW DC
            </text>
            <text
              x={CX}
              y={CY + 22}
              textAnchor="middle"
              fill={strained ? STATE_FILL.anomaly : "#10B981"}
              className="font-mono"
              style={{ fontSize: 11, fontWeight: 700 }}
            >
              {hz != null ? `${hz.toFixed(2)} Hz` : "— Hz"}
            </text>
            <text
              x={CX}
              y={CY + 35}
              textAnchor="middle"
              className="fill-volt-muted font-mono"
              style={{ fontSize: 8 }}
            >
              {deviation != null ? `${deviation >= 0 ? "+" : ""}${deviation.toFixed(3)} · measured` : "not reported"}
            </text>
          </g>

          {/* Station nodes */}
          <g>
            {nodes.map((nd) => {
              const isSelected = nd.id === selectedId;
              const fill = STATE_FILL[nd.state];
              const alert = nd.state === "quarantined" || nd.state === "anomaly";
              return (
                <g
                  key={nd.id}
                  transform={`translate(${nd.x}, ${nd.y})`}
                  onClick={() => setSelectedId(nd.id)}
                  style={{ cursor: "pointer" }}
                  role="button"
                  aria-label={`Station ${nd.id}, ${nd.state}`}
                >
                  {/* Alert halo */}
                  {alert && (
                    <circle
                      r={nodeRadius + 7}
                      fill="none"
                      stroke={fill}
                      strokeWidth={1}
                      opacity={0.5}
                      className="topo-halo"
                    />
                  )}
                  {isSelected && (
                    <circle
                      r={nodeRadius + 11}
                      fill="none"
                      stroke="#E2E8F0"
                      strokeWidth={1.2}
                      strokeDasharray="4 4"
                    />
                  )}
                  <circle
                    r={nodeRadius}
                    fill="#0B0F17"
                    stroke={fill}
                    strokeWidth={nd.live ? 2.4 : 1.4}
                    // Projected nodes read as provisional at a glance.
                    strokeDasharray={nd.live ? undefined : "3 3"}
                    opacity={nd.live ? 1 : 0.8}
                  />
                  <circle r={nodeRadius * 0.42} fill={fill} opacity={nd.live ? 0.95 : 0.55} />
                  {showLabels && (
                    <text
                      y={nodeRadius + 15}
                      textAnchor="middle"
                      className="fill-volt-muted font-mono"
                      style={{ fontSize: 9, letterSpacing: "0.06em" }}
                    >
                      {nd.id}
                    </text>
                  )}
                </g>
              );
            })}
          </g>
        </svg>

        {/* Projection notice — never let a projected node pass as real. */}
        {fleetSize > liveCount && (
          <div className="pointer-events-none absolute bottom-3 left-3 flex items-center gap-1.5 rounded-lg border border-volt-line bg-black/80 px-2.5 py-1.5 font-mono text-[10px] text-volt-muted backdrop-blur-sm">
            <AlertTriangle className="h-3 w-3 text-state-warn" />
            <span>
              {fleetSize - liveCount} projected node
              {fleetSize - liveCount === 1 ? "" : "s"} (dashed) — scaling preview,
              no live feed
            </span>
          </div>
        )}

        <div className="pointer-events-none absolute right-3 top-3 flex items-center gap-1.5 rounded-lg border border-volt-line bg-black/80 px-2.5 py-1.5 font-mono text-[10px] text-volt-muted backdrop-blur-sm">
          <Radio
            className={`h-3 w-3 ${
              controlStatus === "open" ? "text-state-healthy" : "text-volt-muted"
            }`}
          />
          <span>
            Twin control:{" "}
            {controlStatus === "open"
              ? "connected"
              : controlStatus === "unavailable"
              ? "offline (:9100)"
              : "idle"}
          </span>
        </div>
      </div>

      {/* ── Slide-over telemetry drawer ── */}
      {selected && (
        <StationDrawer
          node={selected}
          threat={selectedThreat}
          busy={busy}
          ack={ack}
          onClose={() => {
            setSelectedId(null);
            setAck(null);
          }}
          onInject={runInjection}
        />
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Drawer                                                             */
/* ------------------------------------------------------------------ */

const ATTACKS: { id: AttackType; label: string; icon: React.ReactNode; blurb: string }[] = [
  {
    id: "meter_spoof",
    label: "Spoof",
    icon: <Zap className="h-3.5 w-3.5" />,
    blurb: "Report 5 kW while drawing ~120 kW; V × A contradicts it (trips R7_ELECTRICAL)",
  },
  {
    id: "subtle_drift",
    label: "Drift",
    icon: <Waves className="h-3.5 w-3.5" />,
    blurb: "Slow, self-consistent under-report; Tier-2 ML flags it before any Tier-1 rule",
  },
  {
    id: "fleet_oscillate",
    label: "Oscillate",
    icon: <Activity className="h-3.5 w-3.5" />,
    blurb: "Fleet-wide start/stop flapping (trips R3_OSCILLATION)",
  },
  {
    id: "reset",
    label: "Reset",
    icon: <RefreshCw className="h-3.5 w-3.5" />,
    blurb: "Restore the whole fleet to honest behaviour",
  },
];

function StationDrawer({
  node,
  threat,
  busy,
  ack,
  onClose,
  onInject,
}: {
  node: TopoNode;
  threat: ThreatEvent | undefined;
  busy: AttackType | null;
  ack: ControlAck | null;
  onClose: () => void;
  onInject: (a: AttackType) => void;
}) {
  const t = node.telemetry;

  return (
    <div className="fixed inset-y-0 right-0 z-40 w-full max-w-sm animate-volt-rise overflow-y-auto border-l border-volt-line bg-volt-surface/95 p-5 shadow-2xl backdrop-blur-xl">
      <div className="flex items-start justify-between">
        <div>
          <div className="flex items-center gap-2">
            <span
              className="h-2.5 w-2.5 rounded-full"
              style={{ backgroundColor: STATE_FILL[node.state] }}
            />
            <h3 className="font-display text-xl font-black tracking-tight text-white">
              {node.id}
            </h3>
          </div>
          <p className="mt-1 font-mono text-[10px] uppercase tracking-widest text-volt-muted">
            {node.state}
          </p>
        </div>
        <button
          onClick={onClose}
          className="rounded-lg border border-volt-line bg-black/40 p-1.5 text-volt-muted transition-colors hover:text-white"
          aria-label="Close station drawer"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      {/* Projected-node disclosure */}
      {!node.live && (
        <div className="mt-4 rounded-xl border border-state-warn/40 bg-state-warn/10 p-3">
          <div className="flex items-center gap-1.5 font-mono text-[10px] font-bold uppercase tracking-wider text-state-warn">
            <AlertTriangle className="h-3 w-3" />
            Projected node
          </div>
          <p className="mt-1.5 text-[11px] leading-relaxed text-volt-muted">
            This station is a scaling preview, not a live charger. The values
            below mirror{" "}
            <span className="font-mono text-volt-text">{node.mirrorOf}</span>.
            Red-team injection is disabled — the twin only runs the live fleet.
          </p>
        </div>
      )}

      {/* Telemetry */}
      <div className="mt-4 space-y-2">
        <Stat label="Power draw" value={`${node.powerKw.toFixed(1)} kW DC`} />
        <Stat
          label="DC output"
          value={
            t?.dc_voltage_v != null && t?.dc_current_a != null
              ? `${t.dc_voltage_v.toFixed(0)} V × ${t.dc_current_a.toFixed(0)} A`
              : "—"
          }
          hint="P = V × I, cross-checked by R7"
        />
        <Stat
          label="Supply at inlet"
          value={t?.ac_voltage_v != null ? `${t.ac_voltage_v.toFixed(1)} V L-L` : "—"}
          hint="415 V three-phase bus"
        />
        <Stat label="State of charge" value={`${node.soc.toFixed(0)} %`} />
        <Stat
          label="Transaction ID"
          value={t?.transaction_id != null ? String(t.transaction_id) : "— none —"}
        />
        <Stat
          label="Energy register"
          value={t ? `${t.energy_register_kwh.toFixed(2)} kWh` : "—"}
        />
        <Stat
          label="Energy residual"
          value={t ? `${t.energy_residual_kwh.toFixed(2)} kWh` : "—"}
          hint="register − ∫ reported power · dt"
        />
        <Stat
          label="Risk index"
          value={node.mlScore.toFixed(2)}
          tone={node.mlScore > ML_THRESHOLD ? "warn" : "normal"}
          hint={`Tier-2 alert threshold ${ML_THRESHOLD}`}
        />
      </div>

      {/* SoC bar */}
      <div className="mt-3">
        <div className="h-1.5 w-full overflow-hidden rounded-full bg-volt-bg">
          <div
            className="h-full transition-all duration-500"
            style={{
              width: `${Math.min(Math.max(node.soc, 0), 100)}%`,
              backgroundColor: STATE_FILL[node.state],
            }}
          />
        </div>
      </div>

      {/* Active threat */}
      {threat && (
        <div className="mt-4 rounded-xl border border-state-critical/40 bg-state-critical/10 p-3">
          {/* This is the station's most recent entry in the incident log, not
              necessarily a live condition — a reset station keeps its history. */}
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-1.5 font-mono text-[10px] font-bold uppercase tracking-wider text-rose-300">
              <ShieldAlert className="h-3 w-3" />
              Tier-{threat.tier} · {threat.rule_id ?? "ML"}
            </div>
            <span className="font-mono text-[9px] uppercase tracking-wider text-volt-muted">
              last incident
            </span>
          </div>
          <p className="mt-1.5 text-[11px] leading-relaxed text-volt-muted">
            {threat.reason}
          </p>
          <p className="mt-1 font-mono text-[10px] uppercase tracking-wider text-rose-300/80">
            Action: {threat.action_taken} ·{" "}
            {new Date(threat.ts * 1000).toLocaleTimeString()}
          </p>
        </div>
      )}

      {/* Red-team injection */}
      <div className="mt-5">
        <div className="flex items-center gap-1.5 font-mono text-[10px] font-bold uppercase tracking-widest text-volt-muted">
          <Gauge className="h-3 w-3 text-state-warn" />
          Inject test attack
        </div>
        <p className="mt-1 text-[10px] leading-relaxed text-volt-muted/80">
          Drives the real twin on ws://localhost:9100/control — the same channel
          the red-team CLI uses.
        </p>

        <div className="mt-3 grid grid-cols-2 gap-2">
          {ATTACKS.map((a) => (
            <button
              key={a.id}
              onClick={() => onInject(a.id)}
              disabled={!node.live || busy !== null}
              title={node.live ? a.blurb : "Disabled for projected nodes"}
              className={`flex items-center justify-center gap-1.5 rounded-xl border px-3 py-2.5 font-mono text-[11px] font-bold uppercase tracking-wider transition-all disabled:cursor-not-allowed disabled:opacity-40 ${
                a.id === "reset"
                  ? "border-volt-green/40 bg-volt-green/10 text-volt-green hover:bg-volt-green/20"
                  : "border-state-warn/40 bg-state-warn/10 text-amber-300 hover:bg-state-warn/20"
              }`}
            >
              {busy === a.id ? (
                <RefreshCw className="h-3.5 w-3.5 animate-spin" />
              ) : (
                a.icon
              )}
              {a.label}
            </button>
          ))}
        </div>

        {ack && (
          <div
            className={`mt-3 rounded-xl border p-2.5 font-mono text-[10px] leading-relaxed ${
              ack.ok
                ? "border-state-healthy/40 bg-state-healthy/10 text-emerald-300"
                : "border-state-critical/40 bg-state-critical/10 text-rose-300"
            }`}
          >
            <span className="flex items-start gap-1.5">
              {ack.ok ? (
                <Network className="mt-px h-3 w-3 shrink-0" />
              ) : (
                <Ban className="mt-px h-3 w-3 shrink-0" />
              )}
              <span>{ack.detail || (ack.ok ? "Trigger applied." : "Trigger failed.")}</span>
            </span>
          </div>
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Small pieces                                                        */
/* ------------------------------------------------------------------ */

function Stat({
  label,
  value,
  hint,
  tone = "normal",
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: "normal" | "warn";
}) {
  return (
    <div className="flex items-start justify-between border-b border-volt-line/70 pb-2">
      <div>
        <div className="text-[11px] text-volt-muted">{label}</div>
        {hint && (
          <div className="font-mono text-[9px] text-volt-muted/60">{hint}</div>
        )}
      </div>
      <div
        className={`font-mono text-sm font-bold tabular-nums ${
          tone === "warn" ? "text-amber-300" : "text-slate-100"
        }`}
      >
        {value}
      </div>
    </div>
  );
}

function Legend({ color, label }: { color: string; label: string }) {
  return (
    <span className="flex items-center gap-1.5 rounded-lg border border-volt-line bg-black/40 px-2 py-1 text-volt-muted">
      <span className="h-1.5 w-1.5 rounded-full" style={{ backgroundColor: color }} />
      {label}
    </span>
  );
}
