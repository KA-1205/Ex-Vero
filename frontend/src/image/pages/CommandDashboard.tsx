import { useEffect, useState } from 'react'
import { fetchCloudState } from '../api/client'
import type { CloudFact } from '../types'
import { MonoValue, Panel } from '../components/primitives'

export function CommandDashboard() {
  const [facts, setFacts] = useState<CloudFact[]>([])

  useEffect(() => {
    fetchCloudState().then(setFacts)
    const id = setInterval(() => fetchCloudState().then(setFacts), 8000)
    return () => clearInterval(id)
  }, [])

  const confirmed = facts.filter((f) => f.status === 'CONFIRMED')
  const disputed = facts.filter((f) => f.status === 'DISPUTED')

  return (
    <div className="p-4 flex flex-col gap-3 h-full">
      <div className="flex items-center gap-4 text-xs font-mono">
        <span className="text-ink-dim">TRUSTED FACTS</span>
        <MonoValue className="text-good">{confirmed.length} confirmed</MonoValue>
        <MonoValue className="text-alert">{disputed.length} disputed</MonoValue>
      </div>
      <Panel title="MERGED TRUSTED PICTURE — CLOUD VIEW" className="flex-1">
        <div className="divide-y divide-line">
          {facts.map((f) => (
            <div key={f.id} className="p-3 flex items-start justify-between gap-4">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className="text-[11px] font-mono text-ink-faint">{f.zone}</span>
                  <span
                    className={`font-mono text-[10px] px-1.5 py-0.5 border ${
                      f.status === 'CONFIRMED' ? 'border-good text-good' : 'border-alert text-alert'
                    }`}
                  >
                    {f.status}
                  </span>
                </div>
                <div className="text-sm mt-1">{f.summary}</div>
                <div className="text-[11px] text-ink-faint font-mono mt-1">
                  corroborated by {f.corroborating_devices.join(', ')}
                </div>
              </div>
              <div className="text-right shrink-0">
                <MonoValue className="text-sm">{f.confidence.toFixed(2)}</MonoValue>
                <div className="text-[11px] text-ink-faint">
                  {new Date(f.last_updated).toLocaleTimeString()}
                </div>
              </div>
            </div>
          ))}
          {facts.length === 0 && (
            <div className="text-ink-faint text-xs font-mono py-8 text-center">
              no trusted facts yet — waiting on consensus fold
            </div>
          )}
        </div>
      </Panel>
    </div>
  )
}
