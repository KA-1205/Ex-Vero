import { useEffect, useMemo, useState } from 'react'
import { fetchMemoryRecords } from '../../api/client'
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

  useEffect(() => {
    fetchMemoryRecords(deviceId).then(setRecords)
  }, [deviceId])

  const filtered = useMemo(
    () =>
      records.filter((r) =>
        r.content_preview.toLowerCase().includes(filter.toLowerCase()),
      ),
    [records, filter],
  )

  return (
    <Panel title="LOCAL MEMORY BROWSER" className="h-full">
      <div className="flex flex-col h-full">
        <div className="p-2 border-b border-line">
          <input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="filter…"
            className="w-full bg-base-sunken border border-line px-2 py-1 text-xs outline-none focus-visible:border-good"
          />
        </div>
        <div className="flex-1 overflow-y-auto divide-y divide-line">
          {filtered.map((r) => {
            const isOpen = expanded === r.id
            return (
              <div key={r.id}>
                <button
                  onClick={() => setExpanded(isOpen ? null : r.id)}
                  className="w-full text-left p-2 hover:bg-base-sunken flex items-start justify-between gap-2"
                >
                  <div className="min-w-0">
                    <div className="text-sm truncate">{r.content_preview}</div>
                    <div className="text-[11px] text-ink-faint font-mono">{r.zone}</div>
                  </div>
                  <StateBadge state={r.state} />
                </button>
                {isOpen && (
                  <div className="px-2 pb-2 text-[11px] text-ink-dim space-y-1 bg-base-sunken">
                    <div>
                      <span className="text-ink-faint">decision: </span>
                      {r.decision_reason}
                    </div>
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
