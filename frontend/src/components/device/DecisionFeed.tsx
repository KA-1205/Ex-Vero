import { useEffect, useState } from 'react'
import { fetchDecisionFeed, subscribeDeviceEvents } from '../../api/client'
import type { DecisionFeedEntry } from '../../types'
import { MonoValue, Panel, VerdictBadge } from '../primitives'

export function DecisionFeed({ deviceId }: { deviceId: string }) {
  const [entries, setEntries] = useState<DecisionFeedEntry[]>([])

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

  return (
    <Panel title="LIVE DECISION FEED" className="h-full">
      <div className="h-full overflow-y-auto divide-y divide-line">
        {entries.map((e) => (
          <div key={e.id} className="p-2.5 flex gap-3">
            {e.thumbnail_url ? (
              <img src={e.thumbnail_url} className="w-10 h-10 object-cover border border-line shrink-0" />
            ) : (
              <div className="w-10 h-10 border border-line shrink-0 flex items-center justify-center text-[10px] font-mono text-ink-faint">
                {e.modality === 'vision' ? 'IMG' : 'TXT'}
              </div>
            )}
            <div className="min-w-0 flex-1">
              <div className="flex items-center justify-between gap-2">
                <div className="text-sm truncate">{e.content_preview}</div>
                <VerdictBadge verdict={e.verdict} />
              </div>
              <div className="text-[11px] text-ink-dim mt-0.5">{e.reason}</div>
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
