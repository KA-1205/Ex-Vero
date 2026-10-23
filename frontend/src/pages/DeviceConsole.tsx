import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { QAPanel } from '../components/device/QAPanel'
import { DecisionFeed } from '../components/device/DecisionFeed'
import { MemoryBrowser } from '../components/device/MemoryBrowser'
import { SyncStrip } from '../components/device/SyncStrip'
import { ResourceStrip } from '../components/device/ResourceStrip'
import { CapturePanel } from '../components/device/CapturePanel'
import { ActivityLog } from '../components/device/ActivityLog'
import { fetchDevice } from '../api/client'
import type { DeviceSummary } from '../types'
import { ConnectivityPill } from '../components/primitives'

type Tab = 'console' | 'capture' | 'activity'

const TABS: { id: Tab; label: string }[] = [
  { id: 'console', label: 'CONSOLE' },
  { id: 'capture', label: 'CAPTURE' },
  { id: 'activity', label: 'ACTIVITY LOG' },
]

export function DeviceConsole() {
  const { deviceId = '' } = useParams()
  const [tab, setTab] = useState<Tab>('console')
  const [device, setDevice] = useState<DeviceSummary | null>(null)

  useEffect(() => { let active = true; void fetchDevice(deviceId).then((value) => { if (active) setDevice(value) }).catch((error) => console.error('Unable to load device header', error)); return () => { active = false } }, [deviceId])

  return (
    <div className="ev-page ev-device-console">
      <div className="ev-console-heading">
        <div className="flex items-center gap-2 text-xs font-mono">
          <Link to="/numeric/overview" className="text-ink-dim hover:text-ink">
            FLEET
          </Link>
          <span className="text-ink-faint">/</span>
          <span className="text-ink">{device?.name ?? deviceId}</span>
          {device && <ConnectivityPill state={device.connectivity} />}
        </div>
        <div className="ev-console-tabs" aria-label="Console sections">
          {TABS.map((t) => (
            <button
              key={t.id}
              aria-pressed={tab === t.id}
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
          <div className="ev-console-grid grid grid-cols-1 xl:grid-cols-3 gap-5 min-h-0">
            <QAPanel deviceId={deviceId} />
            <DecisionFeed deviceId={deviceId} />
            <MemoryBrowser deviceId={deviceId} />
          </div>
        </div>
      )}

      {tab === 'console' && (
        <div className="ev-console-strips flex flex-col gap-3">
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
