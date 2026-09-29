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
    <div className={`glass-card flex flex-col ${className}`}>
      {title && (
        <div className={`flex items-center justify-between border-b border-[#AABBC8] ${number ? 'min-h-[36px] px-0 py-0' : 'px-3 py-2'}`}>
          <div className="flex h-full min-w-0 items-stretch">
            {number && <span className="flex w-9 shrink-0 items-center justify-center font-mono text-[10px] text-ink">{number}</span>}
            {number && <span className="w-2 shrink-0 bg-[#234C7D]" />}
            <h2 className={`flex items-center text-xs font-semibold tracking-wide text-ink-dim ${number ? 'px-3' : ''}`}>{title}</h2>
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
      className={`font-mono text-[11px] px-1.5 py-0.5 border ${CONNECTIVITY_STYLES[state]} whitespace-nowrap`}
    >
      {state}
    </span>
  )
}

const VERDICT_STYLES: Record<string, string> = {
  KEEP_LOCAL: 'text-ink-dim border-line',
  QUEUE_LOW: 'text-pending border-pending',
  QUEUE_HIGH: 'text-alert border-alert',
  REJECT: 'text-alert border-alert',
  KEPT_LOCAL: 'text-ink-dim border-line',
  QUEUED: 'text-pending border-pending',
  SYNCED_NOW: 'text-good border-good',
  REJECTED: 'text-alert border-alert',
  REDACT_AND_QUEUE: 'text-pending border-pending',
}

export function VerdictBadge({ verdict }: { verdict: string }) {
  return (
    <span
      className={`font-mono text-[10px] px-1.5 py-0.5 border ${
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
