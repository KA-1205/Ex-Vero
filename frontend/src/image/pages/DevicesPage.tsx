import { FieldGraphic } from '../../components/FieldGraphic'
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { fetchDevices } from '../api/client'
import { Panel } from '../components/primitives'
import { DeviceTile } from './FleetOverview'
import type { DeviceSummary } from '../types'

export function DevicesPage() {
  const [devices, setDevices] = useState<DeviceSummary[] | null>(null)
  const navigate = useNavigate()

  useEffect(() => {
    fetchDevices().then(setDevices)
  }, [])

  return (
    <div className="ev-page ev-devices-page">
      <header className="ev-hero"><div className="ev-hero-copy"><div className="ev-eyebrow">FLEET / DEVICE CONSOLES</div><h1 className="ev-page-title">Intelligence at the edge.</h1><p>Open a device to inspect its observations, decisions, and local memory.</p></div><FieldGraphic /></header>
      <Panel title={`DEVICES — ${devices?.length ?? 0} TOTAL`}>
        <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-5 p-5">
          {devices === null && (
            <div className="text-ink-faint text-sm font-mono col-span-full py-8 text-center">
              loading devices…
            </div>
          )}
          {devices?.map((device) => (
            <DeviceTile
              key={device.id}
              device={device}
              onOpen={() => navigate(`/image/overview/devices/${device.id}`)}
            />
          ))}
        </div>
      </Panel>
    </div>
  )
}
