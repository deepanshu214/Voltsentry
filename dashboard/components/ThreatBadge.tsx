"use client";

import React from "react";
import { ThreatEvent } from "../lib/types";
import { AlertTriangle, Activity, ShieldAlert, FileText } from "lucide-react";

interface ThreatBadgeProps {
  threat?: ThreatEvent;
  tier?: 1 | 2;
  ruleId?: string | null;
  mlScore?: number | null;
  reason?: string;
  actionTaken?: string;
  compact?: boolean;
}

export function ThreatBadge({
  threat,
  tier: propTier,
  ruleId: propRuleId,
  mlScore: propMlScore,
  reason: propReason,
  actionTaken: propActionTaken,
  compact = false,
}: ThreatBadgeProps) {
  const tier = threat?.tier ?? propTier ?? 1;
  const ruleId = threat?.rule_id ?? propRuleId ?? "RULE_VIOLATION";
  const mlScore = threat?.ml_score ?? propMlScore ?? 0.0;
  const reason = threat?.reason ?? propReason ?? "Threat detected";
  const actionTaken = threat?.action_taken ?? propActionTaken ?? "logged";
  // Monitor mode: nothing was blocked; say what enforce mode would have done.
  const recommended = threat?.recommended_action ?? null;
  const actionLabel = recommended ? `monitor · would ${recommended === "quarantined" ? "quarantine" : "block"}` : actionTaken;

  if (compact) {
    return tier === 1 ? (
      <div className="inline-flex items-center space-x-1.5 px-2.5 py-1 rounded-lg bg-rose-500/20 border border-rose-500/40 text-rose-300 font-mono text-xs font-bold animate-pulse">
        <AlertTriangle className="w-3.5 h-3.5 text-rose-400" />
        <span>🔴 TIER-1: {ruleId}{recommended ? (recommended === "quarantined" ? " · WOULD QUARANTINE" : " · WOULD BLOCK") : ""}</span>
      </div>
    ) : (
      <div className="inline-flex items-center space-x-1.5 px-2.5 py-1 rounded-lg bg-amber-500/20 border border-amber-500/40 text-amber-300 font-mono text-xs font-bold">
        <Activity className="w-3.5 h-3.5 text-amber-400 animate-spin" />
        <span>🟡 TIER-2: ML ANOMALY {mlScore ? mlScore.toFixed(2) : ""}</span>
      </div>
    );
  }

  return tier === 1 ? (
    <div className="p-3 rounded-xl bg-rose-950/40 border border-rose-500/50 shadow-lg shadow-rose-950/30 text-rose-200 font-mono text-xs space-y-1.5 animate-pulse">
      <div className="flex items-center justify-between font-bold text-rose-300">
        <span className="flex items-center space-x-1.5">
          <ShieldAlert className="w-4 h-4 text-rose-400" />
          <span>🔴 RULE VIOLATION · {ruleId}</span>
        </span>
        <span className="px-2 py-0.5 rounded bg-black/60 border border-rose-500/30 text-[10px] uppercase">
          {actionLabel}
        </span>
      </div>
      <div className="text-slate-300 font-sans">{reason}</div>
    </div>
  ) : (
    <div className="p-3 rounded-xl bg-amber-950/40 border border-amber-500/50 shadow-lg shadow-amber-950/30 text-amber-200 font-mono text-xs space-y-1.5">
      <div className="flex items-center justify-between font-bold text-amber-300">
        <span className="flex items-center space-x-1.5">
          <Activity className="w-4 h-4 text-amber-400" />
          <span>🟡 ML ANOMALY · {mlScore ? mlScore.toFixed(2) : "0.65+"}</span>
        </span>
        <span className="px-2 py-0.5 rounded bg-black/60 border border-amber-500/30 text-[10px] uppercase">
          {actionTaken}
        </span>
      </div>
      <div className="text-slate-300 font-sans">{reason}</div>
    </div>
  );
}
