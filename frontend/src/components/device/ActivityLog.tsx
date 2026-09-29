import { useEffect, useState } from 'react'
import { fetchActivity, subscribeDeviceEvents } from '../../api/client'
import type { ActivityEntry, ActivityKind } from '../../types'
import { MonoValue, Panel } from '../primitives'

const KINDS: ActivityKind[] = [
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

const KIND_STYLES: Record<ActivityKind, string> = {
  capture: 'text-ink-dim border-line',
  decision: 'text-ink border-line',
  push_attempt: 'text-pending border-pending',
  push_result: 'text-good border-good',
  pull_result: 'text-good border-good',
  consensus: 'text-ink border-ink-faint',
  retraction: 'text-alert border-alert',
  error: 'text-alert border-alert',
  mode_change: 'text-pending border-pending',
}

export function ActivityLog({ deviceId }: { deviceId: string }) {
  const [entries, setEntries] = useState<ActivityEntry[]>([])
  const [kindFilter, setKindFilter] = useState<ActivityKind | 'all'>('all')

  useEffect(() => {
    let active = true
    const refresh = () => void fetchActivity(deviceId, kindFilter === 'all' ? undefined : kindFilter).then((rows) => { if (active) setEntries(rows) }).catch((error) => console.error('Unable to load activity', error))
    refresh()
    const unsubscribe = subscribeDeviceEvents(deviceId, (frame) => {
      if (frame.type !== 'activity') return
      const entry = frame.data
      setEntries((rows) => [entry, ...rows.filter((row) => !(row.timestamp === entry.timestamp && row.kind === entry.kind && row.point_id === entry.point_id))].slice(0, 100))
    })
    return () => { active = false; unsubscribe() }
  }, [deviceId, kindFilter])

  const visible = entries

  return (
    <Panel
      title="ACTIVITY LOG"
      action={
        <select
          value={kindFilter}
          onChange={(e) => setKindFilter(e.target.value as ActivityKind | 'all')}
          className="bg-base-sunken border border-line text-[11px] font-mono px-1.5 py-0.5 text-ink-dim outline-none"
        >
          <option value="all">all kinds</option>
          {KINDS.map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>
      }
    >
      <div className="divide-y divide-line">
        {visible.map((e) => (
          <div key={e.id} className="p-2 flex items-start gap-3 text-xs">
            <MonoValue className="text-ink-faint w-20 shrink-0">
              {new Date(e.timestamp).toLocaleTimeString()}
            </MonoValue>
            <span className={`font-mono px-1.5 py-0.5 border shrink-0 ${KIND_STYLES[e.kind]}`}>
              {e.kind}
            </span>
            <span className="text-ink-dim">{e.detail}</span>
            {e.confidence !== undefined && (
              <MonoValue className="text-ink-faint ml-auto shrink-0">
                conf {e.confidence.toFixed(2)}
              </MonoValue>
            )}
          </div>
        ))}
        {visible.length === 0 && (
          <div className="text-ink-faint text-xs font-mono py-6 text-center">no activity of this kind</div>
        )}
      </div>
    </Panel>
  )
}
