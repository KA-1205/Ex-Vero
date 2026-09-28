import { useEffect, useState } from 'react'
import { fetchTelemetry } from '../../api/client'
import type { TelemetrySample } from '../../types'
import { MonoValue } from '../primitives'

export function ResourceStrip({ deviceId }: { deviceId: string }) {
  const [samples, setSamples] = useState<TelemetrySample[]>([])

  useEffect(() => {
    fetchTelemetry(deviceId).then(setSamples)
    const id = setInterval(() => fetchTelemetry(deviceId).then(setSamples), 5000)
    return () => clearInterval(id)
  }, [deviceId])

  const latest = samples[samples.length - 1]
  if (!latest) return null

  return (
    <div className="border border-line bg-base-raised px-3 py-2 flex items-center gap-6 text-xs font-mono">
      <span className="text-ink-faint">TELEMETRY</span>
      <span className="text-ink-dim">
        CPU <MonoValue className="text-ink">{latest.cpu_pct}%</MonoValue>
      </span>
      <span className="text-ink-dim">
        RAM <MonoValue className="text-ink">{latest.ram_mb} MB</MonoValue>
      </span>
      <span className="text-ink-dim">
        QUERY LATENCY <MonoValue className="text-good">{latest.query_latency_ms} ms</MonoValue>
      </span>
      {latest.model_load_ms !== undefined && (
        <span className="text-ink-dim">
          MODEL LOAD <MonoValue className="text-ink">{latest.model_load_ms} ms</MonoValue>
        </span>
      )}
    </div>
  )
}
