"use client";

import React from "react";
import Link from "next/link";
import { useScrollReveal } from "../lib/useScrollReveal";
import { Icon3DShield, Icon3DTransformer } from "./3dIcons";
import {
  ChevronRight,
  ShieldCheck,
  Plug,
  Globe as GlobeIcon,
  ArrowRight,
  Cpu,
  Activity,
  Radio,
  Zap,
  Gauge,
  Lock,
  Server,
} from "lucide-react";

interface StoryLandingProps {
  onLaunchNoc: () => void;
  onOpenLogin: () => void;
}

/** Small numbered eyebrow tag used to mark each story beat. */
function Chapter({ n, label }: { n: string; label: string }) {
  return (
    <div className="story-reveal inline-flex items-center gap-2.5 font-mono text-xs tracking-widest uppercase text-volt-green mb-5">
      <span className="text-volt-muted">{n}</span>
      <span className="w-8 h-px bg-volt-green/50" />
      <span className="font-bold">{label}</span>
    </div>
  );
}

const FLOW = [
  { k: "Authorize", d: "RFID / app token presented" },
  { k: "StartTransaction", d: "CSMS returns a transactionId" },
  { k: "MeterValues", d: "power, SoC, energy — every tick" },
  { k: "StopTransaction", d: "session closes, meter final" },
];

const TIER1 = [
  { id: "R1", name: "State order", d: "MeterValues / StopTransaction with no live Authorize → StartTransaction." },
  { id: "R2", name: "Physics envelope", d: "power > 150 kW, or > 60 kW past 80% SoC (CV taper), or negative power." },
  { id: "R3", name: "Fleet oscillation", d: "≥ 6 start/stop transitions fleet-wide inside a 10-second window." },
  { id: "R4", name: "Session uniqueness", d: "a second handshake for a chargePointId that already holds a live socket." },
  { id: "R5", name: "Transaction integrity", d: "a MeterValues transactionId that is unknown, or owned by another station." },
  { id: "R6", name: "Meter fraud", d: "the energy register pulling away from the reported power faster than any honest session." },
  { id: "R7", name: "Electrical consistency", d: "reported power that disagrees with the meter's own DC voltage × current." },
  { id: "R8", name: "Card clone", d: "an RFID card already charging on another bay, here or at another VoltSentry site." },
  { id: "R9", name: "Federated intel", d: "a card or charge point another site has reported as compromised." },
];

export function StoryLanding({ onLaunchNoc, onOpenLogin }: StoryLandingProps) {
  const rootRef = useScrollReveal<HTMLDivElement>();

  return (
    <div ref={rootRef} className="w-full">
      {/* ================================================================ 00 · HERO */}
      <section className="min-h-[88vh] flex flex-col items-center justify-center text-center max-w-4xl mx-auto px-2 pt-10 pb-20">
        <div className="story-reveal inline-flex items-center gap-2 text-volt-green font-mono text-xs font-bold tracking-widest uppercase mb-8">
          <ShieldCheck className="w-4 h-4" />
          <span>Inline security proxy for EV charging networks</span>
        </div>
        <h1 className="story-reveal story-reveal-delay-1 text-4xl sm:text-6xl md:text-7xl font-black tracking-tight text-white leading-[1.06]">
          Every charger is a socket
          <br />
          <span className="text-volt-green">the grid didn&apos;t know it opened.</span>
        </h1>
        <p className="story-reveal story-reveal-delay-2 mt-7 text-lg sm:text-xl text-volt-muted max-w-2xl leading-relaxed">
          VoltSentry is a software-defined reverse proxy that sits inline between EV
          chargers and the management backend — reading every OCPP frame, and
          quarantining the ones that lie.
        </p>
        <div className="story-reveal story-reveal-delay-3 mt-9 flex flex-col items-center gap-3">
          <span className="font-mono text-[11px] text-volt-muted tracking-widest uppercase">Scroll to see how</span>
          <span className="w-5 h-8 rounded-full border border-volt-green/40 flex items-start justify-center p-1">
            <span className="w-1 h-2 rounded-full bg-volt-green animate-volt-pulse-line" />
          </span>
        </div>
      </section>

      {/* ================================================================ 01 · THE PROBLEM */}
      <section className="max-w-4xl mx-auto px-2 py-24">
        <Chapter n="01" label="The problem" />
        <h2 className="story-reveal text-3xl sm:text-5xl font-black text-white leading-tight max-w-3xl">
          Public charging runs on an open, mostly unmonitored protocol.
        </h2>
        <p className="story-reveal story-reveal-delay-1 mt-6 text-lg text-volt-muted max-w-2xl leading-relaxed">
          OCPP 1.6-J travels over plain WebSockets. Chargers are physically exposed,
          often weakly authenticated, and cloud backends audit traffic{" "}
          <span className="text-volt-text font-semibold">after the fact</span> — batch
          analysis minutes or hours later, long after an active attack has already
          moved power or money. The dangerous window is the seconds nobody is watching.
        </p>
      </section>

      {/* ================================================================ EV · WHAT A CHARGER IS ON THE WIRE */}
      <section className="max-w-5xl mx-auto px-2 py-24">
        <Chapter n="02" label="What an EV charger really is" />
        <div className="grid lg:grid-cols-2 gap-10 items-start">
          <div>
            <h2 className="story-reveal text-3xl sm:text-5xl font-black text-white leading-tight">
              A charger is just a client
              <span className="text-volt-green"> speaking JSON.</span>
            </h2>
            <p className="story-reveal story-reveal-delay-1 mt-6 text-lg text-volt-muted leading-relaxed">
              To the network, an EV charge point is an OCPP <em>client</em>: it dials the
              management system over a WebSocket and exchanges JSON arrays —{" "}
              <code className="text-volt-green font-mono text-sm">[2, id, action, payload]</code>{" "}
              for a call, <code className="text-volt-green font-mono text-sm">[3, id, payload]</code>{" "}
              for a reply. It boots, it heartbeats, it authorizes a driver, it opens a
              transaction, and then it streams meter readings — power, state-of-charge,
              cumulative energy — as plain strings on the wire.
            </p>
            <p className="story-reveal story-reveal-delay-2 mt-4 text-lg text-volt-muted leading-relaxed">
              Nothing in that stream proves the numbers are true. A compromised charger
              reports whatever it wants. There are{" "}
              <span className="text-volt-text font-semibold">hundreds of thousands</span> of
              these endpoints live worldwide — every one an OCPP socket someone can talk to.
            </p>
            <Link
              href="/globe"
              className="story-reveal story-reveal-delay-3 mt-7 inline-flex items-center gap-2 px-5 py-3 rounded-xl bg-volt-green/10 border border-volt-green/40 text-volt-green font-bold text-sm hover:bg-volt-green/20 transition-all"
            >
              <GlobeIcon className="w-4 h-4" />
              <span>Explore the global charging network</span>
              <ArrowRight className="w-4 h-4" />
            </Link>
          </div>

          <div className="story-reveal story-reveal-delay-1 p-6 rounded-3xl glass-panel-dense font-mono text-xs space-y-3">
            <div className="flex items-center gap-2 text-volt-muted uppercase tracking-widest text-[10px] pb-2 border-b border-volt-line">
              <Plug className="w-3.5 h-3.5 text-volt-green" /> live OCPP frame · MeterValues
            </div>
            <pre className="text-volt-text leading-relaxed overflow-x-auto whitespace-pre-wrap">{`[2, "a3f1", "MeterValues", {
  "connectorId": 1,
  "transactionId": 1041,
  "meterValue": [{
    "sampledValue": [
      { "value": "120.4", "measurand": "Power.Active.Import" },
      { "value": "18.72", "measurand": "Energy.Active.Import.Register" },
      { "value": "64.0",  "measurand": "SoC" }
    ]
  }]
}]`}</pre>
            <div className="text-volt-muted text-[11px] pt-1">
              ↑ strings on the wire. VoltSentry casts, validates, and scores every one.
            </div>
          </div>
        </div>
      </section>

      {/* ================================================================ 03 · WHAT VOLTSENTRY IS */}
      <section className="max-w-5xl mx-auto px-2 py-24">
        <Chapter n="03" label="What VoltSentry is" />
        <h2 className="story-reveal text-3xl sm:text-5xl font-black text-white leading-tight max-w-3xl">
          A transparent guard on the transport layer.
        </h2>
        <p className="story-reveal story-reveal-delay-1 mt-6 text-lg text-volt-muted max-w-2xl leading-relaxed">
          It accepts charger connections, forwards clean traffic to the backend
          untouched, and inspects every frame in between. Station identity comes from
          the URL path, not a payload field — so a frame can&apos;t lie about who sent it.
        </p>
        {/* topology */}
        <div className="story-reveal story-reveal-delay-2 mt-12 grid grid-cols-3 gap-2 sm:gap-4 items-stretch">
          <div className="p-5 rounded-2xl glass-panel flex flex-col items-center text-center gap-3">
            <Plug className="w-8 h-8 text-volt-green" />
            <div className="text-sm font-bold text-white">Charger fleet</div>
            <div className="font-mono text-[10px] text-volt-muted">ws · client</div>
          </div>
          <div className="p-5 rounded-2xl glass-panel flex flex-col items-center text-center gap-3">
            <Icon3DShield className="w-9 h-9" />
            <div className="text-sm font-bold text-white">VoltSentry proxy</div>
            <div className="font-mono text-[10px] text-volt-muted">:8000 → :8100</div>
          </div>
          <div className="p-5 rounded-2xl glass-panel flex flex-col items-center text-center gap-3">
            <Server className="w-8 h-8 text-volt-green" />
            <div className="text-sm font-bold text-white">CSMS backend</div>
            <div className="font-mono text-[10px] text-volt-muted">:9000</div>
          </div>
        </div>
      </section>

      {/* ================================================================ 04 · HEALTHY SESSION FLOW */}
      <section className="max-w-5xl mx-auto px-2 py-24">
        <Chapter n="04" label="A healthy session" />
        <h2 className="story-reveal text-3xl sm:text-5xl font-black text-white leading-tight max-w-3xl">
          This is what normal looks like.
        </h2>
        <p className="story-reveal story-reveal-delay-1 mt-6 text-lg text-volt-muted max-w-2xl leading-relaxed">
          Every legitimate charge walks the same finite-state path. VoltSentry tracks it
          per station — a session is only &ldquo;live&rdquo; once an Authorize has been
          followed by a StartTransaction that returned a transactionId.
        </p>
        <div className="mt-12 flex flex-col sm:flex-row items-stretch gap-3">
          {FLOW.map((step, i) => (
            <React.Fragment key={step.k}>
              <div
                className={`story-reveal story-reveal-delay-${Math.min(i, 3)} flex-1 p-5 rounded-2xl glass-panel`}
              >
                <div className="font-mono text-[10px] text-volt-muted mb-2">STEP {i + 1}</div>
                <div className="font-mono font-bold text-volt-green text-sm">{step.k}</div>
                <div className="text-xs text-volt-muted mt-2 leading-relaxed">{step.d}</div>
              </div>
              {i < FLOW.length - 1 && (
                <div className="hidden sm:flex items-center">
                  <ArrowRight className="w-5 h-5 text-volt-green/50" />
                </div>
              )}
            </React.Fragment>
          ))}
        </div>
      </section>

      {/* ================================================================ 05 · TIER 1 */}
      <section className="max-w-5xl mx-auto px-2 py-24">
        <Chapter n="05" label="Tier 1 · deterministic rules" />
        <h2 className="story-reveal text-3xl sm:text-5xl font-black text-white leading-tight max-w-3xl">
          The loud attacks die on physics and state.
        </h2>
        <p className="story-reveal story-reveal-delay-1 mt-6 text-lg text-volt-muted max-w-2xl leading-relaxed">
          Nine pure-function rules judge every frame against the FSM, the CC-CV
          charging envelope and the meter's own electrical readings. No model, no
          network hop — just deterministic checks.
        </p>

        {/* pinned measured stat */}
        <div className="story-reveal story-reveal-delay-2 my-12 p-8 rounded-3xl glass-panel-dense text-center">
          <div className="text-6xl sm:text-7xl font-black font-mono stat-numeral">~16&thinsp;µs</div>
          <div className="mt-3 text-sm text-volt-text font-semibold">
            mean Tier-1 evaluation, per frame
          </div>
          <div className="mt-1 font-mono text-[11px] text-volt-muted">
            measured over 200,000 MeterValues frames (11 measurands, ~1&thinsp;KB) · median ~16&thinsp;µs · p99 ~19&thinsp;µs · no upstream round-trip
          </div>
        </div>

        <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {TIER1.map((r, i) => (
            <div key={r.id} className={`story-reveal story-reveal-delay-${Math.min(i, 3)} p-5 rounded-2xl glass-panel`}>
              <div className="flex items-center gap-2 mb-2">
                <span className="font-mono font-black text-volt-green text-sm">{r.id}</span>
                <span className="font-bold text-white text-sm">{r.name}</span>
              </div>
              <p className="text-xs text-volt-muted leading-relaxed">{r.d}</p>
            </div>
          ))}
        </div>
      </section>

      {/* ================================================================ 06 · TIER 2 (honest) */}
      <section className="max-w-5xl mx-auto px-2 py-24">
        <Chapter n="06" label="Tier 2 · behavioural ML" />
        <div className="grid lg:grid-cols-2 gap-10 items-start">
          <div>
            <h2 className="story-reveal text-3xl sm:text-5xl font-black text-white leading-tight">
              Some fraud stays inside every threshold.
            </h2>
            <p className="story-reveal story-reveal-delay-1 mt-6 text-lg text-volt-muted leading-relaxed">
              A slow, compounding under-report never trips a hard rule — each single
              frame looks legal. So VoltSentry derives a 5-dimensional feature vector per
              session and feeds it to an Isolation Forest, alerting past a score of{" "}
              <span className="text-volt-green font-bold">0.65</span>.
            </p>
            <div className="story-reveal story-reveal-delay-2 mt-6 p-4 rounded-xl border border-volt-line bg-volt-surface/60 font-mono text-xs text-volt-muted leading-relaxed">
              <span className="text-volt-green">note ·</span> the feature layer runs in the
              live proxy today; the forest scoring is the next build step. The demo replays
              recorded scores so you can watch the curve climb.
            </div>
          </div>
          <div className="story-reveal story-reveal-delay-1 p-6 rounded-3xl glass-panel-dense space-y-4">
            <div className="font-mono text-[10px] text-volt-muted uppercase tracking-widest">feature vector</div>
            <ul className="space-y-2 font-mono text-sm">
              {["power_kw", "soc", "dp_dt", "duration_sec", "energy_residual_kwh"].map((f, i) => (
                <li key={f} className="flex items-center gap-3">
                  <span className="text-volt-muted text-xs w-4">{i}</span>
                  <span className={f === "energy_residual_kwh" ? "text-volt-green font-bold" : "text-volt-text"}>{f}</span>
                </li>
              ))}
            </ul>
            <p className="text-xs text-volt-muted leading-relaxed pt-2 border-t border-volt-line">
              The last one is the tell:{" "}
              <span className="text-volt-text">register − ∫ reported&nbsp;power · dt</span>. Under a
              drift attack it climbs monotonically while every raw value still looks fine — and
              the forest fires.
            </p>
          </div>
        </div>
      </section>

      {/* ================================================================ 07 · QUARANTINE */}
      <section className="max-w-4xl mx-auto px-2 py-24">
        <Chapter n="07" label="Quarantine in action" />
        <h2 className="story-reveal text-3xl sm:text-5xl font-black text-white leading-tight max-w-3xl">
          The moment a station lies, it&apos;s severed.
        </h2>
        <div className="mt-10 space-y-3">
          {[
            { icon: Zap, t: "Drop the frame", d: "the forged MeterValues never reaches the backend." },
            { icon: Lock, t: "ChangeAvailability → Inoperative", d: "the proxy speaks a CSMS→charger call downstream to take the connector offline." },
            { icon: Radio, t: "Wait for the ack", d: "the charger replies with a CALLRESULT; a 3-second deadline closes it anyway if it stays silent." },
            { icon: ShieldCheck, t: "Close 4001", d: "the socket is severed. R3/R4 only alert — quarantining fleet-wide rules would sever all eight stations at once." },
          ].map((s, i) => (
            <div key={s.t} className={`story-reveal story-reveal-delay-${Math.min(i, 3)} flex items-start gap-4 p-5 rounded-2xl glass-panel`}>
              <div className="p-2.5 rounded-xl bg-volt-green/10 border border-volt-green/30 text-volt-green">
                <s.icon className="w-5 h-5" />
              </div>
              <div>
                <div className="font-bold text-white text-sm">{s.t}</div>
                <div className="text-xs text-volt-muted mt-1 leading-relaxed">{s.d}</div>
              </div>
            </div>
          ))}
        </div>
      </section>

      {/* ================================================================ 08 · $0 HARDWARE */}
      <section className="max-w-4xl mx-auto px-2 py-24">
        <Chapter n="08" label="Cost" />
        <h2 className="story-reveal text-3xl sm:text-5xl font-black text-white leading-tight max-w-3xl">
          Zero new hardware. It&apos;s all software.
        </h2>
        <p className="story-reveal story-reveal-delay-1 mt-6 text-lg text-volt-muted max-w-2xl leading-relaxed">
          No sensors, no retrofit chip shipped to millions of deployed stations. VoltSentry
          is a drop-in process in front of an existing backend, validated against a
          software-defined cyber-physical twin running on localhost, scoring with a local
          scikit-learn model. No cloud, no per-station cost.
        </p>
        <div className="story-reveal story-reveal-delay-2 mt-10 flex flex-wrap gap-4">
          {[
            { icon: Cpu, k: "$0", v: "added hardware / station" },
            { icon: Server, k: "1", v: "process, drop-in inline" },
            { icon: Gauge, k: "localhost", v: "twin — no cloud, no DB" },
          ].map((s) => (
            <div key={s.v} className="flex-1 min-w-[160px] p-6 rounded-2xl glass-panel">
              <s.icon className="w-6 h-6 text-volt-green mb-3" />
              <div className="text-3xl font-black font-mono text-white">{s.k}</div>
              <div className="text-xs text-volt-muted mt-1">{s.v}</div>
            </div>
          ))}
        </div>
      </section>

      {/* ================================================================ 09 · SCALE / STAKES */}
      <section className="max-w-4xl mx-auto px-2 py-24">
        <Chapter n="09" label="The stakes" />
        <h2 className="story-reveal text-3xl sm:text-5xl font-black text-white leading-tight max-w-3xl">
          One charger is fraud. A fleet is a grid event.
        </h2>
        <div className="mt-10 grid md:grid-cols-2 gap-5">
          <div className="story-reveal p-7 rounded-3xl glass-panel">
            <div className="text-volt-green font-mono text-xs font-bold uppercase tracking-widest mb-3">single station</div>
            <p className="text-volt-muted leading-relaxed text-sm">
              A spoofed meter bills a session near zero while real power flows — quiet,
              per-socket theft. Caught deterministically by R2 the instant the physics
              breaks.
            </p>
          </div>
          <div className="story-reveal story-reveal-delay-1 p-7 rounded-3xl glass-panel-dense">
            <div className="flex items-center gap-2 mb-3">
              <Icon3DTransformer className="w-6 h-6" />
              <div className="text-volt-green font-mono text-xs font-bold uppercase tracking-widest">coordinated fleet</div>
            </div>
            <p className="text-volt-muted leading-relaxed text-sm">
              Synchronised rapid start/stop across a cluster swings load on the site's shared{" "}
              <span className="text-volt-text font-semibold">1250&nbsp;kVA</span> transformer
              (11&nbsp;kV in, 415&nbsp;V three-phase out) — the exact aggregate VoltSentry tracks
              in kVA, with measured bus voltage and line current, and R3 flags fleet-wide.
            </p>
          </div>
        </div>
      </section>

      {/* ================================================================ 10 · CTA */}
      <section className="max-w-3xl mx-auto px-2 py-28 text-center">
        <h2 className="story-reveal text-4xl sm:text-6xl font-black text-white leading-tight">
          See it happen.
        </h2>
        <p className="story-reveal story-reveal-delay-1 mt-5 text-lg text-volt-muted max-w-xl mx-auto leading-relaxed">
          Open the live NOC console, drive the four attacks, and watch the proxy detect
          and quarantine them in real time.
        </p>
        <div className="story-reveal story-reveal-delay-2 mt-9 flex flex-wrap items-center justify-center gap-4">
          <button
            onClick={onLaunchNoc}
            className="px-8 py-4 rounded-xl bg-volt-green text-black hover:bg-volt-green-dim font-black text-sm uppercase tracking-wider transition-all shadow-volt-glow flex items-center gap-2 transform hover:-translate-y-0.5 active:translate-y-0"
          >
            <span>Launch NOC Dashboard</span>
            <ChevronRight className="w-4 h-4 stroke-[3]" />
          </button>
          <button
            onClick={onOpenLogin}
            className="px-8 py-4 rounded-xl bg-volt-surface text-white hover:bg-volt-elevated border border-volt-line font-bold text-sm uppercase tracking-wider transition-all"
          >
            Operator SSO Login
          </button>
        </div>
        <div className="story-reveal story-reveal-delay-3 mt-6">
          <Link href="/globe" className="font-mono text-xs text-volt-muted hover:text-volt-green transition-colors inline-flex items-center gap-1.5">
            <GlobeIcon className="w-3.5 h-3.5" /> or explore the global charging network first
          </Link>
        </div>
      </section>
    </div>
  );
}

export default StoryLanding;
