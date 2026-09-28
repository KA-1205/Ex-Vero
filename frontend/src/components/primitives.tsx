// Shared visual primitives implementing the design direction in frontend.md §2:
// sharp corners, hairline borders, no shadows, no gradients, mono for data.
import type { ReactNode } from 'react'

export function Panel({
  title,
  action,
  children,
  className = '',
}: {
  title?: string
  action?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <div className={`border border-line bg-base-raised flex flex-col ${className}`}>
      {title && (
        <div className="flex items-center justify-between border-b border-line px-3 py-2">
          <h2 className="text-xs font-semibold tracking-wide text-ink-dim">{title}</h2>
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
