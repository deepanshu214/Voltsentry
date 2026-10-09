"use client";

import React from "react";

export function Footer() {
  return (
    <footer className="w-full border-t border-white/10 bg-slate-950/90 text-slate-400 text-xs font-mono py-12 px-6 sm:px-12">
      <div className="max-w-7xl mx-auto grid grid-cols-1 md:grid-cols-4 gap-8 mb-12">
        {/* Col 1: Brand & Specs */}
        <div className="space-y-3">
          <div className="flex items-center space-x-2 text-white font-bold text-sm tracking-widest uppercase">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src="/voltsentry-mark.png" alt="VoltSentry" className="w-5 h-5 rounded" />
            <span>VOLTSENTRY</span>
          </div>
          <p className="text-slate-400 leading-relaxed font-sans text-xs">
            Software-defined inline OCPP 1.6-J security reverse proxy & cyber-physical twin.
          </p>
          <div className="text-[10px] text-slate-500 font-mono">
            BUILD 2026.10 · v2.0 · MULTI-SITE
          </div>
        </div>

        {/* Col 2: Architecture */}
        <div className="space-y-2">
          <div className="text-white font-bold tracking-wider uppercase mb-3 text-[11px]">Architecture</div>
          <ul className="space-y-2 text-slate-400">
            <li className="hover:text-white transition-colors cursor-pointer">Proxy Ingress (:8000)</li>
            <li className="hover:text-white transition-colors cursor-pointer">Dashboard Feed (:8100)</li>
            <li className="hover:text-white transition-colors cursor-pointer">Charger Twin (:9100)</li>
            <li className="hover:text-white transition-colors cursor-pointer">Mock CSMS (:9000)</li>
            <li className="hover:text-white transition-colors cursor-pointer">Federation Hub (:7700 / :7701)</li>
          </ul>
        </div>

        {/* Col 3: Detection Engine */}
        <div className="space-y-2">
          <div className="text-white font-bold tracking-wider uppercase mb-3 text-[11px]">Engine Specs</div>
          <ul className="space-y-2 text-slate-400">
            <li className="hover:text-white transition-colors cursor-pointer">Tier-1 Rules R1–R9</li>
            <li className="hover:text-white transition-colors cursor-pointer">Tier-2 Isolation Forest ML</li>
            <li className="hover:text-white transition-colors cursor-pointer">Anomaly Threshold &gt; 0.65</li>
            <li className="hover:text-white transition-colors cursor-pointer">ChangeAvailability Quarantine</li>
            <li className="hover:text-white transition-colors cursor-pointer">Signed Cross-Site Threat Intel</li>
          </ul>
        </div>

        {/* Col 4: Documentation */}
        <div className="space-y-2">
          <div className="text-white font-bold tracking-wider uppercase mb-3 text-[11px]">Specifications</div>
          <ul className="space-y-2 text-slate-400">
            <li className="hover:text-white transition-colors cursor-pointer">CONTEXT.md (Binding Spec)</li>
            <li className="hover:text-white transition-colors cursor-pointer">shared/schemas.py (Data Contract)</li>
            <li className="hover:text-white transition-colors cursor-pointer">shared/electrical.py (Site Model)</li>
            <li className="hover:text-white transition-colors cursor-pointer">AGENTS.md (Constitution)</li>
          </ul>
        </div>
      </div>

      <div className="max-w-7xl mx-auto pt-6 border-t border-white/10 flex flex-col sm:flex-row items-center justify-between text-[11px] text-slate-500 gap-4">
        <div>VoltSentry Security NOC © 2026. All rights reserved. No cloud dependencies.</div>
        <div className="flex space-x-6">
          <span className="hover:text-white cursor-pointer transition-colors">PRIVACY POLICY</span>
          <span className="hover:text-white cursor-pointer transition-colors">TERMS OF PROXY</span>
          <span className="hover:text-white cursor-pointer transition-colors">NOC STATUS</span>
        </div>
      </div>
    </footer>
  );
}
