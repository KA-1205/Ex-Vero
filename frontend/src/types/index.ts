// Frontend contract types. API types mirror docs/API.md; UI types are adapters
// used by the existing numeric screens.
export type ConnectivityState = 'OFFLINE' | 'DEGRADED' | 'ONLINE'
export type ApiConnectivity = 'offline' | 'degraded' | 'full'
export type NetworkMode = 'offline' | 'degraded' | 'full'
export type Verdict = 'KEEP_LOCAL' | 'QUEUE_LOW' | 'QUEUE_HIGH' | 'REDACT_AND_QUEUE' | 'REJECT' | 'KEPT_LOCAL' | 'QUEUED' | 'SYNCED_NOW' | 'REJECTED'
export type SyncPriority = 'URGENT' | 'ROUTINE' | 'HELD'
export type MemoryState = 'local_only' | 'synced' | 'pending'
export type Modality = 'text' | 'vision'
export type ActivityKind = 'capture' | 'decision' | 'push_attempt' | 'push_result' | 'pull_result' | 'consensus' | 'retraction' | 'error' | 'mode_change'

export interface ApiDevice {
  id: string; name: string; connectivity: ApiConnectivity
  memory: { used: number; cap: number }; last_sync_at: string | null
  trust: number; activity_sparkline: number[]
  kind?: string; zone?: string; latitude?: number; longitude?: number
}
export interface DeviceSummary {
  id: string; name: string; kind: string; connectivity: ConnectivityState
  memory_used: number; memory_cap: number; last_sync_at: string | null
  activity_sparkline: number[]; latitude?: number; longitude?: number
}
export interface DecisionEvent {
  device_id: string; point_id: number; value_preview: string; modality: Modality
  thumbnail_url: string | null; verdict: Verdict; reason: string; timestamp: string
}
export interface DecisionFeedEntry {
  id: string; device_id: string; point_id?: number; timestamp: string; content_preview: string
  thumbnail_url?: string | null; modality: Modality; verdict: Verdict; reason: string
  urgency_score?: number; value_preview?: string
}
export interface ActivityEntry {
  device_id: string; kind: ActivityKind; detail: string; point_id?: number | null; timestamp: string
  id?: string; confidence?: number
}
export type DeviceEventFrame = { type: 'decision'; data: DecisionEvent } | { type: 'activity'; data: ActivityEntry }
export interface MemoryPoint {
  id: string; value: string; modality: Modality; thumbnail_url: string | null; zone: string | null
  corroboration_key: string; sync_state: MemoryState; model: string; model_version: string; created_at: string
}
export interface MemoryPointWithScore extends MemoryPoint { score?: number }
export interface MemoryDetail {
  point: MemoryPoint; decision: DecisionEvent; activity: ActivityEntry[]; consensus: ApiConsensusEvent | null
}
export interface MemoryRecord {
  id: string; device_id: string; content_preview: string; thumbnail_url?: string; modality: Modality
  state: MemoryState; zone?: string; status?: string; decision_reason: string; sync_verdict?: string
  captured_at: string; activity_ids: string[]; detail?: MemoryDetail; corroboration_key?: string
}
export interface QuerySource { id: string; score: number; value: string; consensus_state: string }
export interface QueryResultHit { id: string; score: number; value?: string; payload?: { value?: string } }
export interface QueryResult {
  answer: string; answer_path: 'offline' | 'online' | 'extractive'; model: string; latency_ms: number
  sources: QuerySource[]; results: QueryResultHit[]
}
export interface CaptureRequest {
  device_id: string; value: string; corroboration_key: string; zone: string; entity?: string
  status?: string; reporter_device_id: string; file?: File; caption?: string
}
export interface ConflictRecord {
  status: 'POSSIBLE_CONFLICT'; new_point_id: number; existing_point_id: number; score: number
  zone: string; new_value: string; existing_value: string; new_key: string; existing_key: string
}
export interface CaptureResponse {
  id: string; verdict: Verdict; reason: string; conflicts: ConflictRecord[]
  modality?: Modality; thumbnail_url?: string
}
export interface ApiSyncStatus {
  pending: Record<SyncPriority, number>; last_attempt_at: string | null; last_success_at: string | null
  consecutive_failures: number; next_backoff_ms: number; last_push: null | {
    bytes: number; duration_ms: number; points_attempted: number; points_accepted: number
    points_failed: number; mode: NetworkMode
  }; last_pull: null | { at: string; points_received: number }
}
export interface SyncStatus {
  device_id: string; last_attempt_at: string | null; last_success_at: string | null
  consecutive_failures: number; next_backoff_s: number | null
  pending_by_priority: Record<SyncPriority, number>; last_push_bytes: number | null
  last_push_duration_ms: number | null; last_push_mode: NetworkMode | null
  last_pull_at: string | null; last_pull_points: number | null
}
export interface ApiDeviceTelemetry {
  target_label: string; cpu_pct: number; ram_mb: number; ram_limit_mb: number; model_load_ms: number
  query_latency_p50_ms: number; query_latency_p95_ms: number
}
export interface TelemetrySample {
  timestamp: string; cpu_pct: number; ram_mb: number; query_latency_ms: number; model_load_ms?: number; target_label?: string
}
export interface WeatherConditions {
  temperature_c: number; apparent_temperature_c: number; relative_humidity_pct: number
  wind_speed_kmh: number; visibility_km: number; weather_code: number; observed_at: string
}
export interface ConsensusCandidate { value: string; devices: string[]; weight: number }
export interface ApiConsensusEvent {
  corroboration_key: string; state: 'CONFIRMED' | 'DISPUTED' | 'RESOLVED_LWW' | 'RETRACTED'
  confidence: number; resolved_value: string | null; candidates: ConsensusCandidate[]
  explanation: string; timestamp: string
}
export type ConsensusOutcome = 'CONFIRMED' | 'DISPUTED' | 'LWW'
export interface DeviceClaim { device_id: string; value: string; trust_score: number; reported_at: string }
export interface ConsensusEvent {
  id: string; corroboration_key: string; timestamp: string; outcome: ConsensusOutcome
  confidence: number; claims: DeviceClaim[]; resolution_summary: string
}
export interface CloudFact {
  id: string; corroboration_key: string; zone?: string; summary: string; status: 'CONFIRMED' | 'DISPUTED'
  confidence: number; corroborating_devices: string[]; last_updated: string
}
export interface ApiCloudFact {
  corroboration_key: string; state: 'CONFIRMED' | 'DISPUTED'; value: string; confidence: number
  corroborating_devices: string[]; updated_at: string
}
export interface CloudState { facts: ApiCloudFact[]; device_trust: Record<string, number> }
export interface NetworkModeState { mode: NetworkMode }
export interface ResolverBenchmark { resolver_accuracy: number; lww_accuracy: number; scenarios: number; trajectory: { seq: number; event: string; resolver: number; lww: number }[] }
export interface RecallBenchmark { dense_recall_at_5: number; hybrid_recall_at_5: number; labeled_queries: number }
