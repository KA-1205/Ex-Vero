// Numeric frontend REST/WebSocket client. Shapes follow docs/API.md; mocks remain
// behind the existing switch while backend endpoints are brought online.
import type {
  ActivityEntry, ActivityKind, ApiCloudFact, ApiConsensusEvent, ApiDevice, ApiDeviceTelemetry, ApiSyncStatus,
  CaptureRequest, CaptureResponse, CloudFact, CloudState, ConsensusEvent, DecisionEvent,
  DecisionFeedEntry, DeviceEventFrame, DeviceSummary, MemoryDetail, MemoryPoint, MemoryRecord,
  NetworkMode, NetworkModeState, QueryResult, SyncStatus, TelemetrySample, WeatherConditions,
  RecallBenchmark, ResolverBenchmark,
} from '../types'
import * as mock from './mock'

// The edge node runs locally on :8000 and is deliberately not deployed (its
// facts live in an on-disk shard, which a serverless function cannot persist).
// VITE_API_BASE_URL overrides this for anyone fronting the node differently.
const BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000').replace(/\/+$/, '')
const WS_BASE_URL = (import.meta.env.VITE_WS_BASE_URL ?? 'ws://localhost:8000').replace(/\/+$/, '')

function apiAsset(url?: string | null): string | null {
  if (!url) return null
  try { return new URL(url, `${BASE_URL}/`).toString() } catch { return url }
}

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`)
  if (!res.ok) throw new Error(`GET ${path} failed: ${res.status}`)
  return res.json() as Promise<T>
}
async function postJSON<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  if (!res.ok) throw new Error(`POST ${path} failed: ${res.status}`)
  return res.json() as Promise<T>
}
function apiDevice(d: ApiDevice): DeviceSummary {
  return { id: d.id, name: d.name, kind: d.kind ?? 'edge node', connectivity: d.connectivity === 'full' ? 'ONLINE' : d.connectivity.toUpperCase() as DeviceSummary['connectivity'], memory_used: d.memory.used, memory_cap: d.memory.cap, last_sync_at: d.last_sync_at, activity_sparkline: d.activity_sparkline, latitude: d.latitude, longitude: d.longitude }
}
function apiFact(f: ApiCloudFact, i: number): CloudFact {
  return { id: `${f.corroboration_key}-${i}`, corroboration_key: f.corroboration_key, zone: f.corroboration_key.split('.')[0]?.replace(/^zone_/, 'Zone ').toUpperCase(), summary: f.value, status: f.state, confidence: f.confidence, corroborating_devices: f.corroborating_devices, last_updated: f.updated_at }
}
function apiFeed(raw: DecisionEvent | Record<string, unknown>): DecisionFeedEntry {
  const e = raw as DecisionEvent & { payload?: { value?: string; modality?: DecisionFeedEntry['modality']; thumbnail_url?: string | null } }
  const pointId = Number(e.point_id ?? 0)
  const value = e.value_preview ?? e.payload?.value ?? ''
  return { device_id: e.device_id, point_id: pointId, id: String(pointId), timestamp: e.timestamp, content_preview: value.slice(0, 160), value_preview: value.slice(0, 160), modality: e.modality ?? e.payload?.modality ?? 'text', thumbnail_url: apiAsset(e.thumbnail_url ?? e.payload?.thumbnail_url) ?? null, verdict: e.verdict, reason: e.reason }
}
function apiActivity(e: ActivityEntry): ActivityEntry { return { ...e, id: `${e.device_id}-${e.timestamp}-${e.kind}-${e.point_id ?? ''}` } }
function apiMemory(p: MemoryPoint, deviceId: string): MemoryRecord {
  return { id: String(p.id), device_id: deviceId, content_preview: p.value, thumbnail_url: apiAsset(p.thumbnail_url) ?? undefined, modality: p.modality, state: p.sync_state, zone: p.zone ?? undefined, decision_reason: '', captured_at: p.created_at, activity_ids: [], corroboration_key: p.corroboration_key }
}

export async function fetchDevices(): Promise<DeviceSummary[]> {
  if (mock.USE_MOCKS) return mock.mockDevices()
  const response = await getJSON<{ devices: ApiDevice[] }>('/devices')
  return response.devices.map(apiDevice)
}
export async function fetchDevice(deviceId: string): Promise<DeviceSummary> {
  if (mock.USE_MOCKS) { const d = mock.mockDevices().find((x) => x.id === deviceId); if (!d) throw new Error('Device not found'); return d }
  return apiDevice(await getJSON<ApiDevice>(`/devices/${encodeURIComponent(deviceId)}`))
}
export async function queryDevice(deviceId: string, question: string): Promise<QueryResult> {
  if (mock.USE_MOCKS) {
    await new Promise((r) => setTimeout(r, 120 + Math.random() * 200))
    return { answer: `Based on retrieved local memory: "${question}" — nearest hazard reports indicate Zone C requires attention.`, answer_path: 'offline', model: 'mock-labeled', latency_ms: Math.floor(4 + Math.random() * 28), sources: [], results: [] }
  }
  const response = await postJSON<{
    answer?: string; answer_path?: QueryResult['answer_path']; model?: string; latency_ms: number;
    sources?: QueryResult['sources']; results: { id: number; score: number; payload?: { value?: string }; value?: string }[]
  }>(`/devices/${encodeURIComponent(deviceId)}/query`, { text: question, answer: true, limit: 10 })
  return {
    answer: response.answer ?? 'No answer was generated for this query.',
    answer_path: response.answer_path ?? 'extractive',
    model: response.model ?? 'unknown',
    latency_ms: response.latency_ms,
    sources: response.sources ?? [],
    results: response.results,
  }
}
export async function captureFact(req: CaptureRequest): Promise<CaptureResponse> {
  if (mock.USE_MOCKS) {
    await new Promise((r) => setTimeout(r, 250))
    return { id: Date.now(), verdict: 'QUEUE_LOW', reason: 'Mock response: capture accepted for demo.', conflicts: [], modality: req.file ? 'vision' : 'text' }
  }
  const path = `/devices/${encodeURIComponent(req.device_id)}/capture`
  if (req.file) {
    const body = new FormData()
    body.append('file', req.file)
    body.append('corroboration_key', req.corroboration_key)
    body.append('zone', req.zone)
    if (req.entity) body.append('entity', req.entity)
    if (req.caption) body.append('caption', req.caption)
    const res = await fetch(`${BASE_URL}${path}`, { method: 'POST', body })
    if (!res.ok) throw new Error(`POST ${path} failed: ${res.status}`)
    return res.json()
  }
  return postJSON(path, { device_id: req.device_id, value: req.value, corroboration_key: req.corroboration_key, zone: req.zone, entity: req.entity, status: req.status ?? 'unverified', reporter_device_id: req.reporter_device_id })
}
export async function fetchNetworkMode(): Promise<NetworkMode> {
  if (mock.USE_MOCKS) return 'full'
  return (await getJSON<NetworkModeState>('/network/mode')).mode
}
export async function setNetworkMode(mode: NetworkMode): Promise<void> {
  if (!mock.USE_MOCKS) await postJSON('/network/mode', { mode })
}
export async function fetchSyncStatus(deviceId: string): Promise<SyncStatus> {
  if (mock.USE_MOCKS) return mock.mockSyncStatus(deviceId)
  const s = await getJSON<ApiSyncStatus>(`/devices/${encodeURIComponent(deviceId)}/sync`)
  return { device_id: deviceId, last_attempt_at: s.last_attempt_at, last_success_at: s.last_success_at, consecutive_failures: s.consecutive_failures, next_backoff_s: s.next_backoff_ms / 1000, pending_by_priority: s.pending, last_push_bytes: s.last_push?.bytes ?? null, last_push_duration_ms: s.last_push?.duration_ms ?? null, last_push_mode: s.last_push?.mode ?? null, last_pull_at: s.last_pull?.at ?? null, last_pull_points: s.last_pull?.points_received ?? null }
}
export async function fetchActivity(deviceId: string, kind?: ActivityKind, limit = 100): Promise<ActivityEntry[]> {
  if (mock.USE_MOCKS) return mock.mockActivity(deviceId).filter((e) => !kind || e.kind === kind).slice(0, limit)
  const p = new URLSearchParams({ limit: String(limit) }); if (kind) p.set('kind', kind)
  const { entries } = await getJSON<{ entries: ActivityEntry[] }>(`/devices/${encodeURIComponent(deviceId)}/activity?${p}`)
  return entries.map(apiActivity)
}
export async function fetchTelemetry(deviceId: string): Promise<TelemetrySample[]> {
  if (mock.USE_MOCKS) return mock.mockTelemetry(deviceId)
  const t = await getJSON<ApiDeviceTelemetry>(`/devices/${encodeURIComponent(deviceId)}/telemetry`)
  return [{ timestamp: new Date().toISOString(), cpu_pct: t.cpu_pct, ram_mb: t.ram_mb, query_latency_ms: t.query_latency_p50_ms, model_load_ms: t.model_load_ms, target_label: t.target_label }]
}

export async function fetchWeather(): Promise<WeatherConditions> {
  const params = new URLSearchParams({ latitude: '28.6139', longitude: '77.2090', current: 'temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m,visibility', timezone: 'Asia/Kolkata', wind_speed_unit: 'kmh' })
  const response = await fetch(`https://api.open-meteo.com/v1/forecast?${params}`)
  if (!response.ok) throw new Error(`Weather request failed: ${response.status}`)
  const payload = await response.json() as { current: { time: string; temperature_2m: number; apparent_temperature: number; relative_humidity_2m: number; weather_code: number; wind_speed_10m: number; visibility: number } }
  return { temperature_c: payload.current.temperature_2m, apparent_temperature_c: payload.current.apparent_temperature, relative_humidity_pct: payload.current.relative_humidity_2m, wind_speed_kmh: payload.current.wind_speed_10m, visibility_km: payload.current.visibility / 1000, weather_code: payload.current.weather_code, observed_at: payload.current.time }
}
export interface MemoryFilters { q?: string; sync_state?: string; modality?: string; zone?: string; limit?: number; offset?: number }
export async function fetchMemoryRecords(deviceId: string, filters: MemoryFilters = {}): Promise<MemoryRecord[]> {
  if (mock.USE_MOCKS) return mock.mockMemoryRecords(deviceId).filter((r) => (!filters.q || r.content_preview.toLowerCase().includes(filters.q.toLowerCase())) && (!filters.sync_state || r.state === filters.sync_state) && (!filters.modality || r.modality === filters.modality) && (!filters.zone || r.zone?.toLowerCase().includes(filters.zone.toLowerCase())))
  const params = new URLSearchParams(); Object.entries(filters).forEach(([k, v]) => { if (v != null && v !== '') params.set(k, String(v)) })
  const suffix = params.size ? `?${params}` : ''
  const { points } = await getJSON<{ points: MemoryPoint[]; total: number }>(`/devices/${encodeURIComponent(deviceId)}/memory${suffix}`)
  return points.map((p) => apiMemory(p, deviceId))
}
export async function fetchMemoryDetail(deviceId: string, pointId: string): Promise<MemoryDetail> {
  if (mock.USE_MOCKS) {
    const row = mock.mockMemoryRecords(deviceId).find((item) => item.id === pointId) ?? mock.mockMemoryRecords(deviceId)[0]
    return { point: { id: Number(pointId.replace(/\D/g, '')) || 1, value: row.content_preview, modality: row.modality, thumbnail_url: row.thumbnail_url ?? null, zone: row.zone ?? null, corroboration_key: `zone_${(row.zone ?? 'C').slice(-1).toLowerCase()}.hazard`, sync_state: row.state, model: 'demo-model', model_version: 'mock', created_at: row.captured_at }, decision: { device_id: deviceId, point_id: Number(pointId.replace(/\D/g, '')) || 1, value_preview: row.content_preview, modality: row.modality, thumbnail_url: row.thumbnail_url ?? null, verdict: 'QUEUE_LOW', reason: row.decision_reason, timestamp: row.captured_at }, activity: [], consensus: null }
  }
  const detail = await getJSON<MemoryDetail>(`/devices/${encodeURIComponent(deviceId)}/memory/${encodeURIComponent(pointId)}`)
  return { ...detail, point: { ...detail.point, thumbnail_url: apiAsset(detail.point.thumbnail_url) }, decision: { ...detail.decision, thumbnail_url: apiAsset(detail.decision.thumbnail_url) } }
}
export async function fetchCloudState(): Promise<CloudFact[]> {
  if (mock.USE_MOCKS) return mock.mockCloudState()
  const { facts } = await getJSON<CloudState>('/cloud/state')
  return facts.map(apiFact)
}
export async function fetchCloudDashboard(): Promise<{ facts: CloudFact[]; device_trust: Record<string, number> }> {
  if (mock.USE_MOCKS) return { facts: mock.mockCloudState(), device_trust: { 'dev-01': 0.9, 'dev-02': 0.76, 'dev-03': 0.42, 'dev-04': 0.83 } }
  const state = await getJSON<CloudState>('/cloud/state')
  return { facts: state.facts.map(apiFact), device_trust: state.device_trust }
}
export async function fetchResolverBenchmark(): Promise<ResolverBenchmark> {
  if (mock.USE_MOCKS) return { resolver_accuracy: 0.94, lww_accuracy: 0.71, scenarios: 300, trajectory: [] }
  return getJSON('/benchmark/resolver-vs-lww')
}
export async function fetchRecallBenchmark(): Promise<RecallBenchmark | null> {
  if (mock.USE_MOCKS) return { dense_recall_at_5: 0.62, hybrid_recall_at_5: 0.84, labeled_queries: 40 }
  const benchmark = await getJSON<RecallBenchmark>('/benchmark/recall')
  return benchmark.labeled_queries > 0 ? benchmark : null
}
export async function fetchDecisionFeed(deviceId: string, limit = 50): Promise<DecisionFeedEntry[]> {
  if (mock.USE_MOCKS) return mock.mockDecisionFeed(deviceId, limit)
  const { events } = await getJSON<{ events: (DecisionEvent | Record<string, unknown>)[] }>(`/devices/${encodeURIComponent(deviceId)}/feed?limit=${limit}`)
  return events.map(apiFeed).reverse()
}
export function subscribeDeviceEvents(deviceId: string, onEvent: (frame: DeviceEventFrame) => void): () => void {
  if (mock.USE_MOCKS) {
    let i = 0; const seed = mock.mockDecisionFeed(deviceId, 50)
    const interval = setInterval(() => { const e = seed[i++ % seed.length]; onEvent({ type: 'decision', data: { device_id: deviceId, point_id: Number(e.point_id) || i, value_preview: e.content_preview, modality: e.modality, thumbnail_url: e.thumbnail_url ?? null, verdict: e.verdict, reason: e.reason, timestamp: new Date().toISOString() } }) }, 3500)
    return () => clearInterval(interval)
  }
  const ws = new WebSocket(`${WS_BASE_URL}/devices/${encodeURIComponent(deviceId)}/events`)
  ws.onmessage = (msg) => { try {
    const frame = JSON.parse(msg.data) as DeviceEventFrame
    if (frame.type === 'decision') frame.data.thumbnail_url = apiAsset(frame.data.thumbnail_url)
    if (frame.type === 'activity' && !frame.data.id) frame.data.id = `${frame.data.device_id}-${frame.data.timestamp}-${frame.data.kind}-${frame.data.point_id ?? ''}`
    onEvent(frame)
  } catch (error) { console.error('Invalid device event frame', error) } }
  return () => ws.close()
}
export async function pushDevice(deviceId: string) { return postJSON(`/devices/${encodeURIComponent(deviceId)}/push`, {}) }
export async function pullDevice(deviceId: string) { return postJSON(`/devices/${encodeURIComponent(deviceId)}/pull`, {}) }
export async function retractPoint(deviceId: string, pointId: string) { if (mock.USE_MOCKS) return { retracted: true, point_id: Number(pointId.replace(/\D/g, '')) || 1 }; return postJSON(`/devices/${encodeURIComponent(deviceId)}/retract/${encodeURIComponent(pointId)}`, {}) }
export async function injectConflict(corroboration_key: string, assignments: Record<string, string>): Promise<{ injected: boolean; corroboration_key?: string; consensus?: ApiConsensusEvent }> { if (mock.USE_MOCKS) return { injected: true }; return postJSON('/demo/inject-conflict', { corroboration_key, assignments }) }
export function mapConsensusEvent(event: ApiConsensusEvent): ConsensusEvent {
  return {
    id: `${event.corroboration_key}-${event.timestamp}`,
    corroboration_key: event.corroboration_key,
    timestamp: event.timestamp,
    outcome: event.state === 'RESOLVED_LWW' ? 'LWW' : event.state === 'RETRACTED' ? 'DISPUTED' : event.state,
    confidence: event.confidence,
    claims: event.candidates.flatMap((candidate) => candidate.devices.map((device_id) => ({ device_id, value: candidate.value, trust_score: candidate.weight, reported_at: event.timestamp }))),
    resolution_summary: event.explanation,
  }
}
export async function setRogueMode(deviceId: string, rogue: boolean) { if (mock.USE_MOCKS) return { id: deviceId, rogue }; return postJSON(`/devices/${encodeURIComponent(deviceId)}/rogue`, { rogue }) }
export function subscribeConsensusEvents(onEvent: (entry: ConsensusEvent) => void): () => void {
  if (mock.USE_MOCKS) return () => undefined
  const ws = new WebSocket(`${WS_BASE_URL}/consensus/events`)
  ws.onmessage = (msg) => { try {
    const event = JSON.parse(msg.data) as import('../types').ApiConsensusEvent
    onEvent({ id: `${event.corroboration_key}-${event.timestamp}`, corroboration_key: event.corroboration_key, timestamp: event.timestamp, outcome: event.state === 'RESOLVED_LWW' ? 'LWW' : event.state === 'RETRACTED' ? 'DISPUTED' : event.state, confidence: event.confidence, claims: event.candidates.flatMap((candidate) => candidate.devices.map((device_id) => ({ device_id, value: candidate.value, trust_score: candidate.weight, reported_at: event.timestamp }))), resolution_summary: event.explanation })
  } catch (error) { console.error('Invalid consensus event', error) } }
  return () => ws.close()
}
