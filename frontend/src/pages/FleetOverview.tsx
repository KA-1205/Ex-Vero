import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { fetchDevices } from '../api/client'
import type { DeviceSummary } from '../types'
import { ConnectivityPill, MonoValue, Panel, StatLabel } from '../components/primitives'

function Sparkline({ values }: { values: number[] }) {
  const max = Math.max(...values, 1)
  return (
    <div className="flex items-end gap-px h-6">
      {values.map((v, i) => (
        <div
          key={i}
          className="w-1 bg-ink-faint"
          style={{ height: `${Math.max((v / max) * 100, 6)}%` }}
        />
      ))}
    </div>
  )
}

export function DeviceTile({ device, onOpen }: { device: DeviceSummary; onOpen: () => void }) {
  const pct = Math.round((device.memory_used / device.memory_cap) * 100)
  return (
    <button
      onClick={onOpen}
      className="glass-card text-left p-3 hover:border-[#315C86] transition-colors flex flex-col gap-2"
    >
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="text-sm font-medium">{device.name}</div>
          <div className="text-[11px] text-ink-faint font-mono">{device.id}</div>
        </div>
        <ConnectivityPill state={device.connectivity} />
      </div>
      <div>
        <StatLabel>local memory</StatLabel>
        <div className="flex items-center gap-2">
          <MonoValue className="text-sm">
            {device.memory_used}/{device.memory_cap}
          </MonoValue>
          <span className="text-[11px] text-ink-faint font-mono">points ({pct}%)</span>
        </div>
        <div className="h-1 bg-base-sunken mt-1">
          <div
            className={`h-1 ${pct > 85 ? 'bg-alert' : pct > 60 ? 'bg-pending' : 'bg-good'}`}
            style={{ width: `${pct}%` }}
          />
        </div>
      </div>
      <div className="flex items-center justify-between">
        <div>
          <StatLabel>last sync</StatLabel>
          <MonoValue className="text-xs">
            {device.last_sync_at ? new Date(device.last_sync_at).toLocaleTimeString() : '—'}
          </MonoValue>
        </div>
        <Sparkline values={device.activity_sparkline} />
      </div>
    </button>
  )
}

export function FleetOverview() {
  const [devices, setDevices] = useState<DeviceSummary[] | null>(null)
  const navigate = useNavigate()

  useEffect(() => {
    fetchDevices().then(setDevices)
  }, [])

  return (
    <div className="p-4">
      <Panel title={`FLEET — ${devices?.length ?? 0} DEVICES`}>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3 p-3">
          {devices === null && (
            <div className="text-ink-faint text-sm font-mono col-span-full py-8 text-center">
              loading fleet state…
            </div>
          )}
          {devices?.map((d) => (
            <DeviceTile key={d.id} device={d} onOpen={() => navigate(`/overview/devices/${d.id}`)} />
          ))}
        </div>
      </Panel>
    </div>
  )
}
