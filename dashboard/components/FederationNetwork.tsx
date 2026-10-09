"use client";

/**
 * FederationNetwork — every VoltSentry site and how they cooperate.
 *
 * Data comes only from the federation hub's live feed (useNetworkFeed). With
 * no hub running the view says so and shows how to start one; it never draws
 * sites or threats that are not in the feed.
 */

import React from "react";
import {
  Activity,
  Fingerprint,
  KeyRound,
  Lock,
  LockOpen,
  Radio,
  Server,
  ShieldAlert,
  ShieldCheck,
  Unplug,
  Zap,
} from "lucide-react";
import { useNetworkFeed, NETWORK_FEED_URL } from "../lib/useNetworkFeed";
import { phaseToNeutralV } from "../lib/electrical";
import type { NetworkIndicator, NetworkNode, NetworkSnapshot, NetworkThreat } from "../lib/types";

const STATUS_STYLE: Record<NetworkNode["status"], { stroke: string; pill: string; label: string }> = {
  online: { stroke: "#10B981", pill: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300", label: "ONLINE" },
  stale: { stroke: "#F59E0B", pill: "border-amber-500/40 bg-amber-500/10 text-amber-300", label: "NO HEARTBEAT" },
  offline: { stroke: "#64748B", pill: "border-slate-600 bg-slate-800/60 text-slate-400", label: "OFFLINE" },
};

function fmtAge(sec: number | null | undefined): string {
  if (sec == null) return "—";
  if (sec < 60) return `${sec.toFixed(0)} s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)} min ago`;
  return `${Math.floor(sec / 3600)} h ago`;
}

function fmtTtl(sec: number): string {
  if (sec <= 0) return "expiring";
  const m = Math.floor(sec / 60);
  return m >= 60 ? `${Math.floor(m / 60)} h ${m % 60} m` : `${m} m ${sec % 60} s`;
}

function fmtTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export default function FederationNetwork() {
  const { snapshot, connected } = useNetworkFeed();

  if (!snapshot) {
    return <HubOffline reconnecting={!connected} />;
  }

  const { hub, nodes, iocs, threats, events } = snapshot;
  return (
    <div className="space-y-4">
      <HubBar hub={hub} total={nodes.length} live={connected} />

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-5">
        <div className="xl:col-span-3">
          <TopologyMap nodes={nodes} />
        </div>
        <div className="xl:col-span-2">
          <HowItWorks hub={hub} />
        </div>
      </div>

      <section className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
        {nodes.length === 0 ? (
          <div className="rounded-xl border border-volt-line bg-black/40 p-4 font-mono text-xs text-volt-muted">
            The hub is up but no VoltSentry node has joined yet.
          </div>
        ) : (
          nodes.map((n) => <SiteCard key={n.node_id} node={n} />)
        )}
      </section>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <IndicatorTable iocs={iocs} />
        <GlobalThreats threats={threats} />
      </div>

      <HubLog events={events} />
    </div>
  );
}

/* ------------------------------------------------------------------ */

function HubOffline({ reconnecting }: { reconnecting: boolean }) {
  return (
    <div className="rounded-2xl border border-volt-line bg-black/50 p-6">
      <div className="flex items-center gap-3">
        <Unplug className="h-5 w-5 text-amber-400" />
        <h3 className="font-mono text-sm font-bold uppercase tracking-wider text-slate-100">
          Federation hub not reachable
        </h3>
        {reconnecting && <span className="font-mono text-[10px] text-volt-muted">retrying every 3 s…</span>}
      </div>
      <p className="mt-3 max-w-2xl text-sm leading-relaxed text-volt-muted">
        This view reads the hub at <span className="font-mono text-slate-300">{NETWORK_FEED_URL}</span>. Each site
        keeps protecting its own chargers without it; the hub only joins them into one network. Start a two-site
        network (central CSMS, hub, Vellore and Chennai nodes with their fleets) with:
      </p>
      <pre className="mt-3 overflow-x-auto rounded-lg border border-volt-line bg-black/70 p-3 font-mono text-xs text-emerald-300">
        python scripts/run_network.py
      </pre>
    </div>
  );
}

function HubBar({ hub, total, live }: { hub: NetworkSnapshot["hub"]; total: number; live: boolean }) {
  const cells = [
    { label: "Sites online", value: `${hub.nodes_online} / ${total}` },
    { label: "Messages in", value: hub.messages_in.toLocaleString() },
    { label: "Relayed", value: hub.relayed.toLocaleString() },
    { label: "Auth failures", value: String(hub.auth_failures), warn: hub.auth_failures > 0 },
    { label: "Rejected msgs", value: String(hub.rejected), warn: hub.rejected > 0 },
    { label: "Hub uptime", value: fmtAge(hub.uptime_sec).replace(" ago", "") },
  ];
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-2xl border border-volt-line bg-black/50 p-3 font-mono text-[10px]">
      <div
        className={`flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 font-bold uppercase tracking-wider ${
          live ? STATUS_STYLE.online.pill : STATUS_STYLE.stale.pill
        }`}
      >
        <Radio className="h-3 w-3" />
        {live ? "Hub feed live" : "Hub feed lost"}
      </div>
      {cells.map((c) => (
        <div key={c.label} className="rounded-lg border border-volt-line bg-black/50 px-2.5 py-1.5">
          <div className="uppercase tracking-wider text-volt-muted">{c.label}</div>
          <div className={`mt-0.5 text-xs font-bold tabular-nums ${c.warn ? "text-amber-300" : "text-white"}`}>
            {c.value}
          </div>
        </div>
      ))}
      <div
        className={`flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 font-bold uppercase tracking-wider ${
          hub.tls ? STATUS_STYLE.online.pill : "border-amber-500/40 bg-amber-500/10 text-amber-300"
        }`}
        title={hub.tls ? "Node links use TLS" : "Node links are plaintext; enable --tls-cert outside a lab"}
      >
        {hub.tls ? <Lock className="h-3 w-3" /> : <LockOpen className="h-3 w-3" />}
        {hub.tls ? "TLS" : "No TLS"}
      </div>
      {hub.dev_secret && (
        <div
          className="flex items-center gap-1.5 rounded-lg border border-amber-500/40 bg-amber-500/10 px-2.5 py-1.5 font-bold uppercase tracking-wider text-amber-300"
          title="The hub is using the built-in development master secret"
        >
          <KeyRound className="h-3 w-3" />
          Dev key
        </div>
      )}
    </div>
  );
}

function TopologyMap({ nodes }: { nodes: NetworkNode[] }) {
  const W = 760;
  const H = 340;
  const cx = W / 2;
  const cy = H / 2;
  const rx = 270;
  const ry = 115;
  const placed = nodes.map((n, i) => {
    const angle = Math.PI + (i * 2 * Math.PI) / Math.max(nodes.length, 1);
    return { n, x: cx + rx * Math.cos(angle), y: cy + ry * Math.sin(angle) };
  });

  return (
    <div className="rounded-2xl border border-volt-line bg-black/50 p-3">
      <div className="mb-1 flex items-center justify-between font-mono text-[10px] uppercase tracking-wider text-volt-muted">
        <span>Federation topology</span>
        <span>Every site dials out to the hub · signed messages</span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full" role="img" aria-label="Federation topology">
        {placed.map(({ n, x, y }) => {
          const st = STATUS_STYLE[n.status];
          return (
            <line
              key={`l-${n.node_id}`}
              x1={x}
              y1={y}
              x2={cx}
              y2={cy}
              stroke={st.stroke}
              strokeWidth={n.status === "online" ? 2.2 : 1.4}
              strokeDasharray={n.status === "offline" ? "3 6" : "10 14"}
              strokeOpacity={n.status === "offline" ? 0.5 : 0.85}
              className={n.status === "online" ? "topo-flow" : undefined}
              style={n.status === "online" ? { animationDuration: "1.6s" } : undefined}
            />
          );
        })}

        <g>
          <circle cx={cx} cy={cy} r={46} fill="#0B1220" stroke="#22D3EE" strokeOpacity={0.6} strokeWidth={1.5} />
          <circle cx={cx} cy={cy} r={54} fill="none" stroke="#22D3EE" strokeOpacity={0.15} className="topo-halo" />
          <text x={cx} y={cy - 6} textAnchor="middle" className="fill-cyan-300 font-mono" style={{ fontSize: 12, fontWeight: 700 }}>
            HUB
          </text>
          <text x={cx} y={cy + 10} textAnchor="middle" className="fill-volt-muted font-mono" style={{ fontSize: 8.5 }}>
            threat intel relay
          </text>
        </g>

        {placed.map(({ n, x, y }) => {
          const st = STATUS_STYLE[n.status];
          const loading = n.heartbeat?.loading_pct;
          return (
            <g key={`n-${n.node_id}`}>
              <rect x={x - 92} y={y - 34} width={184} height={68} rx={10} fill="#0B1220" stroke={st.stroke} strokeOpacity={0.8} />
              <text x={x} y={y - 14} textAnchor="middle" className="fill-white font-mono" style={{ fontSize: 11.5, fontWeight: 700 }}>
                {n.node_id}
              </text>
              <text x={x} y={y + 2} textAnchor="middle" className="fill-volt-muted font-mono" style={{ fontSize: 9 }}>
                {n.site_name || "unnamed site"}
              </text>
              <text x={x} y={y + 20} textAnchor="middle" fill={st.stroke} className="font-mono" style={{ fontSize: 9.5, fontWeight: 700 }}>
                {st.label}
                {n.status !== "offline" && loading != null ? ` · ${loading.toFixed(0)}% kVA · ${n.live_sessions} sessions` : ""}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

function HowItWorks({ hub }: { hub: NetworkSnapshot["hub"] }) {
  const items = [
    {
      icon: <Server className="h-4 w-4 text-cyan-300" />,
      title: "One VoltSentry node per site",
      body: "Each charging site runs its own proxy beside its chargers. All detection and quarantine happens there, so a site keeps protecting itself if the hub or the WAN link goes down.",
    },
    {
      icon: <KeyRound className="h-4 w-4 text-cyan-300" />,
      title: "Authenticated, replay-proof links",
      body: `Every message is HMAC-SHA256 signed with that node's own key and carries a rising sequence number and timestamp. ${hub.auth_failures} forged or replayed connection attempt(s) refused so far.`,
    },
    {
      icon: <ShieldAlert className="h-4 w-4 text-cyan-300" />,
      title: "Shared threat intelligence",
      body: "A quarantine or a cloned card at one site becomes an indicator every other site enforces within a second: a blocked charge point is refused at handshake, a blocked card at Authorize.",
    },
    {
      icon: <Fingerprint className="h-4 w-4 text-cyan-300" />,
      title: "Cross-site card cloning",
      body: "One RFID card can only charge one car at a time. Sites share live sessions as salted hashes (never the card number), so the same card live at two sites is caught. No single site could see that.",
    },
  ];
  return (
    <div className="h-full rounded-2xl border border-volt-line bg-black/50 p-4">
      <div className="mb-3 font-mono text-[10px] uppercase tracking-wider text-volt-muted">How the sites cooperate</div>
      <ul className="space-y-3">
        {items.map((it) => (
          <li key={it.title} className="flex gap-3">
            <div className="mt-0.5">{it.icon}</div>
            <div>
              <div className="text-sm font-semibold text-slate-100">{it.title}</div>
              <p className="text-xs leading-relaxed text-volt-muted">{it.body}</p>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

function SiteCard({ node }: { node: NetworkNode }) {
  const st = STATUS_STYLE[node.status];
  const hb = node.heartbeat || {};
  const v = hb.lt_voltage_v;
  const rows: [string, string][] = [
    ["Transformer", node.transformer_kva != null ? `${node.transformer_kva.toFixed(0)} kVA · ${node.bays ?? "?"} bays` : "—"],
    ["Load", hb.load_kva != null ? `${hb.load_kva.toFixed(0)} kVA (${(hb.loading_pct ?? 0).toFixed(0)}%)` : "—"],
    ["To vehicles", hb.output_kw != null ? `${hb.output_kw.toFixed(0)} kW DC` : "—"],
    ["Bus voltage", v != null ? `${v.toFixed(1)} V L-L · ${phaseToNeutralV(v).toFixed(0)} V L-N` : "—"],
    ["Frequency", hb.frequency_hz != null ? `${hb.frequency_hz.toFixed(3)} Hz` : "—"],
    ["Chargers", `${hb.charging ?? 0} charging / ${hb.stations_connected ?? 0} connected`],
    ["Quarantined", String(hb.quarantined ?? 0)],
    ["Threats raised", String(node.threats_total)],
    ["Last heartbeat", fmtAge(node.last_seen_age_sec)],
  ];
  return (
    <div className={`rounded-xl border bg-black/50 p-3 ${node.status === "online" ? "border-volt-line" : "border-amber-500/30"}`}>
      <div className="flex items-start justify-between gap-2 border-b border-volt-line pb-2">
        <div>
          <div className="font-mono text-sm font-bold text-white">{node.node_id}</div>
          <div className="text-xs text-volt-muted">
            {node.site_name}
            {node.region ? ` · ${node.region}` : ""}
          </div>
        </div>
        <span className={`rounded border px-1.5 py-0.5 font-mono text-[9px] font-bold tracking-wider ${st.pill}`}>{st.label}</span>
      </div>
      <dl className="mt-2 space-y-1 font-mono text-[11px]">
        {rows.map(([k, val]) => (
          <div key={k} className="flex justify-between gap-3">
            <dt className="text-volt-muted">{k}</dt>
            <dd
              className={`text-right tabular-nums ${
                k === "Quarantined" && (hb.quarantined ?? 0) > 0 ? "text-rose-300" : "text-slate-200"
              }`}
            >
              {val}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function IndicatorTable({ iocs }: { iocs: NetworkIndicator[] }) {
  return (
    <section className="rounded-2xl border border-volt-line bg-black/50 p-4">
      <h3 className="mb-2 flex items-center gap-2 font-mono text-xs font-bold uppercase tracking-wider text-slate-200">
        <ShieldCheck className="h-4 w-4 text-cyan-300" />
        Shared indicators of compromise ({iocs.length})
      </h3>
      {iocs.length === 0 ? (
        <p className="rounded-lg border border-volt-line bg-black/40 p-3 font-mono text-xs text-volt-muted">
          No active indicators. A quarantine or a cloned card at any site appears here and is enforced by every site.
        </p>
      ) : (
        <div className="max-h-72 overflow-y-auto">
          <table className="w-full font-mono text-[11px]">
            <thead className="text-left text-[9px] uppercase tracking-wider text-volt-muted">
              <tr>
                <th className="pb-1 pr-2">Kind</th>
                <th className="pb-1 pr-2">Value</th>
                <th className="pb-1 pr-2">Reason</th>
                <th className="pb-1 pr-2">From</th>
                <th className="whitespace-nowrap pb-1 text-right">Expires in</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-volt-line/60">
              {iocs.map((i) => (
                <tr key={i.ioc_id} className="align-top">
                  <td className="whitespace-nowrap py-1.5 pr-2 text-slate-300">{i.kind === "id_tag" ? "RFID card" : "Charge point"}</td>
                  <td className="py-1.5 pr-2 text-cyan-300" title={i.kind === "id_tag" ? "salted hash; the card number never leaves the site" : undefined}>
                    {i.kind === "id_tag" ? `#${i.value.slice(0, 10)}…` : i.value}
                  </td>
                  <td className="py-1.5 pr-2 text-slate-300">
                    {i.reason}
                    {i.hits > 1 ? <span className="text-volt-muted"> ×{i.hits}</span> : null}
                  </td>
                  <td className="whitespace-nowrap py-1.5 pr-2 text-slate-400">{i.source_node}</td>
                  <td className="whitespace-nowrap py-1.5 text-right tabular-nums text-slate-400">{fmtTtl(i.ttl_remaining_sec)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function GlobalThreats({ threats }: { threats: NetworkThreat[] }) {
  return (
    <section className="rounded-2xl border border-volt-line bg-black/50 p-4">
      <h3 className="mb-2 flex items-center gap-2 font-mono text-xs font-bold uppercase tracking-wider text-slate-200">
        <Activity className="h-4 w-4 text-rose-400" />
        Network-wide threat feed ({threats.length})
      </h3>
      {threats.length === 0 ? (
        <p className="rounded-lg border border-volt-line bg-black/40 p-3 font-mono text-xs text-volt-muted">
          No threats reported by any site since the hub started.
        </p>
      ) : (
        <ul className="max-h-72 space-y-1.5 overflow-y-auto">
          {threats.map((t, idx) => (
            <li key={`${t.ts}-${idx}`} className="rounded-lg border border-volt-line bg-black/40 px-2.5 py-1.5 font-mono text-[11px]">
              <div className="flex flex-wrap items-center gap-2">
                <span className="rounded border border-cyan-500/30 bg-cyan-500/10 px-1 text-[9px] font-bold text-cyan-300">{t.node_id}</span>
                <span className="text-slate-100">{t.station_id}</span>
                <span className={t.tier === 2 ? "text-amber-300" : "text-rose-300"}>{t.rule_id ?? `ML ${t.ml_score?.toFixed(2)}`}</span>
                <span className="text-volt-muted">{t.action_taken}</span>
                <span className="ml-auto text-volt-muted">{fmtTime(t.ts)}</span>
              </div>
              <div className="mt-0.5 text-slate-400">{t.reason}</div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function HubLog({ events }: { events: NetworkSnapshot["events"] }) {
  if (events.length === 0) return null;
  return (
    <section className="rounded-2xl border border-volt-line bg-black/50 p-4">
      <h3 className="mb-2 flex items-center gap-2 font-mono text-xs font-bold uppercase tracking-wider text-slate-200">
        <Zap className="h-4 w-4 text-volt-green" />
        Hub event log
      </h3>
      <ul className="max-h-48 space-y-1 overflow-y-auto font-mono text-[11px]">
        {events.map((e, idx) => (
          <li key={`${e.ts}-${idx}`} className="flex gap-3">
            <span className="text-volt-muted">{fmtTime(e.ts)}</span>
            <span className={e.level === "alert" ? "text-rose-300" : e.level === "warn" ? "text-amber-300" : "text-slate-300"}>
              {e.text}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}
