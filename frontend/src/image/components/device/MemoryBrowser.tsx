import { useEffect, useMemo, useState } from 'react'
import { fetchMemoryDetail, fetchMemoryRecords } from '../../api/client'
import type { MemoryRecord, MemoryState } from '../../types'
import { MonoValue, Panel } from '../primitives'

const STATE_STYLES: Record<MemoryState, string> = {
  local_only: 'text-ink-dim border-line bg-white/40',
  synced: 'text-good border-good bg-good/10',
  pending: 'text-pending border-pending bg-pending/10',
}

function StateBadge({ state }: { state: MemoryState }) {
  return (
    <span className={`font-mono text-[9px] px-1.5 py-0.5 border rounded-sm font-medium ${STATE_STYLES[state]}`}>
      {state.replace('_', ' ').toUpperCase()}
    </span>
  )
}

function SeverityLadderMini({ ladder }: { ladder?: { prompt: string; score: number }[] }) {
  if (!ladder || ladder.length === 0) return null
  // Top rung
  const top = [...ladder].sort((a, b) => b.score - a.score)[0]
  if (!top) return null
  const pct = Math.min(100, Math.max(0, Math.round(top.score * 100)))
  return (
    <div className="w-full mt-1">
      <div className="flex items-center justify-between text-[9px] font-mono text-ink-dim">
        <span className="truncate pr-1">{top.prompt}</span>
        <span>{pct}%</span>
      </div>
      <div className="w-full h-1 bg-black/10 rounded-full overflow-hidden mt-0.5">
        <div
          className={`h-full ${pct > 65 ? 'bg-alert' : pct > 35 ? 'bg-pending' : 'bg-good'}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  )
}

function SeverityLadderFull({ ladder }: { ladder?: { prompt: string; score: number }[] }) {
  if (!ladder || ladder.length === 0) return null
  return (
    <div className="space-y-1.5 p-2 bg-base-sunken/60 rounded border border-line">
      <div className="text-[10px] font-mono font-medium text-ink-dim tracking-wider uppercase">
        CLIP Severity Ladder
      </div>
      <div className="space-y-1">
        {ladder.map((rung, i) => {
          const pct = Math.min(100, Math.max(0, Math.round(rung.score * 100)))
          return (
            <div key={i} className="text-[10px] font-mono">
              <div className="flex justify-between text-ink-dim">
                <span className="truncate pr-2">{rung.prompt}</span>
                <span className="shrink-0">{pct}%</span>
              </div>
              <div className="w-full h-1.5 bg-black/10 rounded-full overflow-hidden mt-0.5">
                <div
                  className={`h-full ${pct > 65 ? 'bg-alert' : pct > 35 ? 'bg-pending' : 'bg-good'}`}
                  style={{ width: `${pct}%` }}
                />
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

interface KeyGroup {
  key: string
  records: MemoryRecord[]
}

export function MemoryBrowser({ deviceId }: { deviceId: string }) {
  const [records, setRecords] = useState<MemoryRecord[]>([])
  const [filter, setFilter] = useState('')
  const [selectedPointId, setSelectedPointId] = useState<string | null>(null)
  const [expandedStackKey, setExpandedStackKey] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    void fetchMemoryRecords(deviceId, { q: filter, modality: 'vision' })
      .then((rows) => {
        if (active) setRecords(rows)
      })
      .catch((error) => console.error('Unable to load image memory', error))
    return () => {
      active = false
    }
  }, [deviceId, filter])

  const filtered = useMemo(
    () =>
      records.filter((r) =>
        r.content_preview.toLowerCase().includes(filter.toLowerCase()) ||
        (r.zone && r.zone.toLowerCase().includes(filter.toLowerCase())) ||
        (r.corroboration_key && r.corroboration_key.toLowerCase().includes(filter.toLowerCase()))
      ),
    [records, filter]
  )

  // Group by corroboration_key (Client-side dedup — Task A5)
  const groups = useMemo<KeyGroup[]>(() => {
    const map = new Map<string, MemoryRecord[]>()
    for (const r of filtered) {
      const k = r.corroboration_key || `point_${r.id}`
      if (!map.has(k)) {
        map.set(k, [])
      }
      map.get(k)!.push(r)
    }
    const res: KeyGroup[] = []
    map.forEach((recs, key) => {
      // Sort latest first
      recs.sort((a, b) => new Date(b.captured_at).getTime() - new Date(a.captured_at).getTime())
      res.push({ key, records: recs })
    })
    return res
  }, [filtered])

  // Active selected record
  const selectedRecord = useMemo(
    () => records.find((r) => r.id === selectedPointId) ?? null,
    [records, selectedPointId]
  )

  function openDetail(pointId: string) {
    setSelectedPointId(pointId)
    const rec = records.find((r) => r.id === pointId)
    if (rec && !rec.detail) {
      void fetchMemoryDetail(deviceId, pointId)
        .then((detail) => {
          setRecords((rows) =>
            rows.map((row) =>
              row.id === pointId
                ? {
                    ...row,
                    detail,
                    decision_reason: detail.decision?.reason || row.decision_reason,
                    sync_verdict: detail.decision?.verdict || row.sync_verdict,
                    vision: detail.point?.vision || row.vision,
                  }
                : row
            )
          )
        })
        .catch((err) => console.error('Unable to load point detail', err))
    }
  }

  return (
    <Panel title="LOCAL MEMORY BROWSER (PHOTOS)" className="h-full">
      <div className="flex flex-col h-full min-h-0">
        <div className="p-2 border-b border-line flex items-center justify-between gap-2">
          <input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Search photos, zone, or key…"
            className="w-full bg-base-sunken border border-line px-2 py-1 text-xs outline-none focus-visible:border-good rounded"
          />
          <span className="text-[10px] font-mono text-ink-faint shrink-0">
            {records.length} {records.length === 1 ? 'photo' : 'photos'}
          </span>
        </div>

        <div className="flex-1 overflow-y-auto p-2">
          {groups.length === 0 ? (
            <div className="text-ink-faint text-xs font-mono py-12 text-center">
              No vision points captured yet
            </div>
          ) : (
            <div className="grid grid-cols-2 sm:grid-cols-2 md:grid-cols-3 gap-2.5">
              {groups.map((group) => {
                const primary = group.records[0]
                const stackCount = group.records.length
                const isStackOpen = expandedStackKey === group.key
                const isSelected = selectedPointId === primary.id

                return (
                  <div
                    key={group.key}
                    className={`group relative flex flex-col rounded-md border p-1.5 transition-all text-left bg-base-sunken/40 hover:bg-base-sunken ${
                      isSelected
                        ? 'border-good ring-1 ring-good'
                        : 'border-line hover:border-ink-faint'
                    }`}
                  >
                    {/* Thumbnail Image Container */}
                    <div
                      onClick={() => openDetail(primary.id)}
                      className="relative w-full aspect-square rounded overflow-hidden bg-black/10 border border-line cursor-pointer"
                    >
                      {primary.thumbnail_url ? (
                        <img
                          src={primary.thumbnail_url}
                          alt={primary.content_preview}
                          className="w-full h-full object-cover transition-transform group-hover:scale-105"
                          loading="lazy"
                        />
                      ) : (
                        <div className="w-full h-full flex flex-col items-center justify-center text-[10px] font-mono text-ink-faint p-2 text-center">
                          <span>[PHOTO]</span>
                          <span className="text-[8px] truncate mt-1 max-w-full">
                            {primary.corroboration_key || primary.content_preview}
                          </span>
                        </div>
                      )}

                      {/* State Badge Overlay */}
                      <div className="absolute top-1.5 left-1.5">
                        <StateBadge state={primary.state} />
                      </div>

                      {/* +N Similar Badge (Task A5) */}
                      {stackCount > 1 && (
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation()
                            setExpandedStackKey(isStackOpen ? null : group.key)
                          }}
                          className="absolute bottom-1.5 right-1.5 px-1.5 py-0.5 rounded text-[9px] font-mono font-bold bg-ink text-base shadow hover:scale-105"
                          title="Click to view stack of similar captures"
                        >
                          +{stackCount - 1} similar
                        </button>
                      )}
                    </div>

                    {/* Meta info */}
                    <div className="mt-1.5 min-w-0">
                      <div className="text-[11px] font-medium truncate text-ink">
                        {primary.content_preview.replace(/^\[Photo captured:\s*/, '').replace(/\]$/, '') || primary.corroboration_key}
                      </div>
                      <div className="flex items-center justify-between text-[9px] font-mono text-ink-faint mt-0.5">
                        <span className="truncate">{primary.zone || group.key}</span>
                        <MonoValue>
                          {new Date(primary.captured_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                        </MonoValue>
                      </div>

                      {/* Severity Ladder Mini Rung */}
                      <SeverityLadderMini ladder={primary.vision?.severity_ladder} />
                    </div>

                    {/* Stack expansion view (if toggled) */}
                    {isStackOpen && stackCount > 1 && (
                      <div className="mt-2 pt-2 border-t border-line space-y-1">
                        <div className="text-[9px] font-mono font-medium text-ink-faint uppercase">
                          Stack ({stackCount} frames)
                        </div>
                        <div className="flex gap-1 overflow-x-auto pb-1">
                          {group.records.map((stacked) => (
                            <button
                              key={stacked.id}
                              onClick={(e) => {
                                e.stopPropagation()
                                openDetail(stacked.id)
                              }}
                              className={`relative w-9 h-9 rounded overflow-hidden shrink-0 border ${
                                selectedPointId === stacked.id ? 'border-good ring-1 ring-good' : 'border-line'
                              }`}
                            >
                              {stacked.thumbnail_url ? (
                                <img src={stacked.thumbnail_url} className="w-full h-full object-cover" />
                              ) : (
                                <div className="w-full h-full bg-base-sunken flex items-center justify-center text-[7px]">
                                  #{stacked.id}
                                </div>
                              )}
                            </button>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </div>

        {/* Selected Point Detail Modal / Drawer */}
        {selectedRecord && (
          <div className="p-3 border-t border-line bg-base-sunken/80 backdrop-blur-sm space-y-2">
            <div className="flex items-start justify-between gap-2">
              <div className="flex items-center gap-2">
                <span className="text-xs font-mono font-bold text-ink">
                  Point #{selectedRecord.id}
                </span>
                <StateBadge state={selectedRecord.state} />
                {selectedRecord.vision?.label && (
                  <span className="px-1.5 py-0.5 rounded text-[10px] font-mono bg-pending/20 text-pending border border-pending">
                    {selectedRecord.vision.label} ({Math.round(selectedRecord.vision.confidence * 100)}%)
                  </span>
                )}
              </div>
              <button
                onClick={() => setSelectedPointId(null)}
                className="text-[11px] font-mono text-ink-faint hover:text-ink px-1.5 py-0.5 border border-line rounded"
              >
                CLOSE
              </button>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-[11px]">
              <div>
                <div className="text-ink-faint font-mono text-[10px]">CORROBORATION KEY</div>
                <div className="font-mono text-ink text-xs">{selectedRecord.corroboration_key || '—'}</div>

                <div className="text-ink-faint font-mono text-[10px] mt-1.5">DECISION REASON</div>
                <div className="text-ink-dim">
                  {selectedRecord.decision_reason || selectedRecord.detail?.decision?.reason || '—'}
                </div>

                {selectedRecord.sync_verdict && (
                  <div className="mt-1 text-[10px] font-mono">
                    <span className="text-ink-faint">VERDICT: </span>
                    <span className="text-good">{selectedRecord.sync_verdict}</span>
                  </div>
                )}
              </div>

              <div>
                {/* Full Severity Ladder (Task C1) */}
                <SeverityLadderFull ladder={selectedRecord.vision?.severity_ladder} />

                {/* Consensus State if available */}
                {selectedRecord.detail?.consensus && (
                  <div className="mt-1.5 p-1.5 bg-base-sunken border border-line rounded text-[10px] font-mono">
                    <div className="flex items-center justify-between">
                      <span className="text-ink-faint">CONSENSUS:</span>
                      <span className={selectedRecord.detail.consensus.state === 'CONFIRMED' ? 'text-good' : 'text-alert'}>
                        {selectedRecord.detail.consensus.state} ({Math.round(selectedRecord.detail.consensus.confidence * 100)}%)
                      </span>
                    </div>
                    {selectedRecord.detail.consensus.explanation && (
                      <div className="text-ink-dim text-[9px] mt-0.5 truncate">
                        {selectedRecord.detail.consensus.explanation}
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>
          </div>
        )}
      </div>
    </Panel>
  )
}
