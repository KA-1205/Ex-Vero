// Shared types for the Aegis Edge frontend.
// Mirrors the API surface defined in backend.md §6 / frontend.md §4.
// The frontend is a thin renderer over this contract — no policy logic here.

export type ConnectivityState = 'OFFLINE' | 'DEGRADED' | 'ONLINE'
export type NetworkMode = 'offline' | 'degraded' | 'full'

export type Verdict =
  | 'KEPT_LOCAL'
  | 'QUEUED'
  | 'SYNCED_NOW'
  | 'REJECTED'
  | 'REDACT_AND_QUEUE'

export type SyncPriority = 'URGENT' | 'ROUTINE' | 'HELD'
export type MemoryState = 'local_only' | 'synced' | 'pending'
export type Modality = 'text' | 'vision'

export type ActivityKind =
  | 'capture'
  | 'decision'
  | 'push_attempt'
  | 'push_result'
  | 'pull_result'
  | 'consensus'
  | 'retraction'
  | 'error'
  | 'mode_change'

// --- GET /devices --------------------------------------------------------

export interface DeviceSummary {
  id: string
  name: string
  kind: string // "paramedic tablet" | "kiosk" | "pi node" | ...
  connectivity: ConnectivityState
  memory_used: number
  memory_cap: number
  last_sync_at: string | null // ISO timestamp
  activity_sparkline: number[] // recent decision-engine activity, most-recent-last
  latitude?: number
  longitude?: number
}

// --- WS /devices/{id}/events ---------------------------------------------

export interface DecisionFeedEntry {
  id: string
  device_id: string
  timestamp: string
  content_preview: string
  thumbnail_url?: string
  modality: Modality
  verdict: Verdict
  reason: string // plain-language, e.g. "98% similar to entry 4m ago — discarded as redundant"
  urgency_score?: number
}

// --- POST /devices/{id}/query ---------------------------------------------

export interface QueryResult {
  answer: string
  latency_ms: number
  served_by: 'edge' | 'cloud'
  source_ids: string[]
}

// --- POST /devices/{id}/capture --------------------------------------------

export interface CaptureRequest {
  device_id: string
  modality: Modality
  text?: string
  image_data_url?: string
}

// --- GET /devices/{id}/sync (backend.md §6.6) ------------------------------

export interface SyncStatus {
  device_id: string
  last_attempt_at: string | null
  last_success_at: string | null
  consecutive_failures: number
  next_backoff_s: number | null
  pending_by_priority: Record<SyncPriority, number>
  last_push_bytes: number | null
  last_push_duration_ms: number | null
  last_push_mode: NetworkMode | null
  last_pull_at: string | null
  last_pull_points: number | null
}

// --- GET /devices/{id}/activity (backend.md §6.6) --------------------------

export interface ActivityEntry {
  id: string
  timestamp: string
  device_id: string
  kind: ActivityKind
  detail: string // "pushed 4 points, 2 failed, 18.2 KB, mode=degraded"
  verdict?: Verdict
  confidence?: number
}

// --- Local Memory Browser (3.2 right panel) --------------------------------

export interface MemoryRecord {
  id: string
  device_id: string
  content_preview: string
  thumbnail_url?: string
  modality: Modality
  state: MemoryState
  zone?: string
  status?: string
  decision_reason: string
  sync_verdict?: string
  captured_at: string
  activity_ids: string[] // links into ActivityEntry for the "expand to see why" behavior
}

// --- GET /devices/{id}/telemetry --------------------------------------------

export interface TelemetrySample {
  timestamp: string
  cpu_pct: number
  ram_mb: number
  query_latency_ms: number
  model_load_ms?: number
}

export interface WeatherConditions {
  temperature_c: number
  apparent_temperature_c: number
  relative_humidity_pct: number
  wind_speed_kmh: number
  visibility_km: number
  weather_code: number
  observed_at: string
}

// --- WS /consensus/events + Conflict Theater (3.4) ---------------------------

export type ConsensusOutcome = 'CONFIRMED' | 'DISPUTED' | 'LWW'

export interface DeviceClaim {
  device_id: string
  value: string
  trust_score: number
  reported_at: string
}

export interface ConsensusEvent {
  id: string
  corroboration_key: string
  timestamp: string
  outcome: ConsensusOutcome
  confidence: number
  claims: DeviceClaim[]
  resolution_summary: string // "Zone-C hazard confirmed — 3 corroborating devices..."
}

export interface TrustHistoryPoint {
  device_id: string
  at: string
  trust_score: number
  reason?: string // "after 3 disagreements"
}

// --- GET /cloud/state (Command Dashboard, 3.5) -------------------------------

export type FactStatus = 'CONFIRMED' | 'DISPUTED'

export interface CloudFact {
  id: string
  corroboration_key: string
  zone?: string
  summary: string
  status: FactStatus
  confidence: number
  corroborating_devices: string[]
  last_updated: string
}

// --- Network simulator (persistent top bar control) --------------------------

export interface NetworkModeState {
  mode: NetworkMode
  set_at: string
}
