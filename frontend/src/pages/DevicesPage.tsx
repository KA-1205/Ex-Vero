import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { fetchDevices } from '../api/client'
import { Panel } from '../components/primitives'
import { DeviceTile } from './FleetOverview'
import type { DeviceSummary } from '../types'

export function DevicesPage() {
  const [devices, setDevices] = useState<DeviceSummary[] | null>(null)
  const [error, setError] = useState(false)
  const navigate = useNavigate()

  async function loadDevices() {
    setError(false)
    try {
      const result = await fetchDevices()
      setDevices(result)
    } catch {
      setError(true)
    }
  }

  useEffect(() => {
    void loadDevices()
  }, [])

  return (
    <div className="p-4">
      <Panel title={`DEVICES — ${devices?.length ?? 0} TOTAL`}>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3 p-3">
          {error && (
            <div role="alert" className="col-span-full py-8 text-center text-sm text-ink-dim">
              Device list unavailable. <button type="button" onClick={() => void loadDevices()} className="underline">Retry</button>
            </div>
          )}
          {devices === null && !error && (
            <div className="text-ink-faint text-sm font-mono col-span-full py-8 text-center">
              loading devices…
            </div>
          )}
          {devices?.length === 0 && !error && (
            <div className="col-span-full py-8 text-center text-sm text-ink-faint">No devices found.</div>
          )}
          {devices?.map((device) => (
            <DeviceTile
              key={device.id}
              device={device}
              onOpen={() => navigate(`/numeric/overview/devices/${device.id}`)}
            />
          ))}
        </div>
      </Panel>
    </div>
  )
}
