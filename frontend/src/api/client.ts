// Thin REST/WebSocket client over the backend contract in backend.md §6.
// The frontend must stay a "thin renderer" (frontend.md preamble) — no
// decision/policy logic belongs here, only fetch/parse/stream.

import type {
  ActivityEntry,
  CaptureRequest,
  CloudFact,
  ConsensusEvent,
  DecisionFeedEntry,
  DeviceSummary,
  MemoryRecord,
  NetworkMode,
  QueryResult,
  SyncStatus,
  TelemetrySample,
} from '../types'
import * as mock from './mock'

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'
const WS_BASE_URL = import.meta.env.VITE_WS_BASE_URL ?? 'ws://localhost:8000'

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`)
  if (!res.ok) throw new Error(`GET ${path} failed: ${res.status}`)
  return res.json() as Promise<T>
}

async function postJSON<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`POST ${path} failed: ${res.status}`)
  return res.json() as Promise<T>
}

// --- GET /devices ------------------------------------------------------

export async function fetchDevices(): Promise<DeviceSummary[]> {
  if (mock.USE_MOCKS) return mock.mockDevices()
  return getJSON('/devices')
}

// --- POST /devices/{id}/query -------------------------------------------

export async function queryDevice(deviceId: string, question: string): Promise<QueryResult> {
  if (mock.USE_MOCKS) {
    await new Promise((r) => setTimeout(r, 120 + Math.random() * 200))
    return {
      answer: `Based on retrieved local memory: "${question}" — nearest hazard reports indicate Zone C requires attention.`,
      latency_ms: Math.floor(4 + Math.random() * 28),
      served_by: Math.random() > 0.5 ? 'edge' : 'cloud',
      source_ids: ['mem-0', 'mem-3'],
    }
  }
  return postJSON(`/devices/${deviceId}/query`, { question })
}

// --- POST /devices/{id}/capture ------------------------------------------

export async function captureFact(req: CaptureRequest): Promise<{ id: string }> {
  if (mock.USE_MOCKS) {
    await new Promise((r) => setTimeout(r, 250))
    return { id: `mem-${Date.now()}` }
  }
  return postJSON(`/devices/${req.device_id}/capture`, req)
}

// --- POST /network/mode ---------------------------------------------------

export async function setNetworkMode(mode: NetworkMode): Promise<void> {
  if (mock.USE_MOCKS) return
  await postJSON('/network/mode', { mode })
}

// --- GET /devices/{id}/sync ------------------------------------------------

export async function fetchSyncStatus(deviceId: string): Promise<SyncStatus> {
  if (mock.USE_MOCKS) return mock.mockSyncStatus(deviceId)
  return getJSON(`/devices/${deviceId}/sync`)
}

// --- GET /devices/{id}/activity ---------------------------------------------

export async function fetchActivity(deviceId: string): Promise<ActivityEntry[]> {
  if (mock.USE_MOCKS) return mock.mockActivity(deviceId)
  return getJSON(`/devices/${deviceId}/activity`)
}

// --- GET /devices/{id}/telemetry ---------------------------------------------

export async function fetchTelemetry(deviceId: string): Promise<TelemetrySample[]> {
  if (mock.USE_MOCKS) return mock.mockTelemetry(deviceId)
  return getJSON(`/devices/${deviceId}/telemetry`)
}

// --- Local memory browser (backed by activity/sync until a dedicated
//     endpoint exists — see frontend.md §4 fallback instruction) ------------

export async function fetchMemoryRecords(deviceId: string): Promise<MemoryRecord[]> {
  if (mock.USE_MOCKS) return mock.mockMemoryRecords(deviceId)
  return getJSON(`/devices/${deviceId}/memory`)
}

// --- GET /cloud/state ---------------------------------------------------------

export async function fetchCloudState(): Promise<CloudFact[]> {
  if (mock.USE_MOCKS) return mock.mockCloudState()
  return getJSON('/cloud/state')
}

// --- WS /devices/{id}/events ----------------------------------------------
// Returns an unsubscribe function. In mock mode, emits a synthetic feed entry
// every few seconds instead of opening a socket.

export function subscribeDeviceEvents(
  deviceId: string,
  onEvent: (entry: DecisionFeedEntry) => void,
): () => void {
  if (mock.USE_MOCKS) {
    let i = 0
    const seed = mock.mockDecisionFeed(deviceId, 50)
    const interval = setInterval(() => {
      onEvent({ ...seed[i % seed.length], id: `live-${deviceId}-${Date.now()}`, timestamp: new Date().toISOString() })
      i++
    }, 3500)
    return () => clearInterval(interval)
  }
  const ws = new WebSocket(`${WS_BASE_URL}/devices/${deviceId}/events`)
  ws.onmessage = (msg) => onEvent(JSON.parse(msg.data))
  return () => ws.close()
}

// --- WS /consensus/events ---------------------------------------------------

export function subscribeConsensusEvents(onEvent: (entry: ConsensusEvent) => void): () => void {
  if (mock.USE_MOCKS) {
    const seed = mock.mockConsensusEvents()
    const interval = setInterval(() => {
      const base = seed[0]
      onEvent({ ...base, id: `live-${Date.now()}`, timestamp: new Date().toISOString() })
    }, 8000)
    return () => clearInterval(interval)
  }
  const ws = new WebSocket(`${WS_BASE_URL}/consensus/events`)
  ws.onmessage = (msg) => onEvent(JSON.parse(msg.data))
  return () => ws.close()
}
