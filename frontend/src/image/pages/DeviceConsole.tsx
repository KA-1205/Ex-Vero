import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { QAPanel } from '../components/device/QAPanel'
import { DecisionFeed } from '../components/device/DecisionFeed'
import { MemoryBrowser } from '../components/device/MemoryBrowser'
import { SyncStrip } from '../components/device/SyncStrip'
import { ResourceStrip } from '../components/device/ResourceStrip'
import { CapturePanel } from '../components/device/CapturePanel'
import { ActivityLog } from '../components/device/ActivityLog'

type Tab = 'console' | 'capture' | 'activity'

const TABS: { id: Tab; label: string }[] = [
  { id: 'console', label: 'CONSOLE' },
  { id: 'capture', label: 'CAPTURE' },
  { id: 'activity', label: 'ACTIVITY LOG' },
]

export function DeviceConsole() {
  const { deviceId = '' } = useParams()
  const [tab, setTab] = useState<Tab>('console')

  return (
    <div className="p-4 flex flex-col gap-3 h-full min-h-0">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-xs font-mono">
          <Link to="/image/overview" className="text-ink-dim hover:text-ink">
            FLEET
          </Link>
          <span className="text-ink-faint">/</span>
          <span className="text-ink">{deviceId}</span>
        </div>
        <div className="flex gap-1">
          {TABS.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`px-3 py-1 text-xs font-mono border ${
                tab === t.id
                  ? 'border-ink text-ink bg-base-sunken'
                  : 'border-line text-ink-dim hover:text-ink'
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
      </div>

      {tab === 'console' && (
        <div className="flex flex-col gap-3 flex-1 min-h-0">
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-3 flex-1 min-h-0">
            <QAPanel deviceId={deviceId} />
            <DecisionFeed deviceId={deviceId} />
            <MemoryBrowser deviceId={deviceId} />
          </div>
        </div>
      )}

      {tab === 'console' && (
        <div className="pointer-events-none fixed bottom-0 left-[calc(13rem+1rem)] right-4 z-40 flex flex-col gap-3">
          <SyncStrip deviceId={deviceId} />
          <ResourceStrip deviceId={deviceId} />
        </div>
      )}

      {tab === 'capture' && <CapturePanel deviceId={deviceId} />}

      {tab === 'activity' && (
        <div className="flex-1 min-h-0">
          <ActivityLog deviceId={deviceId} />
        </div>
      )}
    </div>
  )
}
