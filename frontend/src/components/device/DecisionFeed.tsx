import { useEffect, useState } from 'react'
import { fetchDecisionFeed, subscribeDeviceEvents } from '../../api/client'
import type { DecisionFeedEntry } from '../../types'
import { MonoValue, Panel, VerdictBadge } from '../primitives'

type FilterVerdict = 'ALL' | 'QUEUE_HIGH' | 'QUEUE_LOW' | 'KEEP_LOCAL' | 'REJECT'

export function DecisionFeed({ deviceId }: { deviceId: string }) {
  const [entries, setEntries] = useState<DecisionFeedEntry[]>([])
  const [filter, setFilter] = useState<FilterVerdict>('ALL')

  useEffect(() => {
    let active = true
    setEntries([])
    void fetchDecisionFeed(deviceId).then((rows) => { if (active) setEntries(rows) }).catch((error) => console.error('Unable to load decision feed', error))
    const unsub = subscribeDeviceEvents(deviceId, (frame) => {
      if (frame.type !== 'decision') return
      const data = frame.data
      const entry: DecisionFeedEntry = { ...data, id: String(data.point_id), content_preview: data.value_preview }
      setEntries((prev) => [entry, ...prev.filter((row) => row.id !== entry.id)].slice(0, 100))
    })
    return () => { active = false; unsub() }
  }, [deviceId])

  const filtered = filter === 'ALL' ? entries : entries.filter((e) => e.verdict === filter)

  return (
    <Panel title="DECISION TRAIL" className="ev-decision-feed h-full flex flex-col">
      <div className="flex flex-wrap items-center gap-1.5 p-2 border-b border-line bg-base-sunken/40 text-[10px] font-mono shrink-0">
        {(['ALL', 'QUEUE_HIGH', 'QUEUE_LOW', 'KEEP_LOCAL', 'REJECT'] as const).map((v) => (
          <button
            key={v}
            type="button"
            onClick={() => setFilter(v)}
            className={`px-2 py-0.5 border rounded transition-colors ${
              filter === v
                ? 'border-ink bg-base-sunken text-ink font-bold shadow-xs'
                : 'border-transparent text-ink-dim hover:text-ink hover:border-line'
            }`}
          >
            {v === 'ALL' ? `ALL (${entries.length})` : v.replace('_', ' ')}
          </button>
        ))}
      </div>
      <div className="flex-1 overflow-y-auto divide-y divide-line">
        {filtered.length === 0 && (
          <div className="text-ink-faint text-xs font-mono py-8 text-center">
            {entries.length === 0 ? 'no decision events recorded yet' : `no events matching filter "${filter}"`}
          </div>
        )}
        {filtered.map((e) => (
          <div key={e.id} className="ev-decision-entry p-4 flex gap-3">
            {e.thumbnail_url ? (
              <img src={e.thumbnail_url} alt={e.content_preview} className="w-10 h-10 object-cover border border-line shrink-0" />
            ) : (
              <div className="w-10 h-10 border border-line shrink-0 flex items-center justify-center text-[10px] font-mono text-ink-faint">
                {e.modality === 'vision' ? 'IMG' : 'TXT'}
              </div>
            )}
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div className="text-sm leading-relaxed">{e.content_preview}</div>
                <VerdictBadge verdict={e.verdict} />
              </div>
              <div className="text-xs leading-relaxed text-ink-dim mt-2">{e.reason}</div>
              <div className="flex items-center gap-3 mt-1">
                <MonoValue className="text-[10px] text-ink-faint">
                  {new Date(e.timestamp).toLocaleTimeString()}
                </MonoValue>
                {e.urgency_score !== undefined && (
                  <MonoValue className="text-[10px] text-ink-faint">
                    urgency {e.urgency_score.toFixed(2)}
                  </MonoValue>
                )}
              </div>
            </div>
          </div>
        ))}
      </div>
    </Panel>
  )
}
