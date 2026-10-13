export type StationStatus =
  | "Available"
  | "Preparing"
  | "Charging"
  | "Finishing"
  | "Quarantined"
  | "Offline";

/** Units are in the field names: _kw, _kva, _kwh, _v, _a, _hz (mirrors shared/schemas.py). */
export interface TelemetryEvent {
  event: "telemetry";
  station_id: string;
  transaction_id?: number | null;
  ts: number;
  power_kw: number;
  soc: number;
  dp_dt: number;
  duration_sec: number;
  energy_register_kwh: number;
  energy_residual_kwh: number;
  status: StationStatus;
  ml_score: number;
  /** DC output to the vehicle, as reported by the charger's meter. */
  dc_voltage_v?: number | null;
  dc_current_a?: number | null;
  /** AC supply at the charger inlet, line-to-line. */
  ac_voltage_v?: number | null;
  frequency_hz?: number | null;
}

export interface ThreatEvent {
  event: "threat";
  station_id: string;
  ts: number;
  tier: 1 | 2;
  rule_id?: string | null;
  ml_score?: number | null;
  severity: "low" | "medium" | "high";
  reason: string;
  action_taken: "logged" | "frame_dropped" | "quarantined";
  raw_frame?: string | null;
  node_id?: string | null;
  /** Monitor mode only: what enforce mode would have done (action_taken is then "logged"). */
  recommended_action?: "frame_dropped" | "quarantined" | null;
}

/**
 * The site transformer. total_load_kw is the real power it supplies; the
 * rating and headroom are in kVA. Older recordings carry only the first six
 * fields.
 */
export interface GridEvent {
  event: "grid";
  ts: number;
  total_load_kw: number;
  transformer_capacity_kva: number;
  headroom_pct: number;
  active_stations: number;
  node_id?: string | null;
  site_name?: string | null;
  output_kw?: number | null;
  total_load_kva?: number | null;
  loading_pct?: number | null;
  power_factor?: number | null;
  charger_efficiency?: number | null;
  hv_supply_kv?: number | null;
  lt_nominal_v?: number | null;
  lt_voltage_v?: number | null;
  lt_current_a?: number | null;
  frequency_hz?: number | null;
  /** "monitor": detections are reported, nothing is blocked. */
  enforcement?: "enforce" | "monitor" | null;
  /** v3: what this node's defences have done since it started, and how it is set up. */
  defences?: DefenceStats | null;
}

/** Mirrors shared/schemas.py DefenceStats. Counters are since the proxy started. */
export interface DefenceStats {
  frames_inspected: number;
  frames_forwarded: number;
  /** Charger frames held back from the CSMS because of a detection. */
  frames_dropped: number;
  quarantines: number;
  cards_blocked: number;
  duplicate_sessions_refused: number;
  blocklisted_chargers_refused: number;
  oversize_frames_refused: number;
  malformed_connections_refused: number;
  threats_recorded: number;
  ingress_tls: boolean;
  bind: string;
  max_frame_kb: number;
  federation: "standalone" | "connected" | "disconnected";
  federation_dev_key: boolean;
}

export type FeedEvent = TelemetryEvent | ThreatEvent | GridEvent;

// ---- federation hub feed (ws://localhost:7701/network) --------------------

export interface NodeHeartbeat {
  stations_connected?: number;
  charging?: number;
  quarantined?: number;
  output_kw?: number | null;
  input_kw?: number | null;
  load_kva?: number | null;
  transformer_kva?: number | null;
  loading_pct?: number | null;
  lt_voltage_v?: number | null;
  frequency_hz?: number | null;
  uptime_sec?: number;
  mode?: "enforce" | "monitor";
}

export interface NetworkNode {
  node_id: string;
  site_name: string;
  region: string;
  lat: number | null;
  lon: number | null;
  version: string;
  transformer_kva: number | null;
  bays: number | null;
  status: "online" | "stale" | "offline";
  last_seen_age_sec: number | null;
  connected_since: number | null;
  threats_total: number;
  live_sessions: number;
  heartbeat: NodeHeartbeat;
}

export interface NetworkIndicator {
  ioc_id: string;
  kind: "id_tag" | "charge_point";
  value: string;
  reason: string;
  severity: "low" | "medium" | "high";
  source_node: string;
  first_seen: number;
  expires: number;
  hits: number;
  ttl_remaining_sec: number;
}

export interface NetworkThreat {
  station_id: string;
  ts: number;
  tier: 1 | 2;
  rule_id: string | null;
  ml_score: number | null;
  severity: "low" | "medium" | "high";
  reason: string;
  action_taken: string;
  recommended_action?: "frame_dropped" | "quarantined" | null;
  node_id: string;
}

export interface NetworkSnapshot {
  event: "network";
  ts: number;
  hub: {
    uptime_sec: number;
    tls: boolean;
    dev_secret: boolean;
    nodes_online: number;
    messages_in: number;
    relayed: number;
    auth_failures: number;
    rejected: number;
  };
  nodes: NetworkNode[];
  iocs: NetworkIndicator[];
  threats: NetworkThreat[];
  events: { ts: number; level: "info" | "warn" | "alert"; text: string }[];
}
