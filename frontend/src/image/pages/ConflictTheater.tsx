import { useEffect, useState } from 'react'
import { useNetworkMode } from '../context/NetworkModeContext'
import { mockConsensusEvents, USE_MOCKS } from '../api/mock'
import { fetchDevices, fetchMemoryRecords, injectConflict, mapConsensusEvent, subscribeConsensusEvents } from '../api/client'
import type { ConsensusEvent, DeviceSummary, MemoryRecord } from '../types'
import { MonoValue, Panel } from '../components/primitives'

const DEFAULT_CAM_A = {
  id: 'cam-01',
  name: 'Zone A Vision 01',
  value: 'fire',
  thumbnail_url: '/thumbnails/thumb_1.jpg',
  corroboration_key: 'server_room_A.fire_status',
}

const DEFAULT_CAM_B = {
  id: 'cam-02',
  name: 'Zone B Vision 02',
  value: 'none',
  thumbnail_url: '/thumbnails/thumb_9.jpg',
  corroboration_key: 'server_room_A.fire_status',
}

function CameraSide({
  name,
  id,
  value,
  thumbnailUrl,
  reconnected,
}: {
  name: string
  id: string
  value: string
  thumbnailUrl?: string | null
  reconnected: boolean
}) {
  return (
    <div className="glass-card flex-1 p-3 rounded-lg border border-line bg-white/40 backdrop-blur-md">
      <div className="flex items-center justify-between mb-2.5">
        <div>
          <div className="text-sm font-semibold text-ink">{name}</div>
          <div className="text-[11px] text-ink-faint font-mono">{id}</div>
        </div>
        <span
          className={`font-mono text-[10px] px-2 py-0.5 border rounded-sm font-medium ${
            reconnected ? 'border-good text-good bg-good/10' : 'border-alert text-alert bg-alert/10'
          }`}
        >
          {reconnected ? 'ONLINE' : 'OFFLINE'}
        </span>
      </div>

      {/* Camera Thumbnail Side by Side (Task A7) */}
      <div className="relative aspect-video w-full rounded overflow-hidden bg-black/10 border border-line mb-2">
        {thumbnailUrl ? (
          <img src={thumbnailUrl} alt={value} className="w-full h-full object-cover" />
        ) : (
          <div className="w-full h-full flex flex-col items-center justify-center text-xs font-mono text-ink-faint">
            <span>[CAMERA FRAME]</span>
            <span className="text-[10px]">{id}</span>
          </div>
        )}
        <div className="absolute top-1.5 left-1.5 px-1.5 py-0.5 rounded bg-black/70 text-white font-mono text-[10px]">
          {id}
        </div>
      </div>

      <div className="rounded-md border border-line bg-base-sunken/60 p-2 text-xs flex items-center justify-between">
        <span className="text-ink-faint font-mono">REPORTED OBSERVATION:</span>
        <span className={`font-mono font-bold px-1.5 py-0.5 rounded ${value === 'fire' ? 'bg-alert/20 text-alert' : 'bg-good/20 text-good'}`}>
          {value.toUpperCase()}
        </span>
      </div>
    </div>
  )
}

export function ConflictTheater() {
  const { setMode } = useNetworkMode()
  const [reconnected, setReconnected] = useState(false)
  const [resolved, setResolved] = useState<ConsensusEvent | null>(null)
  const [resolving, setResolving] = useState(false)
  const [devices, setDevices] = useState<DeviceSummary[]>([])
  const [camARecords, setCamARecords] = useState<MemoryRecord[]>([])
  const [camBRecords, setCamBRecords] = useState<MemoryRecord[]>([])

  useEffect(() => {
    void fetchDevices()
      .then((devs) => {
        setDevices(devs)
        const c1 = devs.find((d) => d.id === 'cam-01') || devs[0]
        const c2 = devs.find((d) => d.id === 'cam-02') || devs[1]
        if (c1) void fetchMemoryRecords(c1.id, { modality: 'vision' }).then(setCamARecords)
        if (c2) void fetchMemoryRecords(c2.id, { modality: 'vision' }).then(setCamBRecords)
      })
      .catch((error) => console.error('Unable to load image fleet', error))

    return subscribeConsensusEvents((event) => {
      setResolved(event)
      setResolving(false)
    })
  }, [])

  const devA = devices.find((d) => d.id === 'cam-01') ?? devices[0] ?? DEFAULT_CAM_A
  const devB = devices.find((d) => d.id === 'cam-02') ?? devices[1] ?? DEFAULT_CAM_B

  const thumbA = camARecords[0]?.thumbnail_url ?? DEFAULT_CAM_A.thumbnail_url
  const thumbB = camBRecords[0]?.thumbnail_url ?? DEFAULT_CAM_B.thumbnail_url

  async function reconnect() {
    setResolving(true)
    try {
      // Reconnect and push real conflict for server_room_A.fire_status
      const response = await injectConflict('server_room_A.fire_status', {
        [devA.id]: 'fire',
        [devB.id]: 'none',
      })
      setMode('full')
      setReconnected(true)
      if (response.consensus) {
        setResolved(mapConsensusEvent(response.consensus))
        setResolving(false)
      }
      if (USE_MOCKS) {
        window.setTimeout(() => {
          setResolved(mockConsensusEvents()[0])
          setResolving(false)
        }, 1200)
      }
    } catch (error) {
      console.error('Unable to inject conflict', error)
      setResolving(false)
    }
  }

  function reset() {
    setReconnected(false)
    setResolved(null)
    setMode('offline')
  }

  // Cross-modal vote line (Task C2)
  const crossModalLine = (() => {
    if (!resolved) return null
    if (resolved.modal_votes) {
      const textDevs = resolved.modal_votes.text?.devices?.length || 0
      const visionDevs = resolved.modal_votes.vision?.devices?.length || 0
      if (textDevs > 0 && visionDevs > 0) {
        return `${visionDevs} camera + ${textDevs} text report agree, confidence ${resolved.confidence.toFixed(2)}`
      }
    }
    // Fallback if modal_votes not yet stamped
    const hasCam = resolved.claims.some((c) => c.device_id.startsWith('cam-'))
    const hasText = resolved.claims.some((c) => !c.device_id.startsWith('cam-'))
    if (hasCam && hasText) {
      return `1 camera + 1 text report agree, confidence ${resolved.confidence.toFixed(2)}`
    }
    return null
  })()

  return (
    <div className="p-4 flex flex-col gap-4 h-full min-h-0 overflow-y-auto">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-sm font-mono font-bold text-ink tracking-wide">
            CONFLICT THEATER — VISION DISPUTE RESOLUTION
          </h1>
          <p className="text-[11px] text-ink-dim font-mono mt-0.5">
            Key: <span className="text-ink">server_room_A.fire_status</span>
          </p>
        </div>
        <div className="flex gap-2">
          <button
            onClick={reset}
            className="px-3 py-1.5 text-xs font-mono border border-line text-ink-dim hover:text-ink rounded"
          >
            RESET (GO OFFLINE)
          </button>
          <button
            onClick={reconnect}
            disabled={resolving}
            className="px-4 py-1.5 text-xs font-mono border border-good text-good hover:bg-good hover:text-base disabled:opacity-50 rounded font-semibold"
          >
            {resolving ? 'RECONNECTING…' : 'RECONNECT & RESOLVE'}
          </button>
        </div>
      </div>

      {/* 2 Camera Thumbnails Side-by-Side (Task A7) */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <CameraSide
          name={devA.name || 'Zone A Vision 01'}
          id={devA.id}
          value="fire"
          thumbnailUrl={thumbA}
          reconnected={reconnected}
        />
        <CameraSide
          name={devB.name || 'Zone B Vision 02'}
          id={devB.id}
          value="none"
          thumbnailUrl={thumbB}
          reconnected={reconnected}
        />
      </div>

      {/* Consensus Fold Result Panel */}
      <Panel title="CONSENSUS FOLD RESULT" className="flex-1">
        <div className="p-4">
          {!resolved && !resolving && (
            <div className="text-ink-faint text-sm font-mono text-center py-10">
              Both cameras offline, disagreeing on <span className="text-ink">server_room_A.fire_status</span> — press RECONNECT & RESOLVE
            </div>
          )}

          {resolving && (
            <div className="text-pending text-sm font-mono text-center py-10 animate-pulse">
              Folding visual claims over corroboration_key = server_room_A.fire_status…
            </div>
          )}

          {resolved && (
            <div className="space-y-4">
              <div className="flex flex-wrap items-center justify-between gap-3 p-3 bg-base-sunken/60 rounded-md border border-line">
                <div className="flex items-center gap-3">
                  <span
                    className={`font-mono text-xs px-2.5 py-1 border rounded font-bold ${
                      resolved.outcome === 'CONFIRMED'
                        ? 'border-good text-good bg-good/15'
                        : resolved.outcome === 'DISPUTED'
                        ? 'border-alert text-alert bg-alert/15'
                        : 'border-pending text-pending bg-pending/15'
                    }`}
                  >
                    {resolved.outcome}
                  </span>
                  <MonoValue className="text-sm font-bold text-ink">
                    Confidence: {(resolved.confidence * 100).toFixed(1)}%
                  </MonoValue>
                </div>

                {/* Cross-modal vote differentiator badge (Task C2) */}
                {crossModalLine && (
                  <div className="text-xs font-mono font-medium px-2 py-1 rounded bg-pending/15 text-pending border border-pending/40">
                    ⚡ {crossModalLine}
                  </div>
                )}
              </div>

              {/* Claims Breakdown */}
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                {resolved.claims.map((c) => (
                  <div key={c.device_id} className="glass-card p-3 rounded border border-line bg-white/30 text-xs">
                    <div className="flex items-center justify-between">
                      <span className="font-mono text-ink-faint font-semibold">{c.device_id}</span>
                      <span className="font-mono text-ink-dim text-[10px]">
                        weight: <MonoValue>{c.trust_score.toFixed(2)}</MonoValue>
                      </span>
                    </div>
                    <div className="text-sm font-bold text-ink my-1">
                      Observation: {c.value}
                    </div>
                  </div>
                ))}
              </div>

              {/* Explanation line from the consensus event */}
              <div className="border-t border-line pt-3 text-xs text-ink-dim bg-base-sunken/40 p-3 rounded">
                <span className="font-mono text-ink-faint text-[10px] uppercase tracking-wider block mb-0.5">
                  Resolution Explanation
                </span>
                {resolved.resolution_summary || 'Consensus fold evaluated trust-weighted claims across reporting devices.'}
              </div>
            </div>
          )}
        </div>
      </Panel>
    </div>
  )
}
