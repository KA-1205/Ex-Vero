import { useEffect, useState } from 'react'
import { subscribeDeviceEvents } from '../../api/client'
import { mockDecisionFeed } from '../../api/mock'
import type { DecisionFeedEntry } from '../../types'
import { MonoValue, Panel, VerdictBadge } from '../primitives'

export function DecisionFeed({ deviceId }: { deviceId: string }) {
  const [entries, setEntries] = useState<DecisionFeedEntry[]>(() => mockDecisionFeed(deviceId))

  useEffect(() => {
    setEntries(mockDecisionFeed(deviceId))
    const unsub = subscribeDeviceEvents(deviceId, (entry) => {
      setEntries((prev) => [entry, ...prev].slice(0, 100))
    })
    return unsub
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
