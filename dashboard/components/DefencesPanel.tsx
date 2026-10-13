"use client";

/**
 * DefencesPanel — what VoltSentry blocks, and how this site is set up.
 *
 * Answers two questions on one screen: "what firewall are you using?" (the flow
 * diagram: VoltSentry is the application-level firewall between the chargers and
 * the CSMS) and "what else protects the network?" (one card per safeguard).
 *
 * Counters come from the proxy's GridEvent.defences, since the proxy started.
 * Site-to-site figures come from the federation hub's feed. Nothing here is
 * simulated: with no live data a value shows "—", and safeguards that are not
 * built yet are shown as planned.
 */

import React from "react";
import {
  ArrowDown,
  ArrowRight,
  Ban,
  Copy,
  CreditCard,
  Database,
  Eye,
  FileText,
  Fingerprint,
  Hourglass,
  KeyRound,
  Lock,
  LockOpen,
  Network,
  Plug,
  Radio,
  Router,
  Ruler,
  ScanLine,
  Server,
  ShieldCheck,
  ShieldX,
  Unplug,
} from "lucide-react";
import { useNetworkFeed } from "../lib/useNetworkFeed";
import type { DefenceStats, GridEvent, ThreatEvent } from "../lib/types";

type Tone = "good" | "lab" | "planned" | "alert" | "neutral";

const TONE: Record<Tone, string> = {
  good: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
  lab: "border-amber-500/40 bg-amber-500/10 text-amber-300",
  planned: "border-slate-600 bg-slate-800/60 text-slate-400",
  alert: "border-rose-500/40 bg-rose-500/10 text-rose-300",
  neutral: "border-volt-line-strong bg-black/40 text-slate-200",
};

function n(value: number | undefined | null): string {
  return value == null ? "—" : value.toLocaleString();
}

interface Props {
  grid: GridEvent | null;
  threats: ThreatEvent[];
  live: boolean;
}

export default function DefencesPanel({ grid, threats, live }: Props) {
  const d = grid?.defences ?? null;
  const { snapshot, connected: hubConnected } = useNetworkFeed();
  const hub = hubConnected ? snapshot?.hub ?? null : null;
  const monitor = grid?.enforcement === "monitor";
  const refused = d
    ? d.duplicate_sessions_refused + d.blocklisted_chargers_refused + d.oversize_frames_refused + d.malformed_connections_refused
    : null;

  return (
    <div className="space-y-4">
      {!d && (
        <div className="rounded-xl border border-amber-500/40 bg-amber-500/10 p-3 font-mono text-xs text-amber-200">
          {live
            ? "Waiting for the proxy's counters. They arrive once a second from a running VoltSentry node (v3)."
            : "Demo replay is a recording: live counters appear on the Live Charger Stream."}
        </div>
      )}

      <FlowDiagram d={d} refused={refused} monitor={monitor} />

      <Group title="At the connection" note="before a single OCPP message is accepted">
        <Card icon={<Copy className="h-4 w-4" />} title="Duplicate connections refused" rule="R4"
          value={n(d?.duplicate_sessions_refused)} tone={d?.duplicate_sessions_refused ? "alert" : "good"}
          text="A second connection claiming a charger that is already online is closed (4001): no session hijack." />
        <Card icon={<Ban className="h-4 w-4" />} title="Blocklisted chargers refused" rule="R9"
          value={n(d?.blocklisted_chargers_refused)} tone={d?.blocklisted_chargers_refused ? "alert" : "good"}
          text="A charger quarantined at another site is refused here for 15 minutes (4003)." />
        <Card icon={<Ruler className="h-4 w-4" />} title="Oversized messages refused"
          value={n(d?.oversize_frames_refused)} tone={d?.oversize_frames_refused ? "alert" : "good"}
          badge={d ? `limit ${d.max_frame_kb} KB` : undefined}
          text="An OCPP message is a few hundred bytes. Anything bigger is an attack on memory: the connection is closed (1009)." />
        <Card icon={<Unplug className="h-4 w-4" />} title="Malformed connections refused"
          value={n(d?.malformed_connections_refused)} tone={d?.malformed_connections_refused ? "alert" : "good"}
          text="A connection without a charger identity in its address (/ocpp/{id}) is closed at once (1008)." />
        <Card icon={d?.ingress_tls ? <Lock className="h-4 w-4" /> : <LockOpen className="h-4 w-4" />} title="Charger link encryption"
          value={d ? (d.ingress_tls ? "TLS on" : "TLS off") : "—"} tone={d?.ingress_tls ? "good" : "lab"}
          badge={d && !d.ingress_tls ? "lab set-up" : undefined}
          text="Chargers can connect over wss:// (TLS 1.2+). Off on this laptop demo; --tls-cert / --tls-key turn it on." />
        <Card icon={<Router className="h-4 w-4" />} title="Listening on"
          value={d ? (d.bind === "localhost" || d.bind === "127.0.0.1" ? "this machine only" : d.bind) : "—"}
          tone={d && (d.bind === "localhost" || d.bind === "127.0.0.1") ? "good" : "neutral"}
          text="Default is localhost; a site opens it to the charger LAN with --bind, behind its network firewall." />
      </Group>

      <Group title="On every message" note="inspected before it is forwarded">
        <Card icon={<ScanLine className="h-4 w-4" />} title="Detection on every frame" rule="R1–R9 + ML"
          value={n(d?.frames_inspected)} tone="good" badge="inspected"
          text="Protocol order, physics, V × I, energy conservation and the behavioural model, about 16 µs per frame." />
        <Card icon={<ShieldX className="h-4 w-4" />} title="Messages held back from the CSMS"
          value={n(d?.frames_dropped)} tone={d?.frames_dropped ? "alert" : "good"}
          text="A forged reading or a cloned card never reaches billing. Clean traffic passes untouched." />
        <Card icon={<Plug className="h-4 w-4" />} title="Chargers quarantined" rule="R2 R5 R6 R7"
          value={n(d?.quarantines)} tone={d?.quarantines ? "alert" : "good"}
          text="A charger that lies about its meter is told to go Inoperative, then disconnected." />
        <Card icon={<CreditCard className="h-4 w-4" />} title="Cloned cards blocked" rule="R8 R9"
          value={n(d?.cards_blocked)} tone={d?.cards_blocked ? "alert" : "good"}
          text="VoltSentry answers the card Blocked itself; the CSMS never sees it. The real owner gets a new card." />
      </Group>

      <Group title="Between sites" note="from the federation hub">
        <Card icon={<KeyRound className="h-4 w-4" />} title="Signed site links" rule="HMAC-SHA256"
          value={hub ? `${n(hub.auth_failures)} forged refused` : d?.federation === "standalone" ? "standalone site" : "—"}
          tone={!hub ? "planned" : hub.dev_secret || d?.federation_dev_key ? "lab" : "good"}
          badge={hub && (hub.dev_secret || d?.federation_dev_key) ? "dev key" : undefined}
          text="Every message between sites is signed with that site's own key, so one leaked key cannot speak for another site." />
        <Card icon={<Hourglass className="h-4 w-4" />} title="Replay protection"
          value={hub ? `${n(hub.rejected)} rejected` : "—"} tone={hub ? "good" : "planned"}
          text="Rising sequence numbers and a ±120 s time window: a recorded message cannot be sent again." />
        <Card icon={hub?.tls ? <Lock className="h-4 w-4" /> : <LockOpen className="h-4 w-4" />} title="Hub link encryption"
          value={hub ? (hub.tls ? "TLS on" : "TLS off") : "—"} tone={!hub ? "planned" : hub.tls ? "good" : "lab"}
          badge={hub && !hub.tls ? "lab set-up" : undefined}
          text="Sites dial out to the hub over wss://, so a site behind NAT needs no open port." />
        <Card icon={<Fingerprint className="h-4 w-4" />} title="Card numbers never leave a site"
          value="salted hashes" tone="good"
          text="Sites share HMAC(salt, card) only: enough to spot one card at two sites, never the number." />
        <Card icon={<Database className="h-4 w-4" />} title="Shared blocklist" rule="expires"
          value={snapshot && hubConnected ? `${snapshot.iocs.length} active` : "—"} tone={hub ? "good" : "planned"}
          text="Chargers stay blocked network-wide for 15 min, cards for 1 h, then drop off on their own." />
      </Group>

      <Group title="Operations" note="how the protection is run">
        <Card icon={<Eye className="h-4 w-4" />} title="Mode"
          value={grid?.enforcement ? (monitor ? "monitor" : "enforce") : "—"} tone={monitor ? "lab" : "good"}
          text="A new site starts in monitor mode: everything is reported, nothing is blocked, until its false alarms are known." />
        <Card icon={<FileText className="h-4 w-4" />} title="Forensic log"
          value={d ? `${n(d.threats_recorded)} incidents` : "—"} tone="good"
          text="Every detection is written with its raw frame (logs/incidents.jsonl) and can be exported from this console." />
        <Card icon={<Radio className="h-4 w-4" />} title="Works without the hub"
          value={d ? { standalone: "standalone", connected: "hub connected", disconnected: "protecting locally" }[d.federation] : "—"}
          tone={d?.federation === "disconnected" ? "lab" : "good"}
          text="Detection never waits on the hub. If it goes down, each site keeps checking and quarantining on its own." />
      </Group>

      <Group title="Not built yet" note="said plainly">
        <Card icon={<KeyRound className="h-4 w-4" />} title="Charger passwords and certificates" rule="OCPP profiles 1–3"
          value="planned" tone="planned"
          text="OCPP security profiles (basic auth, client certificates). TLS already works; charger authentication is next." />
        <Card icon={<Server className="h-4 w-4" />} title="Console login and feed authentication"
          value="planned" tone="planned"
          text="The login screen is a demo gate, not real authentication, and the feeds are read-only. Keep them on localhost." />
      </Group>

      <RecentBlocks threats={threats} />
    </div>
  );
}

function FlowDiagram({ d, refused, monitor }: { d: DefenceStats | null; refused: number | null; monitor: boolean }) {
  return (
    <section className="rounded-2xl border border-volt-line bg-black/50 p-4">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="font-mono text-xs font-bold uppercase tracking-wider text-slate-100">
          Where the firewall sits
        </h3>
        <span className="font-mono text-[10px] uppercase tracking-wider text-volt-muted">
          live, since the proxy started
        </span>
      </div>

      <div className="flex flex-col items-stretch gap-2 lg:flex-row lg:items-center">
        <Node title="Chargers" sub="CP-01 … CP-08" detail="OCPP 1.6-J over WebSocket" />
        <Arrow />
        <Node title="Site network firewall" sub="IP address and port only" detail="the site's own router / firewall: only chargers reach VoltSentry"
          dashed />
        <Arrow />
        <div className="flex-[1.6] rounded-xl border-2 border-emerald-500/60 bg-emerald-500/[0.07] p-3">
          <div className="flex items-center gap-2">
            <ShieldCheck className="h-5 w-5 text-emerald-400" />
            <div className="font-display text-sm font-black text-white">VoltSentry · application firewall</div>
          </div>
          <div className="mt-1 text-[11px] leading-snug text-volt-muted">
            Reads every OCPP message: protocol, physics, V × I, energy, ML. Built by us, no third-party product.
            {monitor && " Monitor mode: reporting only, nothing is held back."}
          </div>
          <div className="mt-2 grid grid-cols-3 gap-2 font-mono">
            <Count label="inspected" value={n(d?.frames_inspected)} tone="text-cyan-300" />
            <Count label="forwarded" value={n(d?.frames_forwarded)} tone="text-emerald-300" />
            <Count label="held back" value={n(d?.frames_dropped)} tone="text-rose-300" />
          </div>
          <div className="mt-2 flex items-center gap-1.5 font-mono text-[10px] text-rose-300">
            <ArrowDown className="h-3 w-3" />
            <span>
              {n(refused)} connection{refused === 1 ? "" : "s"} refused · {n(d?.quarantines)} quarantined ·{" "}
              {n(d?.cards_blocked)} card{d?.cards_blocked === 1 ? "" : "s"} blocked
            </span>
          </div>
        </div>
        <Arrow label="clean only" />
        <Node title="CSMS" sub="operator's backend" detail="billing and charger management, unchanged" />
      </div>

      <p className="mt-3 text-[11px] leading-relaxed text-volt-muted">
        <Network className="mr-1 inline h-3 w-3 align-[-2px]" />
        A network firewall decides by address and port, so it cannot see a charger reporting 5 kW while drawing 120 kW.
        VoltSentry reads inside the messages. A real site uses both: its own firewall lets only chargers reach VoltSentry,
        and only VoltSentry reach the CSMS (site configuration, not shown live here).
      </p>
    </section>
  );
}

function Node({ title, sub, detail, dashed }: { title: string; sub: string; detail: string; dashed?: boolean }) {
  return (
    <div className={`flex-1 rounded-xl border ${dashed ? "border-dashed border-volt-line-strong" : "border-volt-line-strong"} bg-black/40 p-3`}>
      <div className="font-display text-sm font-bold text-slate-100">{title}</div>
      <div className="font-mono text-[10px] uppercase tracking-wider text-volt-muted">{sub}</div>
      <div className="mt-1 text-[11px] leading-snug text-slate-400">{detail}</div>
    </div>
  );
}

function Arrow({ label }: { label?: string }) {
  return (
    <div className="flex flex-col items-center justify-center text-volt-muted">
      <ArrowRight className="hidden h-5 w-5 lg:block" />
      <ArrowDown className="h-5 w-5 lg:hidden" />
      {label && <span className="font-mono text-[9px] uppercase tracking-wider">{label}</span>}
    </div>
  );
}

function Count({ label, value, tone }: { label: string; value: string; tone: string }) {
  return (
    <div className="rounded-lg border border-volt-line bg-black/50 px-2 py-1.5">
      <div className={`text-lg font-bold leading-none ${tone}`}>{value}</div>
      <div className="mt-1 text-[9px] uppercase tracking-wider text-volt-muted">{label}</div>
    </div>
  );
}

function Group({ title, note, children }: { title: string; note: string; children: React.ReactNode }) {
  return (
    <section>
      <div className="mb-2 flex items-baseline gap-2">
        <h3 className="font-mono text-xs font-bold uppercase tracking-wider text-slate-200">{title}</h3>
        <span className="font-mono text-[10px] text-volt-muted">{note}</span>
      </div>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">{children}</div>
    </section>
  );
}

function Card({
  icon, title, rule, value, tone, badge, text,
}: {
  icon: React.ReactNode;
  title: string;
  rule?: string;
  value: string;
  tone: Tone;
  badge?: string;
  text: string;
}) {
  return (
    <div className="rounded-xl border border-volt-line bg-black/40 p-3">
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-2 text-slate-100">
          <span className="text-volt-muted">{icon}</span>
          <span className="text-sm font-bold leading-tight">{title}</span>
        </div>
        {rule && <span className="shrink-0 font-mono text-[10px] text-cyan-300">{rule}</span>}
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <span className={`rounded-md border px-2 py-0.5 font-mono text-xs font-bold ${TONE[tone]}`}>{value}</span>
        {badge && <span className="rounded-md border border-volt-line px-1.5 py-0.5 font-mono text-[10px] text-volt-muted">{badge}</span>}
      </div>
      <p className="mt-2 text-[11px] leading-snug text-volt-muted">{text}</p>
    </div>
  );
}

function RecentBlocks({ threats }: { threats: ThreatEvent[] }) {
  const blocked = threats.filter((t) => t.action_taken !== "logged" || t.rule_id === "R4_SESSION_UNIQUE").slice(0, 6);
  return (
    <section className="rounded-2xl border border-volt-line bg-black/50 p-4">
      <h3 className="mb-2 font-mono text-xs font-bold uppercase tracking-wider text-slate-200">Last things blocked</h3>
      {blocked.length === 0 ? (
        <div className="font-mono text-xs text-volt-muted">Nothing blocked yet in this session.</div>
      ) : (
        <ul className="space-y-1.5">
          {blocked.map((t, i) => (
            <li key={`${t.ts}-${i}`} className="flex flex-wrap items-center gap-2 font-mono text-[11px]">
              <span className="text-volt-muted">{new Date(t.ts * 1000).toLocaleTimeString()}</span>
              <span className="text-slate-200">{t.station_id}</span>
              <span className="text-cyan-300">{t.rule_id ?? "ML"}</span>
              <span className={`rounded border px-1.5 ${TONE.alert}`}>
                {t.rule_id === "R4_SESSION_UNIQUE" ? "refused" : t.action_taken.replace("_", " ")}
              </span>
              <span className="truncate text-volt-muted">{t.reason}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
