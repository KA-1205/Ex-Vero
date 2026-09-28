import { Link, NavLink } from 'react-router-dom'
import { useNetworkMode } from '../context/NetworkModeContext'
import type { NetworkMode } from '../types'

const MODES: { mode: NetworkMode; label: string }[] = [
  { mode: 'offline', label: 'OFFLINE' },
  { mode: 'degraded', label: 'CELLULAR (DEGRADED)' },
  { mode: 'full', label: 'WI-FI (FULL)' },
]

const MODE_ACCENT: Record<NetworkMode, string> = {
  offline: 'border-alert text-alert',
  degraded: 'border-pending text-pending',
  full: 'border-good text-good',
}

const NAV = [
  { to: '/demo', label: 'Fleet Overview', end: true },
  { to: '/demo/conflict-theater', label: 'Conflict Theater' },
  { to: '/demo/command', label: 'Command Dashboard' },
]

export function TopBar() {
  const { mode, setMode } = useNetworkMode()

  return (
    <header className="border-b border-line bg-base-raised">
      <div className="flex items-center justify-between px-4 py-2 border-b border-line">
        <div className="flex items-center gap-3">
          <Link
            to="/"
            className="font-mono text-sm font-semibold tracking-wide text-ink hover:text-good transition-colors"
          >
            AEGIS EDGE
          </Link>
          <span className="text-[11px] text-ink-faint font-mono hidden sm:inline">
            edge memory kernel — command console
          </span>
        </div>
        <nav className="flex items-center gap-1">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                `px-2.5 py-1 text-xs font-mono border ${
                  isActive
                    ? 'border-ink text-ink bg-base-sunken'
                    : 'border-transparent text-ink-dim hover:text-ink hover:border-line'
                }`
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
      </div>
      <div className="flex items-center gap-2 px-4 py-2">
        <span className="text-[11px] text-ink-dim font-mono mr-1 shrink-0">NETWORK SIM</span>
        <div className="flex gap-1 flex-wrap">
          {MODES.map((m) => (
            <button
              key={m.mode}
              onClick={() => setMode(m.mode)}
              className={`px-3 py-1.5 text-xs font-mono border transition-colors ${
                mode === m.mode
                  ? `${MODE_ACCENT[m.mode]} bg-base-sunken`
                  : 'border-line text-ink-dim hover:text-ink hover:border-ink-faint'
              }`}
            >
              {m.label}
            </button>
          ))}
        </div>
      </div>
    </header>
  )
}