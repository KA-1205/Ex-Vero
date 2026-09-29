import { useEffect, useState } from 'react'
import { fetchSyncStatus } from '../../api/client'
import type { SyncStatus } from '../../types'
import { MonoValue } from '../primitives'

export function SyncStrip({ deviceId }: { deviceId: string }) {
  const [status, setStatus] = useState<SyncStatus | null>(null)

  useEffect(() => {
    fetchSyncStatus(deviceId).then(setStatus)
    const id = setInterval(() => fetchSyncStatus(deviceId).then(setStatus), 6000)
    return () => clearInterval(id)
  }, [deviceId])

  if (!status) return null

  const { URGENT, ROUTINE, HELD } = status.pending_by_priority
  const total = URGENT + ROUTINE + HELD || 1

  return (
    <div className="glass-card px-3 py-2 flex flex-wrap items-center gap-x-6 gap-y-2 text-xs">
      <div className="flex items-center gap-2">
        <span className="text-ink-dim font-mono">PENDING SYNC</span>
        <div className="flex h-2.5 w-40 border border-line overflow-hidden">
          <div className="bg-alert" style={{ width: `${(URGENT / total) * 100}%` }} title={`URGENT ${URGENT}`} />
          <div className="bg-pending" style={{ width: `${(ROUTINE / total) * 100}%` }} title={`ROUTINE ${ROUTINE}`} />
          <div className="bg-ink-faint" style={{ width: `${(HELD / total) * 100}%` }} title={`HELD ${HELD}`} />
        </div>
        <MonoValue className="text-ink-dim">
          <span className="text-alert">U{URGENT}</span> / <span className="text-pending">R{ROUTINE}</span> /{' '}
          <span className="text-ink-faint">H{HELD}</span>
        </MonoValue>
      </div>
      <div className="flex items-center gap-1 text-ink-dim">
        last attempt
        <MonoValue>{status.last_attempt_at ? new Date(status.last_attempt_at).toLocaleTimeString() : '—'}</MonoValue>
      </div>
      <div className="flex items-center gap-1 text-ink-dim">
        last success
        <MonoValue>{status.last_success_at ? new Date(status.last_success_at).toLocaleTimeString() : '—'}</MonoValue>
      </div>
      <div className="flex items-center gap-1 text-ink-dim">
        failures
        <MonoValue className={status.consecutive_failures > 0 ? 'text-alert' : ''}>
          {status.consecutive_failures}
        </MonoValue>
      </div>
      <div className="flex items-center gap-1 text-ink-dim">
        last push
        <MonoValue>
          {status.last_push_bytes ? `${(status.last_push_bytes / 1024).toFixed(1)} KB` : '—'} /{' '}
          {status.last_push_duration_ms ?? '—'} ms / {status.last_push_mode ?? '—'}
        </MonoValue>
      </div>
    </div>
  )
}
