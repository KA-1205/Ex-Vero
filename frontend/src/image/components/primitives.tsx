// Shared visual primitives for the light glass-console interface.
import type { ReactNode } from 'react'

export function Panel({
  title,
  number,
  action,
  children,
  className = '',
}: {
  title?: string
  number?: string
  action?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <div className={`glass-card ev-panel flex flex-col ${className}`}>
      {title && (
        <div className="ev-panel-heading">
          <div className="flex min-w-0 items-center gap-3">
            {number && <span className="ev-panel-number">{number}</span>}
            <h2 className="min-w-0 text-[13px] font-semibold text-ink">{title}</h2>
          </div>
          {action}
        </div>
      )}
      <div className="flex-1 min-h-0">{children}</div>
    </div>
  )
}

const CONNECTIVITY_STYLES: Record<string, string> = {
  ONLINE: 'text-good border-good',
  DEGRADED: 'text-pending border-pending',
  OFFLINE: 'text-alert border-alert',
}

export function ConnectivityPill({ state }: { state: 'ONLINE' | 'DEGRADED' | 'OFFLINE' }) {
  return (
    <span
      className={`ev-badge font-mono text-[11px] px-2 py-1 border ${CONNECTIVITY_STYLES[state]} whitespace-nowrap`}
    >
      {state}
    </span>
  )
}

const VERDICT_STYLES: Record<string, string> = {
  KEPT_LOCAL: 'text-ink-dim border-line',
  QUEUED: 'text-pending border-pending',
  SYNCED_NOW: 'text-good border-good',
  REJECTED: 'text-alert border-alert',
  REDACT_AND_QUEUE: 'text-pending border-pending',
}

export function VerdictBadge({ verdict }: { verdict: string }) {
  return (
    <span
      className={`ev-badge font-mono text-[11px] px-2 py-1 border ${
        VERDICT_STYLES[verdict] ?? 'text-ink-dim border-line'
      } whitespace-nowrap`}
    >
      {verdict.replace(/_/g, ' ')}
    </span>
  )
}

export function MonoValue({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <span className={`font-mono font-tabular ${className}`}>{children}</span>
}

export function StatLabel({ children }: { children: ReactNode }) {
  return <div className="text-[11px] text-ink-dim">{children}</div>
}

export function Divider() {
  return <div className="h-px bg-line w-full" />
}
