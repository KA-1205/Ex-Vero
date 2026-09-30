import { useEffect, useState } from 'react'
import { fetchCloudDashboard } from '../api/client'
import type { CloudFact } from '../types'
import { MonoValue, Panel } from '../components/primitives'

export function CommandDashboard() {
  const [facts, setFacts] = useState<CloudFact[]>([])
  const [deviceTrust, setDeviceTrust] = useState<Record<string, number>>({})

  useEffect(() => {
    const load = () => {
      fetchCloudDashboard()
        .then((res) => {
          setFacts(res.facts)
          setDeviceTrust(res.device_trust || {})
        })
        .catch((err) => console.error('Unable to fetch cloud dashboard', err))
    }

    load()
    const id = setInterval(load, 5000)
    return () => clearInterval(id)
  }, [])

  const confirmed = facts.filter((f) => f.status === 'CONFIRMED')
  const disputed = facts.filter((f) => f.status === 'DISPUTED')

  const trustEntries = Object.entries(deviceTrust).sort((a, b) => b[1] - a[1])

  return (
    <div className="p-4 flex flex-col gap-4 h-full min-h-0 overflow-y-auto">
      {/* Top Metric Bar */}
      <div className="flex flex-wrap items-center justify-between gap-3 bg-white/40 backdrop-blur-md p-3 rounded-lg border border-line">
        <div className="flex items-center gap-4 text-xs font-mono">
          <span className="font-bold text-ink tracking-wide">FLEET CONSENSUS VIEW</span>
          <span className="text-line">|</span>
          <MonoValue className="text-good font-bold">{confirmed.length} confirmed</MonoValue>
          <MonoValue className="text-alert font-bold">{disputed.length} disputed</MonoValue>
          <span className="text-ink-faint">({facts.length} active zones)</span>
        </div>
        <div className="text-[10px] font-mono text-ink-faint">
          Real-time Qdrant Server Hub Sync
        </div>
      </div>

      {/* Main Grid: Per-Zone Photo Tiles + Trust Bars */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 flex-1 min-h-0">
        {/* Per-Zone Tiles (Task A8) */}
        <div className="lg:col-span-2 flex flex-col min-h-0">
          <Panel title="PER-ZONE INCIDENT MAP & VISUAL FACTS" className="h-full">
            <div className="p-3 overflow-y-auto h-full">
              {facts.length === 0 ? (
                <div className="text-ink-faint text-xs font-mono py-16 text-center">
                  Waiting on edge camera ingests & consensus fold…
                </div>
              ) : (
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  {facts.map((f) => {
                    const isConfirmed = f.status === 'CONFIRMED'
                    const confPct = Math.round(f.confidence * 100)

                    return (
                      <div
                        key={f.id}
                        className="glass-card flex flex-col rounded-lg border border-line bg-white/35 p-3 hover:bg-white/50 transition-colors"
                      >
                        {/* Zone Header + State Pill */}
                        <div className="flex items-center justify-between mb-2">
                          <span className="font-mono text-xs font-bold text-ink">
                            {f.zone || f.corroboration_key}
                          </span>
                          <span
                            className={`font-mono text-[9px] px-2 py-0.5 border rounded-sm font-bold ${
                              isConfirmed
                                ? 'border-good text-good bg-good/15'
                                : 'border-alert text-alert bg-alert/15'
                            }`}
                          >
                            {f.status}
                          </span>
                        </div>

                        {/* Representative Photo (Task A8) */}
                        <div className="relative aspect-video w-full rounded overflow-hidden bg-black/10 border border-line mb-2.5">
                          {f.thumbnail_url ? (
                            <img
                              src={f.thumbnail_url}
                              alt={f.summary}
                              className="w-full h-full object-cover"
                              loading="lazy"
                            />
                          ) : (
                            <div className="w-full h-full flex flex-col items-center justify-center text-[10px] font-mono text-ink-faint">
                              <span>[ZONE PHOTO]</span>
                              <span className="text-[9px] text-ink-faint">{f.corroboration_key}</span>
                            </div>
                          )}
                          <div className="absolute bottom-1.5 right-1.5 px-1.5 py-0.5 rounded bg-black/75 text-white font-mono text-[9px] font-bold">
                            {confPct}% conf
                          </div>
                        </div>

                        {/* Merged Fact Summary */}
                        <div className="text-xs font-medium text-ink mb-1 truncate">
                          Status: <span className="font-mono font-bold">{f.summary}</span>
                        </div>

                        {/* Corroborating Devices & Time */}
                        <div className="mt-auto pt-2 border-t border-line/60 flex items-center justify-between text-[10px] font-mono text-ink-faint">
                          <div className="truncate pr-2" title={f.corroborating_devices.join(', ')}>
                            cams: {f.corroborating_devices.join(', ') || '—'}
                          </div>
                          <MonoValue className="shrink-0">
                            {new Date(f.last_updated).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                          </MonoValue>
                        </div>
                      </div>
                    )
                  })}
                </div>
              )}
            </div>
          </Panel>
        </div>

        {/* Device Trust Bars (Task A8) */}
        <div className="flex flex-col min-h-0">
          <Panel title="DEVICE TRUST RATINGS" className="h-full">
            <div className="p-3 overflow-y-auto space-y-3 h-full">
              <div className="text-[11px] text-ink-dim leading-relaxed">
                Trust is derived continuously by the hub consensus fold based on reporting agreement & validity.
              </div>

              {trustEntries.length === 0 ? (
                <div className="text-ink-faint text-xs font-mono py-8 text-center">
                  Calculating fleet trust…
                </div>
              ) : (
                <div className="space-y-2.5">
                  {trustEntries.map(([devId, score]) => {
                    const scorePct = Math.min(100, Math.max(0, Math.round(score * 100)))
                    const isCamera = devId.startsWith('cam-')

                    return (
                      <div key={devId} className="p-2 rounded bg-base-sunken/60 border border-line text-xs font-mono">
                        <div className="flex items-center justify-between mb-1">
                          <div className="flex items-center gap-1.5">
                            <span className="font-bold text-ink">{devId}</span>
                            <span className="text-[9px] px-1 py-0.2 rounded border border-line text-ink-faint">
                              {isCamera ? 'VISION' : 'PATROL'}
                            </span>
                          </div>
                          <MonoValue className="font-bold text-ink">
                            {(score).toFixed(2)}
                          </MonoValue>
                        </div>

                        {/* Trust Bar */}
                        <div className="w-full h-1.5 bg-black/10 rounded-full overflow-hidden">
                          <div
                            className={`h-full transition-all duration-500 ${
                              score >= 0.8 ? 'bg-good' : score >= 0.5 ? 'bg-pending' : 'bg-alert'
                            }`}
                            style={{ width: `${scorePct}%` }}
                          />
                        </div>
                      </div>
                    )
                  })}
                </div>
              )}
            </div>
          </Panel>
        </div>
      </div>
    </div>
  )
}
