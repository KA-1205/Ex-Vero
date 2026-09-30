import { useEffect, useMemo, useState } from 'react'
import { fetchMemoryDetail, fetchMemoryRecords, retractPoint } from '../../api/client'
import type { MemoryRecord, MemoryState } from '../../types'
import { MonoValue, Panel } from '../primitives'

const STATE_STYLES: Record<MemoryState, string> = {
  local_only: 'text-ink-dim border-line',
  synced: 'text-good border-good',
  pending: 'text-pending border-pending',
}

function StateBadge({ state }: { state: MemoryState }) {
  return (
    <span className={`font-mono text-[10px] px-1.5 py-0.5 border ${STATE_STYLES[state]}`}>
      {state.replace('_', ' ').toUpperCase()}
    </span>
  )
}

export function MemoryBrowser({ deviceId }: { deviceId: string }) {
  const [records, setRecords] = useState<MemoryRecord[]>([])
  const [filter, setFilter] = useState('')
  const [expanded, setExpanded] = useState<string | null>(null)
  const [syncState, setSyncState] = useState('')
  const [modality, setModality] = useState('')
  const [zone, setZone] = useState('')
  const [loadingDetail, setLoadingDetail] = useState(false)
  const [retracting, setRetracting] = useState<string | null>(null)
  const [retracted, setRetracted] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    void fetchMemoryRecords(deviceId, { q: filter, sync_state: syncState || undefined, modality: modality || undefined, zone: zone || undefined }).then((rows) => { if (active) setRecords(rows) }).catch((error) => console.error('Unable to load memory', error))
    return () => { active = false }
  }, [deviceId, filter, syncState, modality, zone])

  const filtered = useMemo(
    () =>
      records.filter((r) => !filter || r.content_preview.toLowerCase().includes(filter.toLowerCase())),
    [records, filter],
  )

  return (
    <Panel title="LOCAL MEMORY BROWSER" className="h-full">
      <div className="flex flex-col h-full">
        <div className="p-2 border-b border-line grid grid-cols-2 gap-1.5">
          <input
            className="col-span-2 w-full bg-base-sunken border border-line px-2 py-1 text-xs outline-none focus-visible:border-good"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="filter…"
          />
          <select value={syncState} onChange={(e) => setSyncState(e.target.value)} className="bg-base-sunken border border-line px-1 py-1 text-[10px] font-mono"><option value="">all sync states</option><option value="local_only">local only</option><option value="pending">pending</option><option value="synced">synced</option></select>
          <select value={modality} onChange={(e) => setModality(e.target.value)} className="bg-base-sunken border border-line px-1 py-1 text-[10px] font-mono"><option value="">all modalities</option><option value="text">text</option><option value="vision">image</option></select>
          <input value={zone} onChange={(e) => setZone(e.target.value)} placeholder="zone" className="col-span-2 bg-base-sunken border border-line px-2 py-1 text-xs outline-none focus-visible:border-good" />
        </div>
        <div className="flex-1 overflow-y-auto divide-y divide-line">
          {filtered.map((r) => {
            const isOpen = expanded === r.id
            return (
              <div key={r.id}>
                <button
                  onClick={() => {
                    if (isOpen) { setExpanded(null); return }
                    setExpanded(r.id)
                    if (!r.detail) {
                      setLoadingDetail(true)
                      void fetchMemoryDetail(deviceId, r.id).then((detail) => setRecords((rows) => rows.map((row) => row.id === r.id ? { ...row, detail, decision_reason: detail.decision.reason, sync_verdict: detail.decision.verdict } : row))).catch((error) => console.error('Unable to load memory detail', error)).finally(() => setLoadingDetail(false))
                    }
                  }}
                  className="w-full text-left p-2 hover:bg-base-sunken flex items-start justify-between gap-2"
                >
                  {r.thumbnail_url ? <img src={r.thumbnail_url} alt="Captured field report" className="h-10 w-10 shrink-0 border border-line object-cover" /> : <span aria-hidden="true" className="flex h-10 w-10 shrink-0 items-center justify-center border border-line font-mono text-[9px] text-ink-faint">{r.modality === 'vision' ? 'IMG' : 'TXT'}</span>}
                  <div className="min-w-0">
                    <div className="text-sm truncate">{r.content_preview}</div>
                    <div className="text-[11px] text-ink-faint font-mono">{r.zone}</div>
                  </div>
                  <StateBadge state={r.state} />
                </button>
                {isOpen && (
                  <div className="px-2 pb-2 text-[11px] text-ink-dim space-y-1 bg-base-sunken">
                    {loadingDetail && <div className="font-mono text-ink-faint">loading fact history…</div>}
                    <div>
                      <span className="text-ink-faint">decision: </span>
                      {r.decision_reason}
                    </div>
                    {r.detail?.activity.map((entry) => <div key={`${entry.timestamp}-${entry.kind}`}><MonoValue>{new Date(entry.timestamp).toLocaleTimeString()}</MonoValue> · {entry.kind}: {entry.detail}</div>)}
                    {r.detail?.consensus && <div>consensus: {r.detail.consensus.state} · {r.detail.consensus.explanation}</div>}
                    <button disabled={retracting === r.id} onClick={async () => {
                      setRetracting(r.id)
                      try { await retractPoint(deviceId, r.id); setRetracted(r.id); void fetchMemoryRecords(deviceId, { q: filter, sync_state: syncState || undefined, modality: modality || undefined, zone: zone || undefined }).then(setRecords) }
                      catch (error) { console.error('Unable to retract memory point', error) }
                      finally { setRetracting(null) }
                    }} className="mt-1 border border-alert px-2 py-1 font-mono text-[10px] text-alert disabled:opacity-50">{retracting === r.id ? 'RETRACTING…' : 'RETRACT FACT'}</button>
                    {retracted === r.id && <span className="ml-2 font-mono text-[10px] text-good">RETRACTION RECORDED</span>}
                    {r.sync_verdict && (
                      <div>
                        <span className="text-ink-faint">sync verdict: </span>
                        {r.sync_verdict.replace(/_/g, ' ')}
                      </div>
                    )}
                    <div>
                      <span className="text-ink-faint">captured: </span>
                      <MonoValue>{new Date(r.captured_at).toLocaleString()}</MonoValue>
                    </div>
                  </div>
                )}
              </div>
            )
          })}
          {filtered.length === 0 && (
            <div className="text-ink-faint text-xs font-mono py-6 text-center">no matches</div>
          )}
        </div>
      </div>
    </Panel>
  )
}
