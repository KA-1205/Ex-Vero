import { useEffect, useState } from 'react'
import { fetchDecisionFeed, subscribeDeviceEvents } from '../../api/client'
import type { DecisionFeedEntry } from '../../types'
import { MonoValue, Panel, VerdictBadge } from '../primitives'

export function DecisionFeed({ deviceId }: { deviceId: string }) {
  const [entries, setEntries] = useState<DecisionFeedEntry[]>([])

  useEffect(() => {
    let active = true
    setEntries([])
    void fetchDecisionFeed(deviceId).then((rows) => { if (active) setEntries(rows) }).catch((error) => console.error('Unable to load image decision feed', error))
    const unsub = subscribeDeviceEvents(deviceId, (frame) => {
      if (frame.type !== 'decision') return
      const e = frame.data
      const entry: DecisionFeedEntry = { ...e, id: String(e.point_id), content_preview: e.value_preview }
      setEntries((prev) => [entry, ...prev.filter((row) => row.id !== entry.id)].slice(0, 100))
    })
    return () => { active = false; unsub() }
  }, [deviceId])

  return (
    <Panel title="DECISION TRAIL" className="ev-decision-feed h-full">
      <div className="h-full overflow-y-auto divide-y divide-line">
        {entries.map((e) => (
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
