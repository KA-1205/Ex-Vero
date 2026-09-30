import { useEffect, useState } from 'react'
import { fetchDeviceTrend, type DeviceTrend } from '../../api/client'
import { MonoValue } from '../primitives'

export function TrendStrip({ deviceId }: { deviceId: string }) {
  const [trend, setTrend] = useState<DeviceTrend | null>(null)

  useEffect(() => {
    let active = true
    const load = () => {
      fetchDeviceTrend(deviceId)
        .then((t) => {
          if (active) setTrend(t)
        })
        .catch(() => {})
    }
    load()
    const id = setInterval(load, 8000)
    return () => {
      active = false
      clearInterval(id)
    }
  }, [deviceId])

  if (!trend || !trend.frames || trend.frames.length === 0) return null

  const isEvolving = trend.status === 'EVOLVING'

  return (
    <div className="glass-card px-3 py-2 flex flex-wrap items-center justify-between gap-3 text-xs bg-white/40 backdrop-blur-md rounded border border-line">
      <div className="flex items-center gap-2">
        <span className="text-ink-dim font-mono text-[11px] font-semibold">CHANGE-OVER-TIME</span>
        <span
          className={`font-mono text-[10px] px-2 py-0.5 border rounded-sm font-bold ${
            isEvolving ? 'border-alert text-alert bg-alert/15' : 'border-good text-good bg-good/15'
          }`}
        >
          {trend.status}
        </span>
        <span className="text-[10px] font-mono text-ink-faint">
          latest drift: <MonoValue>{trend.latest_drift.toFixed(3)}</MonoValue>
        </span>
      </div>

      {/* Frame Strip */}
      <div className="flex items-center gap-2 overflow-x-auto py-1">
        {trend.frames.map((frame, idx) => (
          <div key={frame.point_id || idx} className="flex items-center gap-1.5 shrink-0">
            {idx > 0 && (
              <div className="text-[9px] font-mono text-ink-faint px-1">
                → {frame.drift_from_prev ? (frame.drift_from_prev).toFixed(2) : '0.00'}
              </div>
            )}
            <div className="relative w-8 h-8 rounded overflow-hidden border border-line bg-black/10">
              {frame.thumbnail_url ? (
                <img src={frame.thumbnail_url} alt="" className="w-full h-full object-cover" />
              ) : (
                <div className="w-full h-full bg-base-sunken flex items-center justify-center text-[8px] font-mono">
                  #{frame.point_id}
                </div>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
