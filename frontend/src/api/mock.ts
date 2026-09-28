// Mock data — stands in for the FastAPI backend (backend.md §6) until it's wired up.
// frontend.md §4: "If any field named above doesn't exist yet in the backend,
// stub it with mock data rather than blocking."
//
// Swap `USE_MOCKS` to false once the real endpoints exist; every function
// in client.ts checks this flag so pages never need to know the difference.

import type {
  ActivityEntry,
  CloudFact,
  ConsensusEvent,
  DecisionFeedEntry,
  DeviceSummary,
  MemoryRecord,
  SyncStatus,
  TelemetrySample,
} from '../types'

export const USE_MOCKS = true

const DEVICE_NAMES = [
  { id: 'dev-01', name: 'Paramedic Tablet 01', kind: 'paramedic tablet', latitude: 28.6329, longitude: 77.2195 },
  { id: 'dev-02', name: 'Kiosk — Shelter B', kind: 'kiosk', latitude: 28.6129, longitude: 77.2295 },
  { id: 'dev-03', name: 'Field Pi Node 03', kind: 'pi node', latitude: 28.6562, longitude: 77.2410 },
  { id: 'dev-04', name: 'Paramedic Tablet 02', kind: 'paramedic tablet', latitude: 28.5933, longitude: 77.2190 },
]

const REASONS = [
  '98% similar to entry 4m ago — discarded as redundant',
  'urgency 0.91 — pushed immediately despite degraded link',
  'PII detected — redacted to anonymized embedding, flagged local_only',
  'missing required field "status: verified" — held in QUEUE_LOW',
  'novel report, no nearby match — queued high priority',
  'below completeness gate — awaiting reporter_device_id',
]

const VERDICTS: DecisionFeedEntry['verdict'][] = [
  'KEPT_LOCAL',
  'QUEUED',
  'SYNCED_NOW',
  'REJECTED',
  'REDACT_AND_QUEUE',
]

function rng(seed: number) {
  let s = seed
  return () => {
    s = (s * 1103515245 + 12345) & 0x7fffffff
    return s / 0x7fffffff
  }
}

export function mockDevices(): DeviceSummary[] {
  const r = rng(7)
  return DEVICE_NAMES.map((d, i) => ({
    id: d.id,
    name: d.name,
    kind: d.kind,
    connectivity: (['ONLINE', 'DEGRADED', 'OFFLINE', 'ONLINE'] as const)[i],
    memory_used: Math.floor(180 + r() * 320),
    memory_cap: 500,
    last_sync_at: new Date(Date.now() - r() * 1000 * 60 * 40).toISOString(),
    activity_sparkline: Array.from({ length: 20 }, () => Math.floor(r() * 10)),
    latitude: d.latitude,
    longitude: d.longitude,
  }))
}

export function mockDecisionFeed(deviceId: string, count = 12): DecisionFeedEntry[] {
  const r = rng(deviceId.length * 31)
  return Array.from({ length: count }, (_, i) => {
    const verdict = VERDICTS[Math.floor(r() * VERDICTS.length)]
    return {
      id: `feed-${deviceId}-${i}`,
      device_id: deviceId,
      timestamp: new Date(Date.now() - i * 1000 * 45).toISOString(),
      content_preview:
        i % 3 === 0
          ? 'Zone C — structural collapse reported near east stairwell'
          : i % 3 === 1
          ? 'Water level rising, block 4 access road'
          : 'Supply cache confirmed, north depot',
      modality: i % 4 === 0 ? 'vision' : 'text',
      verdict,
      reason: REASONS[Math.floor(r() * REASONS.length)],
      urgency_score: Number(r().toFixed(2)),
    }
  })
}

export function mockMemoryRecords(deviceId: string, count = 10): MemoryRecord[] {
  const r = rng(deviceId.length * 53)
  const states: MemoryRecord['state'][] = ['local_only', 'synced', 'pending']
  return Array.from({ length: count }, (_, i) => ({
    id: `mem-${deviceId}-${i}`,
    device_id: deviceId,
    content_preview: `Zone ${String.fromCharCode(65 + (i % 5))} — field report #${i + 1}`,
    modality: i % 5 === 0 ? 'vision' : 'text',
    state: states[Math.floor(r() * states.length)],
    zone: `Zone ${String.fromCharCode(65 + (i % 5))}`,
    status: r() > 0.5 ? 'verified' : 'unverified',
    decision_reason: REASONS[Math.floor(r() * REASONS.length)],
    sync_verdict: VERDICTS[Math.floor(r() * VERDICTS.length)],
    captured_at: new Date(Date.now() - i * 1000 * 60 * 6).toISOString(),
    activity_ids: [`act-${deviceId}-${i}`, `act-${deviceId}-${i + 1}`],
  }))
}

export function mockSyncStatus(deviceId: string): SyncStatus {
  const r = rng(deviceId.length * 91)
  return {
    device_id: deviceId,
    last_attempt_at: new Date(Date.now() - r() * 1000 * 60).toISOString(),
    last_success_at: new Date(Date.now() - r() * 1000 * 60 * 5).toISOString(),
    consecutive_failures: Math.floor(r() * 3),
    next_backoff_s: Math.floor(4 + r() * 20),
    pending_by_priority: {
      URGENT: Math.floor(r() * 4),
      ROUTINE: Math.floor(r() * 30),
      HELD: Math.floor(r() * 12),
    },
    last_push_bytes: Math.floor(1200 + r() * 18000),
    last_push_duration_ms: Math.floor(80 + r() * 900),
    last_push_mode: 'degraded',
    last_pull_at: new Date(Date.now() - r() * 1000 * 60 * 12).toISOString(),
    last_pull_points: Math.floor(r() * 40),
  }
}

export function mockActivity(deviceId: string, count = 20): ActivityEntry[] {
  const r = rng(deviceId.length * 113)
  const kinds: ActivityEntry['kind'][] = [
    'capture',
    'decision',
    'push_attempt',
    'push_result',
    'pull_result',
    'consensus',
    'retraction',
    'error',
    'mode_change',
  ]
  return Array.from({ length: count }, (_, i) => {
    const kind = kinds[Math.floor(r() * kinds.length)]
    const detailByKind: Record<ActivityEntry['kind'], string> = {
      capture: 'text fact captured, embedding queued',
      decision: 'verdict QUEUE_HIGH — urgency 0.91',
      push_attempt: 'attempting push of 6 points, mode=degraded',
      push_result: 'pushed 4 points, 2 failed, 18.2 KB, mode=degraded',
      pull_result: 'partial snapshot applied, 12 points received',
      consensus: 'zone_c.hazard_status resolved CONFIRMED, confidence 0.87',
      retraction: 'prior report for zone_b.supply_status retracted',
      error: 'push failed — connection reset (simulated degraded link)',
      mode_change: 'network mode set to degraded',
    }
    return {
      id: `act-${deviceId}-${i}`,
      timestamp: new Date(Date.now() - i * 1000 * 30).toISOString(),
      device_id: deviceId,
      kind,
      detail: detailByKind[kind],
      confidence: kind === 'consensus' ? Number(r().toFixed(2)) : undefined,
    }
  })
}

export function mockTelemetry(deviceId: string, count = 30): TelemetrySample[] {
  const r = rng(deviceId.length * 17)
  return Array.from({ length: count }, (_, i) => ({
    timestamp: new Date(Date.now() - (count - i) * 1000 * 5).toISOString(),
    cpu_pct: Math.floor(15 + r() * 40),
    ram_mb: Math.floor(180 + r() * 90),
    query_latency_ms: Math.floor(4 + r() * 30),
    model_load_ms: 820,
  }))
}

export function mockConsensusEvents(): ConsensusEvent[] {
  return [
    {
      id: 'cons-1',
      corroboration_key: 'zone_c.hazard_status',
      timestamp: new Date().toISOString(),
      outcome: 'DISPUTED',
      confidence: 0.54,
      claims: [
        { device_id: 'dev-01', value: 'gas leak, active', trust_score: 0.9, reported_at: new Date().toISOString() },
        { device_id: 'dev-03', value: 'gas leak, contained', trust_score: 0.42, reported_at: new Date().toISOString() },
      ],
      resolution_summary:
        'Zone-C hazard status disputed — 2 conflicting reports, confidence 0.54, held for human review',
    },
  ]
}

export function mockCloudState(): CloudFact[] {
  const r = rng(3)
  const zones = ['Zone A', 'Zone B', 'Zone C', 'Zone D']
  return Array.from({ length: 9 }, (_, i) => ({
    id: `fact-${i}`,
    corroboration_key: `zone_${String.fromCharCode(97 + (i % 4))}.status_${i}`,
    zone: zones[i % zones.length],
    summary:
      i % 5 === 0
        ? 'Structural collapse confirmed, east stairwell — evacuation in progress'
        : 'Road access confirmed clear',
    status: r() > 0.75 ? 'DISPUTED' : 'CONFIRMED',
    confidence: Number((0.5 + r() * 0.5).toFixed(2)),
    corroborating_devices: ['dev-01', 'dev-02', 'dev-03'].slice(0, 1 + Math.floor(r() * 3)),
    last_updated: new Date(Date.now() - r() * 1000 * 60 * 30).toISOString(),
  }))
}
