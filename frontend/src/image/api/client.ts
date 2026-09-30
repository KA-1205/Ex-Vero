// Numeric frontend REST/WebSocket client. Shapes follow docs/API.md; mocks remain
// behind the existing switch while backend endpoints are brought online.
import type {
  ActivityEntry, ActivityKind, ApiCloudFact, ApiDevice, ApiDeviceTelemetry, ApiSyncStatus,
  CaptureRequest, CaptureResponse, CloudFact, CloudState, ConsensusEvent, DecisionEvent,
  DecisionFeedEntry, DeviceEventFrame, DeviceSummary, MemoryDetail, MemoryPoint, MemoryRecord,
  NetworkMode, NetworkModeState, QueryResult, SyncStatus, TelemetrySample, WeatherConditions,
  RecallBenchmark, ResolverBenchmark,
} from '../types'
import * as mock from './mock'

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? ''
const WS_BASE_URL = import.meta.env.VITE_WS_BASE_URL ?? (() => {
  if (BASE_URL) {
    const apiUrl = new URL(BASE_URL, location.origin)
    return `${apiUrl.protocol === 'https:' ? 'wss' : 'ws'}://${apiUrl.host}`
  }
  return `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.hostname}:8000`
})()

export function assetUrl(path: string | null | undefined): string | null {
  if (!path) return null
  if (/^https?:\/\//.test(path)) return path
  return `${BASE_URL}${path}`
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
export function isVision(p: any): boolean {
  if (!p) return false
  if (p.modality === 'vision') return true
  if (p.thumbnail_url) return true
  if (p.payload && (p.payload.modality === 'vision' || p.payload.thumbnail_url)) return true
  if (p.kind === 'capture' && typeof p.detail === 'string' && p.detail.includes('vision')) return true
  if (typeof p.device_id === 'string' && p.device_id.startsWith('cam-')) return true
  if (Array.isArray(p.corroborating_devices) && p.corroborating_devices.some((d: string) => d.startsWith('cam-'))) return true
  if (typeof p.corroboration_key === 'string' && p.corroboration_key.includes('fire_status')) return true
  return false
}

function apiDevice(d: ApiDevice): DeviceSummary {
  return { id: d.id, name: d.name, kind: 'edge node', connectivity: d.connectivity === 'full' ? 'ONLINE' : d.connectivity.toUpperCase() as DeviceSummary['connectivity'], memory_used: d.memory.used, memory_cap: d.memory.cap, last_sync_at: d.last_sync_at, activity_sparkline: d.activity_sparkline }
}
function apiFact(f: ApiCloudFact, i: number): CloudFact {
  return {
    id: `${f.corroboration_key}-${i}`,
    corroboration_key: f.corroboration_key,
    zone: f.corroboration_key.split('.')[0]?.replace(/^zone_/, 'Zone ').replace(/_/g, ' ').toUpperCase(),
    summary: f.value,
    status: f.state,
    confidence: f.confidence,
    corroborating_devices: f.corroborating_devices,
    last_updated: f.updated_at,
    thumbnail_url: assetUrl(f.thumbnail_url),
    modality: f.modality ?? 'vision',
  }
}
function apiFeed(e: DecisionEvent): DecisionFeedEntry {
  return { ...e, id: String(e.point_id), content_preview: e.value_preview, thumbnail_url: assetUrl(e.thumbnail_url) }
}
function apiActivity(e: ActivityEntry): ActivityEntry { return { ...e, id: e.point_id == null ? `${e.device_id}-${e.timestamp}-${e.kind}` : String(e.point_id) } }
function apiMemory(p: MemoryPoint, deviceId: string): MemoryRecord {
  return {
    id: String(p.id),
    device_id: deviceId,
    content_preview: p.value,
    thumbnail_url: assetUrl(p.thumbnail_url) ?? undefined,
    modality: p.modality,
    state: p.sync_state,
    zone: p.zone ?? undefined,
    decision_reason: '',
    captured_at: typeof p.created_at === 'number' ? new Date(p.created_at / 1e6).toISOString() : String(p.created_at),
    activity_ids: [],
    corroboration_key: p.corroboration_key,
    vision: p.vision,
  }
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
    const result = { answer: `Based on retrieved local memory: "${question}" — nearest hazard reports indicate Zone C requires attention.`, answer_path: 'offline' as const, model: 'mock-labeled', latency_ms: Math.floor(4 + Math.random() * 28), sources: [], results: [] }
    return { ...result, served_by: 'edge', source_ids: [] }
  }
  const raw = await postJSON<any>(`/devices/${encodeURIComponent(deviceId)}/query`, { text: question, answer: true, limit: 10 })
  const mappedResults: import('../types').MemoryPointWithScore[] = (raw.results || []).map((r: any) => {
    const payload = r.payload || {}
    const meta = payload._sync_meta || {}
    const syncState = meta.synced === 1 ? 'synced' : (meta.syncable === 0 ? 'local_only' : 'pending')
    return {
      id: r.id,
      score: r.score,
      value: payload.value ?? '',
      modality: payload.modality ?? 'text',
      thumbnail_url: assetUrl(payload.thumbnail_url),
      zone: payload.zone ?? null,
      corroboration_key: payload.corroboration_key ?? '',
      sync_state: syncState,
      model: payload.model ?? '',
      model_version: payload.model_version ?? '',
      created_at: payload.client_timestamp_ns ? new Date(payload.client_timestamp_ns / 1e6).toISOString() : new Date().toISOString(),
      vision: payload.vision,
    }
  })
  return {
    ...raw,
    results: mappedResults,
    served_by: raw.answer_path === 'offline' || raw.answer_path === 'extractive' ? 'edge' : 'cloud',
    source_ids: (raw.sources || []).map((source: any) => String(source.id)),
  }
}
export async function captureFact(req: CaptureRequest): Promise<CaptureResponse> {
  if (mock.USE_MOCKS) {
    await new Promise((r) => setTimeout(r, 250))
    return { id: Date.now(), verdict: 'QUEUE_LOW', reason: 'Mock response: capture accepted for demo.', conflicts: [], modality: req.file ? 'vision' : 'text' }
  }
  const path = `/devices/${encodeURIComponent(req.device_id)}/capture`
  let file = req.file
  if (!file && req.image_data_url) {
    const response = await fetch(req.image_data_url)
    const blob = await response.blob()
    file = new File([blob], 'image-capture', { type: blob.type || 'image/jpeg' })
  }
  const value = req.value ?? req.text ?? req.caption ?? ''
  const corroboration_key = req.corroboration_key?.trim() || 'server_room_A.fire_status'
  const zone = req.zone?.trim() || 'server_room_A'
  if (file) {
    const body = new FormData()
    body.append('file', file)
    body.append('corroboration_key', corroboration_key)
    body.append('zone', zone)
    if (req.entity) body.append('entity', req.entity)
    if (req.caption ?? value) body.append('caption', req.caption ?? value)
    const res = await fetch(`${BASE_URL}${path}`, { method: 'POST', body })
    if (!res.ok) throw new Error(`POST ${path} failed: ${res.status}`)
    return res.json()
  }
  return postJSON(path, { value, corroboration_key, zone, entity: req.entity, status: req.status ?? 'unverified', reporter_device_id: req.reporter_device_id ?? req.device_id })
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
  return { device_id: deviceId, last_attempt_at: s.last_attempt_at, last_success_at: s.last_success_at, consecutive_failures: s.consecutive_failures, next_backoff_s: s.next_backoff_ms / 1000, pending_by_priority: s.pending, last_push_bytes: s.last_push?.bytes ?? null, last_push_duration_ms: s.last_push?.duration_ms ?? null, last_push_mode: s.last_push?.mode ?? null, last_push_attempted: s.last_push?.points_attempted ?? null, last_push_accepted: s.last_push?.points_accepted ?? null, last_push_failed: s.last_push?.points_failed ?? null, last_pull_at: s.last_pull?.at ?? null, last_pull_points: s.last_pull?.points_received ?? null }
}
export async function fetchActivity(deviceId: string, kind?: ActivityKind, limit = 100): Promise<ActivityEntry[]> {
  if (mock.USE_MOCKS) return mock.mockActivity(deviceId).filter((e) => !kind || e.kind === kind).slice(0, limit)
  const p = new URLSearchParams({ limit: String(limit) }); if (kind) p.set('kind', kind)
  const { entries } = await getJSON<{ entries: ActivityEntry[] }>(`/devices/${encodeURIComponent(deviceId)}/activity?${p}`)
  return entries.filter(isVision).map(apiActivity)
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
  const params = new URLSearchParams()
  // Task A1: Image client sends modality=vision on GET /devices/{id}/memory
  const effectiveFilters: MemoryFilters = { modality: 'vision', ...filters }
  Object.entries(effectiveFilters).forEach(([k, v]) => { if (v != null && v !== '') params.set(k, String(v)) })
  const suffix = params.size ? `?${params}` : ''
  const { points } = await getJSON<{ points: MemoryPoint[]; total: number }>(`/devices/${encodeURIComponent(deviceId)}/memory${suffix}`)
  return points.map((p) => apiMemory(p, deviceId))
}
export async function fetchMemoryDetail(deviceId: string, pointId: string): Promise<MemoryDetail> {
  if (mock.USE_MOCKS) {
    const row = mock.mockMemoryRecords(deviceId).find((item) => item.id === pointId) ?? mock.mockMemoryRecords(deviceId)[0]
    return { point: { id: Number(pointId.replace(/\D/g, '')) || 1, value: row.content_preview, modality: row.modality, thumbnail_url: row.thumbnail_url ?? null, zone: row.zone ?? null, corroboration_key: `zone_${(row.zone ?? 'C').slice(-1).toLowerCase()}.hazard`, sync_state: row.state, model: 'demo-model', model_version: 'mock', created_at: row.captured_at }, decision: { device_id: deviceId, point_id: Number(pointId.replace(/\D/g, '')) || 1, value_preview: row.content_preview, modality: row.modality, thumbnail_url: row.thumbnail_url ?? null, verdict: 'QUEUE_LOW', reason: row.decision_reason, timestamp: row.captured_at }, activity: [], consensus: null }
  }
  const detail = await getJSON<any>(`/devices/${encodeURIComponent(deviceId)}/memory/${encodeURIComponent(pointId)}`)
  return {
    ...detail,
    point: {
      ...detail.point,
      vision: detail.point?.vision,
      created_at: typeof detail.point?.created_at === 'number' ? new Date(detail.point.created_at / 1e6).toISOString() : String(detail.point?.created_at || ''),
    }
  }
}
export async function fetchCloudState(): Promise<CloudFact[]> {
  if (mock.USE_MOCKS) return mock.mockCloudState()
  const { facts } = await getJSON<CloudState>('/cloud/state')
  return facts.filter(isVision).map(apiFact)
}
export async function fetchCloudDashboard(): Promise<{ facts: CloudFact[]; device_trust: Record<string, number> }> {
  if (mock.USE_MOCKS) return { facts: mock.mockCloudState(), device_trust: { 'cam-01': 0.95, 'cam-02': 0.88, 'cam-03': 0.72 } }
  const state = await getJSON<CloudState>('/cloud/state')
  return { facts: state.facts.filter(isVision).map(apiFact), device_trust: state.device_trust }
}
export async function fetchDeviceConflicts(deviceId: string) {
  if (mock.USE_MOCKS) return { device_id: deviceId, conflicts: [] as import('../types').ConflictRecord[] }
  return getJSON<{ device_id: string; conflicts: import('../types').ConflictRecord[] }>(`/devices/${encodeURIComponent(deviceId)}/conflicts`)
}
export async function fetchResolverBenchmark(): Promise<ResolverBenchmark> {
  if (mock.USE_MOCKS) return { resolver_accuracy: 0.94, lww_accuracy: 0.71, scenarios: 300, trajectory: [] }
  return getJSON('/benchmark/resolver-vs-lww')
}
export async function fetchRecallBenchmark(): Promise<RecallBenchmark> {
  if (mock.USE_MOCKS) return { dense_recall_at_5: 0.62, hybrid_recall_at_5: 0.84, labeled_queries: 40 }
  return getJSON('/benchmark/recall')
}
export async function fetchDecisionFeed(deviceId: string, limit = 50): Promise<DecisionFeedEntry[]> {
  if (mock.USE_MOCKS) return mock.mockDecisionFeed(deviceId, limit)
  const { events } = await getJSON<{ events: DecisionEvent[] }>(`/devices/${encodeURIComponent(deviceId)}/feed?limit=${limit}`)
  return events.filter(isVision).map(apiFeed)
}
export function subscribeDeviceEvents(deviceId: string, onEvent: (frame: DeviceEventFrame) => void): () => void {
  if (mock.USE_MOCKS) {
    let i = 0; const seed = mock.mockDecisionFeed(deviceId, 50)
    const interval = setInterval(() => { const e = seed[i++ % seed.length]; onEvent({ type: 'decision', data: { device_id: deviceId, point_id: Number(e.point_id) || i, value_preview: e.content_preview, modality: e.modality, thumbnail_url: e.thumbnail_url ?? null, verdict: e.verdict, reason: e.reason, timestamp: new Date().toISOString() } }) }, 3500)
    return () => clearInterval(interval)
  }
  const ws = new WebSocket(`${WS_BASE_URL}/devices/${encodeURIComponent(deviceId)}/events`)
  ws.onmessage = (msg) => {
    try {
      const frame = JSON.parse(msg.data) as DeviceEventFrame
      if (isVision(frame.data)) {
        onEvent(frame)
      }
    } catch (error) {
      console.error('Invalid device event frame', error)
    }
  }
  return () => ws.close()
}
export async function pushDevice(deviceId: string) { return postJSON(`/devices/${encodeURIComponent(deviceId)}/push`, {}) }
export async function pullDevice(deviceId: string) { return postJSON(`/devices/${encodeURIComponent(deviceId)}/pull`, {}) }
export async function retractPoint(deviceId: string, pointId: string) { if (mock.USE_MOCKS) return { retracted: true, point_id: Number(pointId.replace(/\D/g, '')) || 1 }; return postJSON(`/devices/${encodeURIComponent(deviceId)}/retract/${encodeURIComponent(pointId)}`, {}) }
export async function injectConflict(corroboration_key: string, assignments: Record<string, string>) { if (mock.USE_MOCKS) return { injected: true }; return postJSON('/demo/inject-conflict', { corroboration_key, assignments }) }
export async function setRogueMode(deviceId: string, rogue: boolean) { if (mock.USE_MOCKS) return { id: deviceId, rogue }; return postJSON(`/devices/${encodeURIComponent(deviceId)}/rogue`, { rogue }) }
export function subscribeConsensusEvents(onEvent: (entry: ConsensusEvent) => void): () => void {
  if (mock.USE_MOCKS) return () => undefined
  const ws = new WebSocket(`${WS_BASE_URL}/consensus/events`)
  ws.onmessage = (msg) => { try {
    const event = JSON.parse(msg.data) as import('../types').ApiConsensusEvent
    onEvent({
      id: `${event.corroboration_key}-${event.timestamp}`,
      corroboration_key: event.corroboration_key,
      timestamp: event.timestamp,
      outcome: event.state === 'RESOLVED_LWW' ? 'LWW' : event.state === 'RETRACTED' ? 'DISPUTED' : event.state,
      confidence: event.confidence,
      claims: event.candidates.flatMap((candidate) => candidate.devices.map((device_id) => ({ device_id, value: candidate.value, trust_score: candidate.weight, reported_at: event.timestamp }))),
      resolution_summary: event.explanation,
      modal_votes: event.modal_votes,
    })
  } catch (error) { console.error('Invalid consensus event', error) } }
  return () => ws.close()
}

export interface TrendFrame {
  point_id: number
  thumbnail_url: string
  timestamp: string
  drift_from_prev?: number
}
export interface DeviceTrend {
  device_id: string
  corroboration_key: string
  status: 'EVOLVING' | 'STABLE'
  frames: TrendFrame[]
  latest_drift: number
}
export async function fetchDeviceTrend(deviceId: string, key?: string): Promise<DeviceTrend | null> {
  try {
    const q = key ? `?key=${encodeURIComponent(key)}` : ''
    return await getJSON<DeviceTrend>(`/devices/${encodeURIComponent(deviceId)}/trend${q}`)
  } catch {
    return null
  }
}

