import { useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  assetUrl,
  fetchCloudDashboard,
  fetchDecisionFeed,
  fetchDevices,
  fetchMemoryRecords,
  fetchNetworkMode,
  fetchSyncStatus,
  fetchTelemetry,
  queryDevice,
  subscribeConsensusEvents,
  subscribeDeviceEvents,
} from '../api/client'
import type { CloudFact, ConsensusEvent, DecisionFeedEntry, DeviceSummary, MemoryRecord, QueryResult, SyncStatus, TelemetrySample } from '../types'
import { ConnectivityPill, MonoValue, Panel, VerdictBadge } from '../components/primitives'

type CameraSnapshot = {
  device: DeviceSummary
  records: MemoryRecord[]
  feed: DecisionFeedEntry[]
  sync: SyncStatus | null
  telemetry: TelemetrySample | null
}

export function OverviewDashboard() {
  const navigate = useNavigate()
  const [devices, setDevices] = useState<DeviceSummary[] | null>(null)
  const [cameras, setCameras] = useState<CameraSnapshot[]>([])
  const [facts, setFacts] = useState<CloudFact[]>([])
  const [trust, setTrust] = useState<Record<string, number>>({})
  const [networkMode, setNetworkMode] = useState<string | null>(null)
  const [consensusEvents, setConsensusEvents] = useState<ConsensusEvent[]>([])
  const [query, setQuery] = useState('')
  const [queryState, setQueryState] = useState<(QueryResult & { question: string }) | null>(null)
  const [queryPending, setQueryPending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function refresh() {
    try {
      const fleet = await fetchDevices()
      setDevices(fleet)
      const cameraDevices = fleet.filter((device) => device.id.startsWith('cam-'))
      const [cloud, mode, rows] = await Promise.all([
        fetchCloudDashboard().catch(() => ({ facts: [], device_trust: {} })),
        fetchNetworkMode().catch(() => null),
        Promise.all(cameraDevices.map(async (device): Promise<CameraSnapshot> => {
          const [records, feed, sync, telemetry] = await Promise.all([
            fetchMemoryRecords(device.id, { modality: 'vision', limit: 1000 }).catch(() => []),
            fetchDecisionFeed(device.id, 100).catch(() => []),
            fetchSyncStatus(device.id).catch(() => null),
            fetchTelemetry(device.id).catch(() => []),
          ])
          return { device, records, feed, sync, telemetry: telemetry.at(-1) ?? null }
        })),
      ])
      setFacts(cloud.facts)
      setTrust(cloud.device_trust)
      setNetworkMode(mode)
      setCameras(rows)
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to reach the edge node')
      setDevices([])
      setCameras([])
      setFacts([])
      setTrust({})
    }
  }

  useEffect(() => {
    void refresh()
    const timer = window.setInterval(() => void refresh(), 15000)
    return () => window.clearInterval(timer)
  }, [])

  const cameraIds = cameras.map(({ device }) => device.id).join(',')
  useEffect(() => {
    if (!cameraIds) return
    const unsubscribers = cameraIds.split(',').map((deviceId) => subscribeDeviceEvents(deviceId, () => void refresh()))
    const unsubscribeConsensus = subscribeConsensusEvents((event) => {
      setConsensusEvents((current) => [event, ...current.filter((item) => item.id !== event.id)].slice(0, 20))
      void refresh()
    })
    return () => {
      unsubscribers.forEach((unsubscribe) => unsubscribe())
      unsubscribeConsensus()
    }
  }, [cameraIds])

  const allRecords = useMemo(() => cameras.flatMap(({ records }) => records).sort(byNewest), [cameras])
  const allFeed = useMemo(() => cameras.flatMap(({ feed }) => feed).sort(byNewest), [cameras])
  const allSync = useMemo(() => cameras.map(({ sync }) => sync).filter((item): item is SyncStatus => item !== null), [cameras])
  const latestByKey = useMemo(() => latestRecordsByKey(allRecords), [allRecords])
  const disputedFacts = facts.filter((fact) => fact.status === 'DISPUTED')
  const confirmedFacts = facts.filter((fact) => fact.status === 'CONFIRMED')
  const photos = allRecords.length
  const memoryCap = cameras.reduce((sum, row) => sum + row.device.memory_cap, 0)
  const online = cameras.filter(({ device }) => device.connectivity === 'ONLINE').length
  const degraded = cameras.filter(({ device }) => device.connectivity === 'DEGRADED').length
  const offline = cameras.filter(({ device }) => device.connectivity === 'OFFLINE').length
  const bytesSent = allSync.reduce((sum, status) => sum + (status.last_push_bytes ?? 0), 0)
  const pipeline = allSync.reduce((result, status) => ({
    URGENT: result.URGENT + (status.pending_by_priority.URGENT ?? 0),
    ROUTINE: result.ROUTINE + (status.pending_by_priority.ROUTINE ?? 0),
    HELD: result.HELD + (status.pending_by_priority.HELD ?? 0),
  }), { URGENT: 0, ROUTINE: 0, HELD: 0 })
  const latestTelemetry = cameras.map(({ telemetry }) => telemetry).filter((item): item is TelemetrySample => item !== null)
  const activeZoneKeys = new Set([...facts.map((fact) => fact.corroboration_key), ...allRecords.flatMap((record) => record.corroboration_key ? [record.corroboration_key] : [])])
  const zones = [...activeZoneKeys].sort()
  const camerasMissing = devices !== null && cameras.length === 0
  const noFrames = !camerasMissing && cameras.length > 0 && photos === 0

  async function submitQuery() {
    const camera = cameras[0]
    if (!camera || !query.trim() || queryPending) return
    setQueryPending(true)
    try {
      setQueryState({ ...(await queryDevice(camera.device.id, query.trim())), question: query.trim() })
      setQuery('')
    } finally {
      setQueryPending(false)
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col gap-3 overflow-y-auto p-4 text-ink lg:p-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <div className="font-mono text-[10px] tracking-[0.35em] text-ink-dim">IMAGE FLEET / OVERVIEW</div>
          <h1 className="mt-1 font-serif text-4xl leading-none tracking-tight">Visual Situation Board</h1>
          <p className="mt-2 text-sm text-ink-dim">What the cameras see, whether they agree, and what reached the cloud.</p>
        </div>
        <div className="font-mono text-[10px] text-ink-dim">{networkMode ? `NETWORK ${networkMode.toUpperCase()}` : 'NETWORK —'}</div>
      </header>

      {error && <div className="border border-alert bg-alert/10 px-3 py-2 font-mono text-[11px] text-alert">Edge node unavailable: {error}</div>}
      {camerasMissing && <EmptyState>NO CAMERAS PROVISIONED. Run <span className="text-ink">python tools/seed_fleet.py</span>.</EmptyState>}
      {noFrames && <EmptyState>NO FRAMES CAPTURED YET. Run <span className="text-ink">python tools/load_image_dataset.py</span>.</EmptyState>}

      <section className="glass-card grid grid-cols-2 divide-x divide-y divide-[#AABBC8] xl:grid-cols-5 xl:divide-y-0">
        <Kpi value={devices === null ? '—' : `${online}/${cameras.length || '—'}`} label="CAMERAS ONLINE" detail={devices === null ? '—' : `${offline} offline · ${degraded} degraded`} />
        <Kpi value={devices === null ? '—' : `${photos}/${memoryCap || '—'}`} label="PHOTOS IN MEMORY" detail="vision points / local cap" />
        <Kpi value={facts.length ? String(confirmedFacts.length) : '—'} label="ZONES WITH ACTIVE HAZARD" detail="confirmed visual facts" />
        <Kpi value={facts.length ? String(disputedFacts.length) : '—'} label="OPEN VISUAL DISPUTES" detail="disputed visual facts" alert />
        <Kpi value={bytesSent ? formatBytes(bytesSent) : '—'} label="BYTES SENT TO CLOUD" detail={networkMode ? networkMode.toUpperCase() : '—'} />
      </section>

      <section className="grid min-h-[360px] grid-cols-1 gap-3 xl:grid-cols-5">
        <Panel number="01" title="ZONE SITUATION BOARD" className="min-h-0 xl:col-span-3">
          <div className="grid h-full min-h-0 grid-cols-1 gap-3 overflow-y-auto p-3 sm:grid-cols-2">
            {zones.map((key) => <ZoneTile key={key} keyName={key} fact={facts.find((item) => item.corroboration_key === key)} records={latestByKey.get(key) ?? []} onOpen={() => navigate('/image/overview/command')} />)}
            {zones.length === 0 && <LoadingRow text={camerasMissing || noFrames ? '—' : 'Waiting for vision facts…'} />}
          </div>
        </Panel>
        <Panel number="02" title="CONSENSUS WATCH" className="min-h-0 xl:col-span-2">
          <div className="h-full space-y-3 overflow-y-auto p-3">
            {disputedFacts.slice(0, 4).map((fact) => {
              const records = latestByKey.get(fact.corroboration_key) ?? []
              const event = consensusEvents.find((item) => item.corroboration_key === fact.corroboration_key)
              return <DisputeCard key={fact.id} fact={fact} records={records} explanation={event?.resolution_summary ?? '—'} />
            })}
            {disputedFacts.length === 0 && <LoadingRow text={facts.length ? 'No open disputes — all cameras agree.' : 'Waiting for consensus facts…'} />}
          </div>
        </Panel>
      </section>

      <Panel number="03" title="LIVE EVIDENCE STREAM" className="min-h-[190px]">
        <div className="flex gap-3 overflow-x-auto p-3">
          {allFeed.slice(0, 12).map((entry) => <EvidenceCard key={`${entry.device_id}-${entry.id}`} entry={entry} />)}
          {allFeed.length === 0 && <LoadingRow text="Waiting for camera decisions…" />}
        </div>
      </Panel>

      <section className="grid grid-cols-1 gap-3 xl:grid-cols-3">
        <Panel number="04" title="CAMERA HEALTH" className="min-h-[250px]">
          <div className="grid gap-2 p-3 sm:grid-cols-2">
            {cameras.map(({ device, records, sync }) => <CameraHealth key={device.id} device={device} latest={records[0]} sync={sync} trust={trust[device.id]} onOpen={() => navigate(`/image/overview/devices/${device.id}`)} />)}
            {cameras.length === 0 && <LoadingRow text="—" />}
          </div>
        </Panel>
        <Panel number="05" title="EDGE → CLOUD PIPELINE" className="min-h-[250px]">
          <Pipeline pipeline={pipeline} sync={allSync} mode={networkMode} />
        </Panel>
        <Panel number="06" title="ASK THE FLEET" className="min-h-[250px]">
          <div className="flex h-full flex-col gap-3 p-3">
            <form className="flex gap-2" onSubmit={(event) => { event.preventDefault(); void submitQuery() }}>
              <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="fire near the server racks" disabled={!cameras.length} className="min-w-0 flex-1 border border-line bg-base-sunken px-2 py-2 text-xs outline-none focus:border-good" />
              <button type="submit" disabled={!cameras.length || queryPending} className="border border-line px-3 font-mono text-[10px] hover:border-good disabled:opacity-50">{queryPending ? '…' : 'ASK'}</button>
            </form>
            {queryState ? <QueryResultView result={queryState} /> : <LoadingRow text={cameras.length ? 'Ask for visual evidence from the local fleet.' : '—'} />}
          </div>
        </Panel>
      </section>

      <TelemetryFooter samples={latestTelemetry} />
    </div>
  )
}

function ZoneTile({ keyName, fact, records, onOpen }: { keyName: string; fact?: CloudFact; records: MemoryRecord[]; onOpen: () => void }) {
  const latest = records[0]
  const state = fact?.status ?? (latest ? 'DISPUTED' : 'NO DATA')
  return (
    <button onClick={onOpen} className="flex min-w-0 flex-col border border-line bg-white/30 p-2 text-left hover:border-[#315C86]">
      <div className="flex items-center justify-between gap-2"><span className="truncate font-mono text-xs font-semibold">{fact?.zone ?? zoneLabel(keyName)}</span><StatePill state={state} /></div>
      <Thumb src={latest?.thumbnail_url ?? fact?.thumbnail_url} alt={latest?.content_preview ?? fact?.summary ?? keyName} className="mt-2 aspect-video" />
      <div className="mt-2 flex items-center justify-between gap-2 font-mono text-[10px] text-ink-dim"><span>{fact ? `${Math.round(fact.confidence * 100)}% confidence` : '—'}</span><span>{fact ? `${fact.corroborating_devices.length} cameras` : '—'}</span></div>
      <div className="mt-1 flex justify-between gap-2 text-[10px] text-ink-faint"><span>{records.length > 1 ? `+${records.length - 1} similar` : '—'}</span><span>{latest ? timeSince(latest.captured_at) : '—'}</span></div>
    </button>
  )
}

function DisputeCard({ fact, records, explanation }: { fact: CloudFact; records: MemoryRecord[]; explanation: string }) {
  const claims = records.slice(0, 2)
  return <div className="border border-alert/50 bg-alert/5 p-2"><div className="flex items-center justify-between gap-2"><span className="truncate font-mono text-[10px] font-semibold">{fact.zone ?? fact.corroboration_key}</span><MonoValue className="text-[10px] text-alert">{Math.round(fact.confidence * 100)}%</MonoValue></div><div className="mt-2 grid grid-cols-2 gap-2">{claims.map((record) => <div key={record.id} className="min-w-0"><Thumb src={record.thumbnail_url} alt={record.content_preview} className="aspect-video" /><div className="mt-1 truncate font-mono text-[9px] text-ink-dim">{record.content_preview}</div></div>)}{claims.length === 0 && <div className="col-span-2 font-mono text-[10px] text-ink-faint">No frame thumbnails available</div>}</div><div className="mt-2 truncate font-mono text-[10px] text-ink-dim">{explanation}</div></div>
}

function EvidenceCard({ entry }: { entry: DecisionFeedEntry }) {
  return <div className="w-[170px] shrink-0 border border-line bg-white/30 p-2"><Thumb src={entry.thumbnail_url} alt={entry.content_preview} className="aspect-video" /><div className="mt-2 flex items-center justify-between gap-1"><span className="truncate font-mono text-[9px] text-ink-dim">{entry.device_id}</span><VerdictBadge verdict={entry.verdict} /></div><div className="mt-1 line-clamp-2 text-[10px] text-ink-dim">{entry.reason || '—'}</div></div>
}

function CameraHealth({ device, latest, sync, trust, onOpen }: { device: DeviceSummary; latest?: MemoryRecord; sync: SyncStatus | null; trust?: number; onOpen: () => void }) {
  return <button onClick={onOpen} className="border border-line bg-white/30 p-2 text-left hover:border-[#315C86]"><div className="flex items-center justify-between gap-2"><span className="font-mono text-xs font-semibold">{device.id}</span><ConnectivityPill state={device.connectivity} /></div><div className="mt-2 flex gap-2"><Thumb src={latest?.thumbnail_url} alt={latest?.content_preview ?? device.id} className="h-12 w-20 shrink-0" /><div className="min-w-0 space-y-1 text-[10px] text-ink-dim"><div>memory <MonoValue>{device.memory_used}/{device.memory_cap}</MonoValue></div><div>pending <MonoValue>{sync ? Object.values(sync.pending_by_priority).reduce((a, b) => a + b, 0) : '—'}</MonoValue></div><div>trust <MonoValue>{trust == null ? '—' : trust.toFixed(2)}</MonoValue></div></div></div><div className="mt-2 border-t border-line pt-1 font-mono text-[9px] text-ink-faint">last sync {formatTime(device.last_sync_at)}</div></button>
}

function Pipeline({ pipeline, sync, mode }: { pipeline: { URGENT: number; ROUTINE: number; HELD: number }; sync: SyncStatus[]; mode: string | null }) {
  const totalPending = pipeline.URGENT + pipeline.ROUTINE + pipeline.HELD
  const last = sync.find((item) => item.last_push_bytes != null) ?? sync[0]
  return <div className="flex h-full flex-col gap-4 p-3"><div className="flex h-8 overflow-hidden border border-line bg-base-sunken">{(['URGENT', 'ROUTINE', 'HELD'] as const).map((key) => <div key={key} className={key === 'URGENT' ? 'bg-alert/70' : key === 'ROUTINE' ? 'bg-pending/70' : 'bg-ink-faint/40'} style={{ width: `${totalPending ? pipeline[key] / totalPending * 100 : 0}%` }} title={`${key}: ${pipeline[key]}`} />)}</div><div className="grid grid-cols-3 gap-2 font-mono text-[10px] text-ink-dim"><span>URGENT {pipeline.URGENT}</span><span>ROUTINE {pipeline.ROUTINE}</span><span>HELD {pipeline.HELD}</span></div><div className="grid grid-cols-2 gap-2 border-t border-line pt-3 text-[10px] text-ink-dim"><span>last push bytes <MonoValue>{last?.last_push_bytes == null ? '—' : formatBytes(last.last_push_bytes)}</MonoValue></span><span>mode <MonoValue>{last?.last_push_mode ?? mode ?? '—'}</MonoValue></span><span>accepted <MonoValue>{last?.last_push_accepted ?? '—'}</MonoValue></span><span>failed <MonoValue className="text-alert">{last?.last_push_failed ?? '—'}</MonoValue></span></div></div>
}

function QueryResultView({ result }: { result: QueryResult & { question: string } }) {
  const photos = result.results.filter((item) => item.modality === 'vision' || item.thumbnail_url)
  return <div className="min-h-0 space-y-2 overflow-y-auto"><div className="flex items-center justify-between gap-2 text-[10px] text-ink-dim"><span className="truncate">“{result.question}”</span><MonoValue>{result.latency_ms.toFixed(0)} ms · {(result.answer_path || '—').toUpperCase()}</MonoValue></div><div className="text-xs text-ink">{result.answer || '—'}</div><div className="flex gap-2 overflow-x-auto">{photos.map((photo) => <div key={photo.id} className="w-24 shrink-0"><Thumb src={photo.thumbnail_url} alt={photo.value} className="aspect-square" /><div className="mt-1 truncate font-mono text-[9px] text-ink-faint">{photo.score == null ? '—' : `${Math.round(photo.score * 100)}%`}</div></div>)}{photos.length === 0 && <span className="font-mono text-[10px] text-ink-faint">No visual matches</span>}</div></div>
}

function TelemetryFooter({ samples }: { samples: TelemetrySample[] }) {
  const sample = samples[0]
  return <div className="flex flex-wrap items-center justify-between gap-x-5 gap-y-1 border-t border-line px-1 pt-2 font-mono text-[9px] text-ink-faint"><span>EMULATED CONSTRAINED TARGET</span><span>CPU {sample ? `${sample.cpu_pct.toFixed(0)}%` : '—'}</span><span>RAM {sample ? `${sample.ram_mb.toFixed(0)} MB` : '—'}</span><span>MODEL LOAD {sample?.model_load_ms == null ? '—' : `${sample.model_load_ms.toFixed(0)} ms`}</span><span>QUERY P50 {sample ? `${sample.query_latency_ms.toFixed(0)} ms` : '—'}</span></div>
}

function Kpi({ value, label, detail, alert = false }: { value: string; label: string; detail: string; alert?: boolean }) {
  return <div className="min-w-0 p-3 xl:p-4"><div className={`font-mono text-2xl font-semibold leading-none ${alert ? 'text-alert' : 'text-ink'}`}>{value}</div><div className="mt-1 font-mono text-[9px] tracking-[0.16em] text-ink">{label}</div><div className="mt-1 truncate text-[10px] text-ink-dim">{detail}</div></div>
}

function StatePill({ state }: { state: string }) {
  const style = state === 'CONFIRMED' ? 'border-good text-good bg-good/10' : state === 'DISPUTED' ? 'border-alert text-alert bg-alert/10' : 'border-line text-ink-faint'
  return <span className={`shrink-0 border px-1.5 py-0.5 font-mono text-[9px] ${style}`}>{state}</span>
}

function Thumb({ src, alt, className = '' }: { src?: string | null; alt: string; className?: string }) {
  return <div className={`overflow-hidden border border-line bg-base-sunken ${className}`}>{src ? <img src={assetUrl(src) ?? undefined} alt={alt} className="h-full w-full object-cover" loading="lazy" /> : <div className="flex h-full items-center justify-center font-mono text-[9px] text-ink-faint">NO THUMBNAIL</div>}</div>
}

function EmptyState({ children }: { children: ReactNode }) {
  return <div className="border border-pending/50 bg-pending/5 px-3 py-2 font-mono text-[11px] text-ink-dim">{children}</div>
}

function LoadingRow({ text }: { text: string }) {
  return <div className="flex min-h-24 flex-1 animate-pulse items-center justify-center border border-line bg-white/20 px-4 text-center font-mono text-[10px] text-ink-faint">{text}</div>
}

function latestRecordsByKey(records: MemoryRecord[]) {
  const groups = new Map<string, MemoryRecord[]>()
  records.forEach((record) => {
    if (!record.corroboration_key) return
    groups.set(record.corroboration_key, [...(groups.get(record.corroboration_key) ?? []), record].sort(byNewest))
  })
  return groups
}

function byNewest(a: { captured_at?: string; timestamp?: string }, b: { captured_at?: string; timestamp?: string }) {
  return Date.parse(b.captured_at ?? b.timestamp ?? '') - Date.parse(a.captured_at ?? a.timestamp ?? '')
}

function zoneLabel(key: string) {
  return key.split('.')[0]?.replace(/^zone[_-]/i, 'Zone ').replace(/[_-]/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase()) || key
}

function formatTime(value: string | null | undefined) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function timeSince(value: string) {
  const time = new Date(value).getTime()
  if (!Number.isFinite(time)) return '—'
  const minutes = Math.max(0, Math.round((Date.now() - time) / 60000))
  return minutes < 1 ? 'now' : `${minutes}m ago`
}

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}