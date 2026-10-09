"use client";

/**
 * useNetworkFeed — live snapshot of the VoltSentry federation from the hub.
 *
 * The hub (python -m federation.hub) pushes one NetworkSnapshot per second on
 * ws://localhost:7701/network. The socket is only opened while a component
 * using this hook is mounted, and it reconnects every 3 s if the hub is down.
 */

import { useEffect, useState } from "react";
import type { NetworkSnapshot } from "./types";

export const NETWORK_FEED_URL = "ws://localhost:7701/network";

export function useNetworkFeed(url: string = NETWORK_FEED_URL) {
  const [snapshot, setSnapshot] = useState<NetworkSnapshot | null>(null);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let ws: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;

    const connect = () => {
      if (cancelled) return;
      ws = new WebSocket(url);
      ws.onopen = () => !cancelled && setConnected(true);
      ws.onmessage = (msg) => {
        if (cancelled) return;
        try {
          const data = JSON.parse(msg.data);
          if (data?.event === "network") setSnapshot(data as NetworkSnapshot);
        } catch {
          /* ignore a malformed frame */
        }
      };
      ws.onclose = () => {
        if (cancelled) return;
        setConnected(false);
        retry = setTimeout(connect, 3000);
      };
      ws.onerror = () => ws?.close();
    };

    connect();
    return () => {
      cancelled = true;
      if (retry) clearTimeout(retry);
      ws?.close();
    };
  }, [url]);

  return { snapshot, connected };
}
