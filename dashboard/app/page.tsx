"use client";

import React, { useState, useCallback } from "react";
import dynamic from "next/dynamic";
import { useFeed } from "../lib/useFeed";
import { useAudioAlert } from "../lib/useAudioAlert";
import { useVoiceAlert } from "../lib/useVoiceAlert";
import { GridEvent, StationStatus, TelemetryEvent, ThreatEvent } from "../lib/types";
import { useGridFrequency } from "../lib/useGridFrequency";
import { phaseToNeutralV } from "../lib/electrical";
import { useSyntheticFleet } from "../lib/useSyntheticFleet";
import { WaveBackground } from "../components/WaveBackground";
import { PowerChart } from "../components/PowerChart";
import { TransformerGauge } from "../components/TransformerGauge";
import { ThreatBadge } from "../components/ThreatBadge";
import { Footer } from "../components/Footer";
import { StoryLanding } from "../components/StoryLanding";
import { LoginModal } from "../components/LoginModal";
import { LoadingScreen } from "../components/LoadingScreen";
import { AccountView } from "../components/AccountView";
import { SettingsView } from "../components/SettingsView";
import {
  Icon3DShield,
} from "../components/3dIcons";
import { Play, Volume2, Radio, Zap, Download, ShieldAlert, User, Settings, LayoutDashboard, Globe, Box, Loader2, LogOut, Network, Activity, Layers, Minus, Plus, Gauge, Cpu } from "lucide-react";
import { useRouter } from "next/navigation";
import { RequireAuth, useAuth } from "../context/AuthContext";

/**
 * Three.js touches `window` at import time, so the 3D twin must never be part
 * of the server render. `ssr: false` keeps it strictly client-side; the 2D grid
 * below is unaffected and stays the fallback if WebGL is unavailable.
 */
const ChargingTwin3D = dynamic(() => import("../components/ChargingTwin3D"), {
  ssr: false,
  loading: () => (
    <div className="w-full h-[540px] rounded-2xl border border-white/10 bg-slate-950 flex flex-col items-center justify-center space-y-3">
      <Loader2 className="w-8 h-8 text-slate-400 animate-spin" />
      <span className="font-mono text-xs text-slate-400 tracking-wider">
        INITIALISING WEBGL DIGITAL TWIN...
      </span>
    </div>
  ),
});

/**
 * The landing hero is procedural WebGL; same SSR rule as the twin above.
 * It replaces the old video hero, so the landing page ships no media files.
 */
const InteractiveHero3D = dynamic(() => import("../components/InteractiveHero3D"), {
  ssr: false,
  loading: () => (
    <div className="flex h-[86vh] min-h-[560px] w-full flex-col items-center justify-center space-y-3 rounded-2xl border border-volt-line bg-volt-bg">
      <Loader2 className="h-8 w-8 animate-spin text-slate-400" />
      <span className="font-mono text-xs tracking-wider text-slate-400">
        SPINNING UP REACTOR CORE...
      </span>
    </div>
  ),
});

/** Topology is plain SVG, but it is heavy enough to keep out of the first load. */
const NetworkTopology = dynamic(() => import("../components/NetworkTopology"), {
  ssr: false,
  loading: () => (
    <div className="flex h-[560px] w-full items-center justify-center rounded-2xl border border-volt-line bg-volt-bg">
      <Loader2 className="h-6 w-6 animate-spin text-slate-400" />
    </div>
  ),
});

/** Multi-site federation view; only opens a socket to the hub while mounted. */
const FederationNetwork = dynamic(() => import("../components/FederationNetwork"), {
  ssr: false,
  loading: () => (
    <div className="flex h-[360px] w-full items-center justify-center rounded-2xl border border-volt-line bg-volt-bg">
      <Loader2 className="h-6 w-6 animate-spin text-slate-400" />
    </div>
  ),
});

type ViewState = "landing" | "noc" | "account" | "settings";
type NocTab = "all" | "grid" | "fleet" | "twin3d" | "topology" | "network" | "threats";

// The stations the twin actually runs a live socket for.
const LIVE_STATIONS = ["CP-01", "CP-02", "CP-03", "CP-04", "CP-05", "CP-06", "CP-07", "CP-08"];
const ALL_STATIONS = LIVE_STATIONS; // kept for topology's live-fleet prop
const FLEET_PRESETS = [8, 16, 24, 32] as const;
const FLEET_MIN = 4;
const FLEET_MAX = 48;

function getStatusBadgeStyle(status: StationStatus | undefined) {
  switch (status) {
    case "Charging":
      return "bg-state-active/10 text-cyan-300 border-cyan-500/30";
    case "Available":
      return "bg-state-healthy/10 text-emerald-300 border-emerald-500/30";
    case "Preparing":
      return "bg-state-warn/10 text-amber-300 border-amber-500/30";
    case "Finishing":
      return "bg-state-healthy/10 text-emerald-300 border-emerald-500/30";
    case "Quarantined":
      return "bg-state-critical/10 text-red-300 border-red-500/40";
    case "Offline":
      return "bg-volt-elevated text-volt-muted border-volt-line";
    default:
      return "bg-volt-elevated text-volt-muted border-volt-line";
  }
}

export default function App() {
  const [currentView, setCurrentView] = useState<ViewState>("landing");
  const [nocTab, setNocTab] = useState<NocTab>("all");
  const [isLoginOpen, setIsLoginOpen] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [loadingMessage, setLoadingMessage] = useState("");
  const [user, setUser] = useState<{ name: string; email: string } | null>(null);
  const [waveOpacity] = useState(0.65); // Fixed at 65%

  const router = useRouter();
  const { operator, logout } = useAuth();
  const handleSignOut = () => {
    logout();
    router.push("/login");
  };

  const { isUnlocked, unlockAudio, playAlert } = useAudioAlert();
  const { isEnabled: voiceEnabled, setIsEnabled: setVoiceEnabled, speakThreat } = useVoiceAlert();

  const handleThreat = useCallback(
    (threat: ThreatEvent) => {
      if (threat.tier === 1 || threat.severity === "high") {
        playAlert();
        speakThreat(threat);
      }
    },
    [playAlert, speakThreat]
  );

  const { stations, grid, threats, powerHistory, isConnected, ingressRate, mode, setMode } = useFeed({
    source: "fixture",
    onThreat: handleThreat,
  });

  // Fleet-size configurator. Live stations pass through; anything above the live
  // count is synthesised and badged SIM (see useSyntheticFleet).
  const [stationCount, setStationCount] = useState(LIVE_STATIONS.length);
  const fleet = useSyntheticFleet(stations, LIVE_STATIONS, stationCount);

  // Effective substation envelope across the whole (live + sim) fleet.
  const fleetAgg = React.useMemo(() => {
    let kw = 0;
    let active = 0;
    for (const id of fleet.stationIds) {
      const t = fleet.stations[id];
      if (!t) continue;
      kw += t.power_kw;
      if (t.status === "Charging" || t.status === "Finishing") active += 1;
    }
    return { kw, active };
  }, [fleet]);

  const navigateTo = (view: ViewState, msg = "Loading Proxy View...") => {
    setLoadingMessage(msg);
    setIsLoading(true);
    setTimeout(() => {
      setCurrentView(view);
      setIsLoading(false);
      window.scrollTo(0, 0);
    }, 600);
  };

  const exportForensicLogs = () => {
    const dataStr = "data:text/json;charset=utf-8," + encodeURIComponent(JSON.stringify(threats, null, 2));
    const downloadAnchor = document.createElement("a");
    downloadAnchor.setAttribute("href", dataStr);
    downloadAnchor.setAttribute("download", `voltsentry_forensics_${Date.now()}.json`);
    document.body.appendChild(downloadAnchor);
    downloadAnchor.click();
    downloadAnchor.remove();
  };

  return (
    <RequireAuth>
    <div className="relative min-h-screen bg-volt-bg text-slate-200 font-sans flex flex-col justify-between selection:bg-volt-green/20 selection:text-slate-50">
      {/* Dynamic 3D Kexsio Wave Canvas Background (Fixed at 0.65 Opacity) */}
      <WaveBackground opacity={0.65} />

      {/* Loading Screen Overlay */}
      {isLoading && <LoadingScreen message={loadingMessage} />}

      {/* Login Modal */}
      <LoginModal
        isOpen={isLoginOpen}
        onClose={() => setIsLoginOpen(false)}
        onLoginSuccess={(userData) => {
          setUser(userData);
          navigateTo("noc", "Authenticating Operator SSO & Connecting Ingress Feed...");
        }}
      />

      {/* Global Starlink-Style Navbar */}
      <nav className="relative z-30 w-full border-b border-white/10 bg-slate-950/80 backdrop-blur-md px-6 sm:px-12 py-4">
        <div className="max-w-7xl mx-auto flex items-center justify-between">
          {/* Logo */}
          <div
            onClick={() => navigateTo("landing", "Navigating to VoltSentry Overview...")}
            className="flex items-center space-x-2.5 cursor-pointer group"
          >
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src="/voltsentry-mark.png"
              alt="VoltSentry"
              className="w-7 h-7 rounded-md group-hover:rotate-12 transition-transform"
            />
            <span className="text-lg font-black tracking-wider uppercase font-mono text-white">
              VOLTSENTRY
            </span>
          </div>

          {/* Center Navigation Links */}
          <div className="hidden md:flex items-center space-x-8 font-mono text-xs text-slate-300">
            <button
              onClick={() => navigateTo("landing", "Loading Overview...")}
              className={`hover:text-white transition-colors flex items-center space-x-1.5 ${
                currentView === "landing" ? "text-volt-green font-bold" : ""
              }`}
            >
              <Globe className="w-3.5 h-3.5" />
              <span>OVERVIEW</span>
            </button>
            <button
              onClick={() => navigateTo("noc", "Connecting to NOC Security Console...")}
              className={`hover:text-white transition-colors flex items-center space-x-1.5 ${
                currentView === "noc" ? "text-volt-green font-bold" : ""
              }`}
            >
              <LayoutDashboard className="w-3.5 h-3.5" />
              <span>FLEET NOC</span>
            </button>
            <button
              onClick={() => navigateTo("account", "Loading Operator Profile...")}
              className={`hover:text-white transition-colors flex items-center space-x-1.5 ${
                currentView === "account" ? "text-volt-green font-bold" : ""
              }`}
            >
              <User className="w-3.5 h-3.5" />
              <span>ACCOUNT</span>
            </button>
            <button
              onClick={() => navigateTo("settings", "Loading System Settings...")}
              className={`hover:text-white transition-colors flex items-center space-x-1.5 ${
                currentView === "settings" ? "text-volt-green font-bold" : ""
              }`}
            >
              <Settings className="w-3.5 h-3.5" />
              <span>SETTINGS</span>
            </button>
          </div>

          {/* Right Action Items — logged-in Operator widget */}
          <div className="flex items-center space-x-2 font-mono text-xs">
            {/* Supply frequency — measured at the charger inlets. */}
            <GridFrequencyWidget grid={grid} />
            <button
              onClick={() => navigateTo("account")}
              className="flex items-center gap-2 rounded-xl border border-volt-green/40 bg-volt-green/15 px-3 py-1.5 text-volt-green transition-colors hover:bg-volt-green/25"
              title="Open operator profile"
            >
              <User className="h-3.5 w-3.5" />
              <span className="font-bold">{(operator?.name ?? "Operator").split(" ")[0]}</span>
              <span className="rounded-md bg-black/40 px-1.5 py-0.5 text-[10px] font-bold tracking-wider text-volt-green/90">
                {operator?.employeeId ?? "EMP-—"}
              </span>
            </button>
            <button
              onClick={handleSignOut}
              className="flex items-center gap-1.5 rounded-xl border border-rose-500/40 bg-rose-500/15 px-3 py-1.5 font-bold uppercase tracking-wider text-[11px] text-rose-300 transition-colors hover:bg-rose-500/25"
              title="Sign out"
            >
              <LogOut className="h-3.5 w-3.5" />
              <span className="hidden sm:inline">Sign Out</span>
            </button>
          </div>
        </div>
      </nav>

      {/* Main Content Area */}
      <div className="relative z-10 max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 w-full flex-1">
        {/* VIEW 1: LANDING PAGE */}
        {currentView === "landing" && (
          <>
            {/* Procedural WebGL hero — replaces the old video splash. */}
            <div className="pt-5">
              <InteractiveHero3D
                onLaunchNoc={() => navigateTo("noc", "Opening NOC Console...")}
                onOpenTopology={() => {
                  setNocTab("topology");
                  navigateTo("noc", "Rendering Fleet Network Topology...");
                }}
              />
            </div>

            <StoryLanding
              onLaunchNoc={() => navigateTo("noc", "Opening NOC Console...")}
              onOpenLogin={() => setIsLoginOpen(true)}
            />
          </>
        )}

        {/* VIEW 2: ACCOUNT PROFILE */}
        {currentView === "account" && (
          <AccountView
            user={operator ? { name: operator.name, email: operator.email } : user}
            onLogout={handleSignOut}
          />
        )}

        {/* VIEW 3: SYSTEM SETTINGS */}
        {currentView === "settings" && (
          <SettingsView
            waveOpacity={waveOpacity}
            setWaveOpacity={() => {}}
            voiceEnabled={voiceEnabled}
            setVoiceEnabled={setVoiceEnabled}
          />
        )}

        {/* VIEW 4: NOC DASHBOARD */}
        {currentView === "noc" && (
          <div className="space-y-6 pt-6 pb-16">
            {/* Header / Control Bar */}
            <header className="flex flex-col lg:flex-row lg:items-center justify-between gap-4 p-4 rounded-lg glass-panel-dense">
              <div className="flex items-center gap-3">
                <div className="p-2 bg-volt-elevated rounded-md border border-volt-line">
                  <Icon3DShield className="w-8 h-8" />
                </div>
                <div>
                  <h1 className="text-lg font-bold tracking-tight text-slate-100">
                    VoltSentry <span className="text-volt-muted font-mono text-[11px] font-medium tracking-wide ml-1.5">SECURITY CONSOLE</span>
                  </h1>
                  <p className="text-xs text-volt-muted mt-0.5">Real-time threat detection for EV charging networks</p>
                </div>
              </div>

              {/* Controls */}
              <div className="flex flex-wrap items-center gap-3">
                {!isUnlocked ? (
                  <button
                    onClick={unlockAudio}
                    className="flex items-center space-x-2 px-4 py-2 rounded-xl bg-volt-green/20 hover:bg-volt-green/30 text-volt-green border border-volt-green/40 text-xs font-bold transition-all shadow-lg"
                  >
                    <Play className="w-4 h-4 fill-current text-volt-green" />
                    <span>START MONITORING (ENABLE AUDIO)</span>
                  </button>
                ) : (
                  <div className="flex items-center space-x-2 px-3.5 py-1.5 rounded-xl bg-emerald-500/20 text-emerald-300 border border-emerald-500/40 text-xs font-bold">
                    <Volume2 className="w-4 h-4" />
                    <span>AUDIO & VOICE ALERTS ARMED</span>
                  </div>
                )}

                {/* Mode Switcher */}
                <div className="flex items-center bg-black/60 rounded-xl p-1 border border-white/15 text-xs font-mono">
                  <button
                    onClick={() => setMode("fixture")}
                    className={`px-3 py-1.5 rounded-lg transition-all font-bold ${
                      mode === "fixture" ? "bg-white text-black shadow-md" : "text-slate-300 hover:text-white"
                    }`}
                  >
                    DEMO REPLAY MODE
                  </button>
                  <button
                    onClick={() => setMode("live")}
                    className={`px-3 py-1.5 rounded-lg transition-all font-bold ${
                      mode === "live" ? "bg-white text-black shadow-md" : "text-slate-300 hover:text-white"
                    }`}
                  >
                    LIVE CHARGER STREAM
                  </button>
                </div>

                {/* Live Stream Indicator */}
                <div className="flex items-center space-x-2 px-3.5 py-1.5 rounded-xl bg-black/60 border border-white/15 text-xs font-mono">
                  <Radio className={`w-4 h-4 ${isConnected ? "text-emerald-400 animate-pulse" : "text-rose-500"}`} />
                  <span className={isConnected ? "text-emerald-400 font-bold" : "text-rose-400 font-bold"}>
                    {isConnected ? "MONITORING ACTIVE" : "RECONNECTING TO NETWORK..."}
                  </span>
                </div>

                {/* Forensic JSON Export Button */}
                <button
                  onClick={exportForensicLogs}
                  className="flex items-center space-x-1.5 px-3 py-1.5 rounded-xl bg-white/10 hover:bg-white/20 text-slate-200 border border-white/20 text-xs font-semibold transition-all shadow-md"
                  title="Download accumulated ThreatEvent log JSON"
                >
                  <Download className="w-3.5 h-3.5 text-slate-400" />
                  <span>EXPORT INCIDENT LOGS</span>
                </button>
              </div>
            </header>

            {/* SCADA telemetry strip — measured frequency, bus voltage,
                transformer kVA load, line current, ingress, readiness. */}
            <div className="rounded-2xl glass-panel-dense p-3">
              <ScadaHeaderStrip grid={grid} threats={threats} isConnected={isConnected} ingressRate={ingressRate} />
            </div>

            {grid?.enforcement === "monitor" && (
              <div className="flex flex-wrap items-center gap-3 rounded-2xl border border-amber-500/40 bg-amber-500/10 px-4 py-2.5 font-mono text-xs text-amber-200">
                <span className="rounded border border-amber-400/60 px-1.5 py-0.5 text-[10px] font-bold tracking-wider text-amber-300">
                  MONITOR-ONLY
                </span>
                <span>
                  {grid.node_id ?? "This site"} reports every detection but blocks nothing. Switch it to enforce
                  (<span className="text-amber-100">--mode enforce</span>) once its rules are proven on this site&apos;s meters.
                </span>
              </div>
            )}

            {/* NOC Sub-Navigation Menu Bar for Clean Admin Focus */}
            <div className="flex flex-wrap items-center justify-between gap-3 bg-slate-900/90 p-2 rounded-2xl border border-white/10 text-xs font-mono shadow-lg">
              <div className="flex flex-wrap items-center gap-2">
                <button
                  onClick={() => setNocTab("all")}
                  className={`px-3.5 py-2 rounded-xl transition-all flex items-center space-x-2 font-bold ${
                    nocTab === "all"
                      ? "bg-volt-elevated text-slate-100 border border-volt-line-strong"
                      : "text-slate-400 hover:text-white bg-black/40 border border-transparent"
                  }`}
                >
                  <LayoutDashboard className="w-4 h-4" />
                  <span>FULL OVERVIEW</span>
                </button>

                <button
                  onClick={() => setNocTab("grid")}
                  className={`px-3.5 py-2 rounded-xl transition-all flex items-center space-x-2 font-bold ${
                    nocTab === "grid"
                      ? "bg-volt-elevated text-slate-100 border border-volt-line-strong"
                      : "text-slate-400 hover:text-white bg-black/40 border border-transparent"
                  }`}
                >
                  <Zap className="w-4 h-4" />
                  <span>POWER GRID & CHARTS</span>
                </button>

                <button
                  onClick={() => setNocTab("fleet")}
                  className={`px-3.5 py-2 rounded-xl transition-all flex items-center space-x-2 font-bold ${
                    nocTab === "fleet"
                      ? "bg-volt-elevated text-slate-100 border border-volt-line-strong"
                      : "text-slate-400 hover:text-white bg-black/40 border border-transparent"
                  }`}
                >
                  <Radio className="w-4 h-4" />
                  <span>2D FLEET GRID ({stationCount})</span>
                </button>

                <button
                  onClick={() => setNocTab("twin3d")}
                  className={`px-3.5 py-2 rounded-xl transition-all flex items-center space-x-2 font-bold ${
                    nocTab === "twin3d"
                      ? "bg-volt-elevated text-slate-100 border border-volt-line-strong"
                      : "text-slate-400 hover:text-white bg-black/40 border border-transparent"
                  }`}
                >
                  <Box className="w-4 h-4" />
                  <span>3D DIGITAL TWIN</span>
                </button>

                <button
                  onClick={() => setNocTab("topology")}
                  className={`px-3.5 py-2 rounded-xl transition-all flex items-center space-x-2 font-bold ${
                    nocTab === "topology"
                      ? "bg-volt-elevated text-slate-100 border border-volt-line-strong"
                      : "text-slate-400 hover:text-white bg-black/40 border border-transparent"
                  }`}
                >
                  <Network className="w-4 h-4" />
                  <span>NETWORK TOPOLOGY</span>
                </button>

                <button
                  onClick={() => setNocTab("network")}
                  className={`px-3.5 py-2 rounded-xl transition-all flex items-center space-x-2 font-bold ${
                    nocTab === "network"
                      ? "bg-volt-elevated text-slate-100 border border-volt-line-strong"
                      : "text-slate-400 hover:text-white bg-black/40 border border-transparent"
                  }`}
                >
                  <Globe className="w-4 h-4" />
                  <span>MULTI-SITE NETWORK</span>
                </button>

                <button
                  onClick={() => setNocTab("threats")}
                  className={`px-3.5 py-2 rounded-xl transition-all flex items-center space-x-2 font-bold ${
                    nocTab === "threats"
                      ? "bg-rose-500/20 text-rose-300 border border-rose-500/40 shadow-sm"
                      : "text-slate-400 hover:text-white bg-black/40 border border-transparent"
                  }`}
                >
                  <ShieldAlert className="w-4 h-4" />
                  <span>SECURITY LOGS ({threats.length})</span>
                </button>
              </div>

              <div className="hidden lg:flex items-center space-x-2 text-[11px] text-slate-400 font-sans pr-2">
                <span>Filter View:</span>
                <span className="font-mono text-slate-300 font-medium uppercase">{nocTab === "all" ? "All Sections" : nocTab}</span>
              </div>
            </div>

            {/* Transformer Gauge + Power Chart Section */}
            {(nocTab === "all" || nocTab === "grid") && (
              <section className="grid grid-cols-1 lg:grid-cols-3 gap-5">
                <div className="lg:col-span-1">
                  <TransformerGauge
                    grid={grid}
                    bays={stationCount}
                    scaledOutputKw={fleet.hasSim ? fleetAgg.kw : undefined}
                    scaledActive={fleet.hasSim ? fleetAgg.active : undefined}
                  />
                </div>
                <div className="lg:col-span-2">
                  <PowerChart data={powerHistory} />
                </div>
              </section>
            )}

            {/* 3D Digital Twin — mounts only on its own tab so the 2D console
                never pays the WebGL cost, and unmounts cleanly when you leave. */}
            {nocTab === "twin3d" && (
              <div className="space-y-4">
                <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-3 pt-2">
                  <h2 className="text-xl font-black text-slate-100 flex items-center space-x-3">
                    <Box className="w-5 h-5 text-slate-400" />
                    <span>Charging Bay Digital Twin ({stationCount} Bays)</span>
                  </h2>
                  <FleetConfigurator count={stationCount} setCount={setStationCount} />
                </div>

                <ChargingTwin3D
                  stations={fleet.stations}
                  threats={threats}
                  stationIds={fleet.stationIds}
                  simIds={fleet.simIds}
                />
              </div>
            )}

            {/* Dynamic fleet network topology — scalable node graph + red-team drawer. */}
            {nocTab === "topology" && (
              <div className="space-y-4">
                <div className="flex items-center justify-between pt-2">
                  <h2 className="text-xl font-black text-slate-100 flex items-center space-x-3">
                    <Network className="w-5 h-5 text-slate-400" />
                    <span>Fleet Network Topology</span>
                  </h2>
                  <div className="text-xs font-mono text-slate-300 bg-black/60 px-3 py-1 rounded-lg border border-white/15">
                    Live fleet: <span className="text-slate-300 font-medium">{ALL_STATIONS.length} stations</span>
                  </div>
                </div>

                <NetworkTopology
                  stations={stations}
                  threats={threats}
                  grid={grid}
                  liveStationIds={ALL_STATIONS}
                />
              </div>
            )}

            {/* Multi-site federation: every VoltSentry node and the intel they share. */}
            {nocTab === "network" && (
              <div className="space-y-4">
                <div className="flex items-center justify-between pt-2">
                  <h2 className="text-xl font-black text-slate-100 flex items-center space-x-3">
                    <Globe className="w-5 h-5 text-slate-400" />
                    <span>VoltSentry Multi-Site Network</span>
                  </h2>
                  <div className="text-xs font-mono text-slate-300 bg-black/60 px-3 py-1 rounded-lg border border-white/15">
                    This console: <span className="text-slate-100 font-medium">{grid?.node_id ?? "standalone"}</span>
                  </div>
                </div>
                <FederationNetwork />
              </div>
            )}

            {/* Fleet Section Header & 8-Station Grid */}
            {(nocTab === "all" || nocTab === "fleet") && (
              <div className="space-y-4">
                <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-3 pt-2">
                  <h2 className="text-xl font-black text-slate-100 flex items-center space-x-3">
                    <Zap className="w-5 h-5 text-slate-400" />
                    <span>Charger Fleet Overview ({stationCount} Stations)</span>
                  </h2>
                  <FleetConfigurator count={stationCount} setCount={setStationCount} />
                </div>

                <section className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
                  {fleet.stationIds.map((stationId) => {
                    const telemetry: TelemetryEvent | undefined = fleet.stations[stationId];
                    const isSim = fleet.simIds.has(stationId);
                    const isQuarantined = telemetry?.status === "Quarantined";
                    const isMlAnomaly = (telemetry?.ml_score ?? 0) > 0.65;
                    const stationThreat = threats.find((t) => t.station_id === stationId);
                    // Monitor mode: a rule fired but the bay was left online.
                    const wouldBlock = !isQuarantined && stationThreat?.recommended_action != null;
                    const risk = Math.min(Math.max(telemetry?.ml_score ?? 0, 0), 1);
                    const soc = Math.min(Math.max(telemetry?.soc ?? 0, 0), 100);
                    const isCharging = telemetry?.status === "Charging";
                    const dotClass = isQuarantined
                      ? "bg-state-critical"
                      : isMlAnomaly
                      ? "bg-state-warn"
                      : isCharging
                      ? "bg-state-active"
                      : telemetry
                      ? "bg-state-healthy"
                      : "bg-slate-600";
                    const riskBarClass = isQuarantined
                      ? "bg-state-critical"
                      : risk > 0.65
                      ? "bg-state-warn"
                      : risk > 0.4
                      ? "bg-amber-500/60"
                      : "bg-slate-500";

                    return (
                      <div
                        key={stationId}
                        className={`p-3 rounded-lg transition-colors ${
                          isQuarantined
                            ? "glass-card-threat-tier1"
                            : isMlAnomaly || wouldBlock
                            ? "glass-card-threat-tier2"
                            : "glass-panel hover:border-volt-line-strong"
                        }`}
                      >
                        {/* Header */}
                        <div className="flex items-center justify-between pb-2 border-b border-volt-line">
                          <div className="flex items-center gap-2">
                            <span className={`w-1.5 h-1.5 rounded-full ${dotClass}`} />
                            <span className="font-mono font-semibold text-sm text-slate-100">{stationId}</span>
                            {isSim && (
                              <span className="rounded border border-amber-500/40 bg-amber-500/10 px-1 py-0.5 font-mono text-[8px] font-bold uppercase tracking-wider text-amber-300">
                                SIM
                              </span>
                            )}
                          </div>
                          <span
                            className={`text-[10px] px-1.5 py-0.5 rounded border font-medium tracking-wide ${getStatusBadgeStyle(
                              telemetry?.status
                            )}`}
                          >
                            {telemetry ? telemetry.status : "—"}
                          </span>
                        </div>

                        {/* Line-item telemetry */}
                        <div className="mt-2 divide-y divide-volt-line/70 font-mono text-xs">
                          <div className="flex items-center justify-between py-1">
                            <span className="font-sans text-volt-muted">Power</span>
                            <span className="text-slate-100 tabular-nums">
                              {telemetry ? telemetry.power_kw.toFixed(1) : "0.0"}
                              <span className="text-volt-muted ml-1">kW</span>
                            </span>
                          </div>
                          <div className="flex items-center justify-between py-1">
                            <span className="font-sans text-volt-muted">DC out</span>
                            <span className="text-slate-100 tabular-nums">
                              {telemetry?.dc_voltage_v != null ? telemetry.dc_voltage_v.toFixed(0) : "—"}
                              <span className="text-volt-muted ml-0.5 mr-1.5">V</span>
                              {telemetry?.dc_current_a != null ? telemetry.dc_current_a.toFixed(0) : "—"}
                              <span className="text-volt-muted ml-0.5">A</span>
                            </span>
                          </div>
                          <div className="flex items-center justify-between py-1">
                            <span className="font-sans text-volt-muted">SoC</span>
                            <span className="text-slate-100 tabular-nums">
                              {telemetry ? soc.toFixed(0) : "0"}
                              <span className="text-volt-muted ml-1">%</span>
                            </span>
                          </div>
                          <div className="flex items-center justify-between py-1">
                            <span className="font-sans text-volt-muted">Energy</span>
                            <span className="text-slate-100 tabular-nums">
                              {telemetry ? telemetry.energy_register_kwh.toFixed(1) : "0.0"}
                              <span className="text-volt-muted ml-1">kWh</span>
                            </span>
                          </div>
                        </div>

                        {/* SoC bar */}
                        <div className="mt-2 h-1 w-full rounded-full bg-volt-bg overflow-hidden">
                          <div
                            className={`h-full transition-all duration-500 ${
                              isQuarantined ? "bg-state-critical" : isCharging ? "bg-state-active" : "bg-state-healthy"
                            }`}
                            style={{ width: `${soc}%` }}
                          />
                        </div>

                        {/* Risk Index */}
                        <div className="mt-2.5 pt-2 border-t border-volt-line">
                          <div className="flex items-center justify-between text-xs font-mono">
                            <span className="font-sans text-volt-muted">Risk Index</span>
                            <span
                              className={`tabular-nums font-medium ${
                                isMlAnomaly ? "text-amber-300" : "text-slate-300"
                              }`}
                            >
                              {telemetry ? risk.toFixed(2) : "0.00"}
                            </span>
                          </div>
                          <div className="mt-1 h-1 w-full rounded-full bg-volt-bg overflow-hidden">
                            <div
                              className={`h-full transition-all duration-500 ${riskBarClass}`}
                              style={{ width: `${risk * 100}%` }}
                            />
                          </div>
                        </div>

                        {/* Badges */}
                        {isQuarantined && (
                          <div className="mt-2">
                            <ThreatBadge
                              threat={stationThreat}
                              tier={1}
                              ruleId="QUARANTINED"
                              reason="Isolated by a Tier-1 rule; see the security log"
                              actionTaken="quarantined"
                              compact={true}
                            />
                          </div>
                        )}
                        {wouldBlock && (
                          <div className="mt-2">
                            <ThreatBadge threat={stationThreat} compact={true} />
                          </div>
                        )}
                        {isMlAnomaly && !isQuarantined && !wouldBlock && (
                          <div className="mt-2">
                            <ThreatBadge
                              threat={stationThreat}
                              tier={2}
                              mlScore={telemetry?.ml_score}
                              reason="Risk index above the 0.65 Tier-2 threshold"
                              actionTaken="logged"
                              compact={true}
                            />
                          </div>
                        )}
                      </div>
                    );
                  })}
                </section>
              </div>
            )}

            {/* Threat Timeline Section */}
            {(nocTab === "all" || nocTab === "threats") && (
              <section className="p-5 rounded-2xl glass-panel space-y-3">
                <div className="flex items-center justify-between pb-1">
                  <h3 className="text-lg font-black text-slate-200 flex items-center space-x-2">
                    <ShieldAlert className="w-5 h-5 text-rose-400" />
                    <span>Security Incident Log ({threats.length} Recorded)</span>
                  </h3>
                  <button
                    onClick={exportForensicLogs}
                    className="text-xs font-mono text-slate-300 hover:text-slate-100 flex items-center space-x-1"
                  >
                    <Download className="w-3.5 h-3.5" />
                    <span>Download Log JSON</span>
                  </button>
                </div>
                <div className="space-y-3 max-h-64 overflow-y-auto pr-2">
                  {threats.length === 0 ? (
                    <div className="text-xs font-mono text-slate-400 p-4 text-center bg-black/40 rounded-xl border border-white/10">
                      No security threats detected. All charging stations are operating safely within normal parameters.
                    </div>
                  ) : (
                    threats.map((threat, idx) => <ThreatBadge key={idx} threat={threat} />)
                  )}
                </div>
              </section>
            )}
          </div>
        )}
      </div>

      {/* Global Starlink-Style Footer */}
      <Footer />
    </div>
    </RequireAuth>
  );
}

/**
 * Fleet dimension configurator — preset pills + a numeric stepper. Stations
 * above the live backend count are synthesised and badged SIM downstream.
 */
function FleetConfigurator({
  count,
  setCount,
}: {
  count: number;
  setCount: (n: number) => void;
}) {
  const clamp = (n: number) => Math.max(FLEET_MIN, Math.min(FLEET_MAX, n));
  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="flex items-center gap-1.5 font-mono text-[10px] font-bold uppercase tracking-widest text-volt-muted">
        <Layers className="h-3.5 w-3.5 text-volt-green" />
        Fleet size
      </span>
      <div className="flex items-center gap-1 rounded-xl border border-volt-line bg-black/50 p-1">
        {FLEET_PRESETS.map((p) => (
          <button
            key={p}
            onClick={() => setCount(p)}
            className={`min-w-[34px] rounded-lg px-2 py-1.5 font-mono text-xs font-bold transition-all ${
              count === p ? "bg-volt-green text-slate-950" : "text-volt-muted hover:text-white"
            }`}
          >
            {p}
          </button>
        ))}
      </div>
      <div className="flex items-center gap-1 rounded-xl border border-volt-line bg-black/50 p-1">
        <button
          onClick={() => setCount(clamp(count - 1))}
          className="rounded-lg p-1.5 text-volt-muted transition-colors hover:text-white disabled:opacity-40"
          disabled={count <= FLEET_MIN}
          aria-label="Remove a bay"
        >
          <Minus className="h-3.5 w-3.5" />
        </button>
        <span className="min-w-[28px] text-center font-mono text-sm font-bold tabular-nums text-white">
          {count}
        </span>
        <button
          onClick={() => setCount(clamp(count + 1))}
          className="rounded-lg p-1.5 text-volt-muted transition-colors hover:text-white disabled:opacity-40"
          disabled={count >= FLEET_MAX}
          aria-label="Add a bay"
        >
          <Plus className="h-3.5 w-3.5" />
        </button>
      </div>
      <span className="font-mono text-[9px] uppercase tracking-wider text-volt-muted/70">
        {FLEET_MIN}–{FLEET_MAX} bays
      </span>
    </div>
  );
}

/**
 * SCADA telemetry strip for the NOC header. Every figure is measured: frequency
 * and bus voltage are the chargers' own inlet readings (averaged by the proxy),
 * load is the transformer's kVA against its kVA rating, and ingress counts the
 * MeterValues frames that actually arrived in the last second.
 */
function ScadaHeaderStrip({
  grid,
  threats,
  isConnected,
  ingressRate,
}: {
  grid: GridEvent | null;
  threats: ThreatEvent[];
  isConnected: boolean;
  ingressRate: number;
}) {
  const { hz, deviation, strained } = useGridFrequency(grid);
  const busV = grid?.lt_voltage_v ?? null;
  const kva = grid?.total_load_kva ?? null;
  const loadPct = grid?.loading_pct ?? (kva != null && grid ? (kva / grid.transformer_capacity_kva) * 100 : null);
  const lineA = grid?.lt_current_a ?? null;

  // DEFCON-style readiness from live threat pressure.
  const recentCritical = threats.some(
    (t) => t.action_taken === "quarantined" || t.tier === 1
  );
  const anyThreat = threats.length > 0;
  const readiness = recentCritical
    ? { label: "THREAT — LEVEL 3", cls: "border-rose-500/50 bg-rose-500/15 text-rose-300", dot: "bg-rose-400" }
    : anyThreat
    ? { label: "ELEVATED — LEVEL 2", cls: "border-amber-500/50 bg-amber-500/15 text-amber-300", dot: "bg-amber-400" }
    : { label: "SECURE — LEVEL 1", cls: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300", dot: "bg-emerald-400" };

  return (
    <div className="flex flex-wrap items-center gap-2 font-mono text-[10px]">
      <ScadaCell
        icon={<Activity className="h-3 w-3" />}
        label="Grid Freq"
        value={hz != null ? `${hz.toFixed(3)} Hz` : "—"}
        sub={deviation != null ? `${deviation >= 0 ? "+" : ""}${deviation.toFixed(3)} · measured` : "not reported"}
        tone={strained ? "warn" : "ok"}
      />
      <ScadaCell
        icon={<Zap className="h-3 w-3" />}
        label="Bus Voltage"
        value={busV != null ? `${busV.toFixed(1)} V` : "—"}
        sub={busV != null ? `L-L · ${phaseToNeutralV(busV).toFixed(0)} V L-N` : "not reported"}
        tone="ok"
      />
      <ScadaCell
        icon={<Gauge className="h-3 w-3" />}
        label="Transformer"
        value={loadPct != null ? `${loadPct.toFixed(0)}%` : "—"}
        sub={kva != null && grid ? `${kva.toFixed(0)} / ${grid.transformer_capacity_kva.toFixed(0)} kVA` : "kVA load"}
        tone={loadPct != null && loadPct > 85 ? "warn" : "ok"}
      />
      <ScadaCell
        icon={<Zap className="h-3 w-3" />}
        label="Line Current"
        value={lineA != null ? `${lineA.toFixed(0)} A` : "—"}
        sub="per phase"
        tone="ok"
      />
      <ScadaCell
        icon={<Cpu className="h-3 w-3" />}
        label="Ingress"
        value={`${ingressRate} msg/s`}
        sub={isConnected ? "MeterValues inspected" : "reconnecting"}
        tone="ok"
      />
      <div
        className={`flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 font-bold uppercase tracking-wider ${readiness.cls}`}
        title="Grid readiness derived from live threat pressure"
      >
        <span className={`h-1.5 w-1.5 rounded-full ${readiness.dot} animate-pulse`} />
        {readiness.label}
      </div>
    </div>
  );
}

function ScadaCell({
  icon,
  label,
  value,
  sub,
  tone,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  sub: string;
  tone: "ok" | "warn";
}) {
  return (
    <div className="rounded-lg border border-volt-line bg-black/50 px-2.5 py-1.5">
      <div className="flex items-center gap-1 uppercase tracking-wider text-volt-muted">
        <span className={tone === "warn" ? "text-state-warn" : "text-state-active"}>{icon}</span>
        {label}
      </div>
      <div className={`mt-0.5 text-xs font-bold tabular-nums ${tone === "warn" ? "text-amber-300" : "text-white"}`}>
        {value}
      </div>
      <div className="text-[8px] uppercase tracking-wider text-volt-muted/70">{sub}</div>
    </div>
  );
}

function GridFrequencyWidget({ grid }: { grid: GridEvent | null }) {
  const { hz, deviation, strained } = useGridFrequency(grid);

  return (
    <div
      className="hidden items-center gap-2 rounded-xl border border-volt-line bg-black/50 px-3 py-1.5 lg:flex"
      title="Supply frequency measured at the charger inlets (Indian grid band 49.90-50.05 Hz)"
    >
      <Activity
        className={`h-3.5 w-3.5 ${strained ? "text-state-warn" : "text-state-healthy"}`}
      />
      <div className="leading-none">
        <div className="flex items-baseline gap-1">
          <span
            className={`font-mono text-xs font-bold tabular-nums ${
              strained ? "text-amber-300" : "text-emerald-300"
            }`}
          >
            {hz != null ? hz.toFixed(2) : "—"}
          </span>
          <span className="font-mono text-[9px] text-volt-muted">Hz</span>
        </div>
        <div className="mt-0.5 font-mono text-[8px] uppercase tracking-wider text-volt-muted">
          {deviation != null ? `${deviation >= 0 ? "+" : ""}${deviation.toFixed(3)} · measured` : "not reported"}
        </div>
      </div>
    </div>
  );
}
