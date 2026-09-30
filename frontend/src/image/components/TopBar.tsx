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
  { to: '/image/overview', label: 'Overview', end: true, icon: 'home' },
  { to: '/image/overview/devices', label: 'Devices', end: true, icon: 'devices' },
  { to: '/image/overview/conflict-theater', label: 'Conflict Theater', icon: 'danger' },
  { to: '/image/overview/command', label: 'Reports', icon: 'report' },
]

export function TopBar() {
  const { mode, setMode } = useNetworkMode()

  return (
    <header className="relative isolate w-52 shrink-0 min-h-screen overflow-hidden border-r border-[#163751] bg-[#040229] text-white flex flex-col">
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 z-0 bg-[url('/image-data/Contour-Map-navbar.svg')] bg-cover bg-[center_65%] opacity-[0.08]"
      />
      <div className="relative z-10 border-b border-[#163751]">
        <div className="px-4 pt-6 pb-4">
          <Link
            to="/"
            className="navbar-brand flex items-center justify-between font-serif text-[22px] leading-[1.05] tracking-tight text-[#F3F7FA]"
          >
            <span>EX-VERO</span>
            <svg aria-hidden="true" viewBox="0 0 32 32" className="w-8 h-8 text-[#DCE8F0] fill-current">
              <path d="M16 0c1.7 9.3 3.2 12.3 12 16-8.8 3.7-10.3 6.7-12 16C14.3 22.7 12.8 19.7 4 16 12.8 12.3 14.3 9.3 16 0Z" />
            </svg>
          </Link>
          <span className="mt-3 block max-w-[150px] text-[10px] leading-[1.7] tracking-[0.2em] text-[#9FB4C8] font-mono">
            REAL TIME INTELLIGENCE<br />SAFER TOMORROW
          </span>
        </div>
        <nav className="flex flex-col py-3">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              data-navbar-item="true"
              className={({ isActive }) =>
                `flex items-center gap-3 px-4 py-3 text-left text-xs font-mono border-l-2 border-y-0 border-r-0 ${
                  isActive
                    ? 'border-[#72B7EF] text-white bg-[#123B60]'
                    : 'border-transparent text-[#C2D0DC] hover:text-white hover:bg-[#0D2A47]'
                }`
              }
            >
              <NavIcon name={item.icon} />
              {item.label}
            </NavLink>
          ))}
        </nav>
      </div>
      <div className="relative z-10 mt-auto flex flex-col items-start gap-2 px-3 py-4 border-t border-[#163751]">
        <span className="text-[12px] text-[#9FB4C8] font-mono">NETWORK SIM</span>
        <div className="flex flex-col w-full gap-1">
          {MODES.map((m) => (
            <button
              key={m.mode}
              onClick={() => setMode(m.mode)}
              className={`px-2 py-2 text-left text-[10px] font-mono border transition-colors ${
                mode === m.mode
                  ? m.mode === 'full'
                    ? 'border-[#22C55E] bg-[#0D2A47] text-[#22C55E]'
                    : `${MODE_ACCENT[m.mode]} bg-[#0D2A47]`
                  : 'border-[#36516B] text-[#C2D0DC] hover:text-white hover:border-[#72B7EF]'
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

function NavIcon({ name }: { name: string }) {
  const common = 'w-[18px] h-[18px] shrink-0'

  if (name === 'home') {
    return (
      <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" className={common}>
        <path d="m3 10 9-7 9 7v10a1 1 0 0 1-1 1h-6v-7h-4v7H4a1 1 0 0 1-1-1V10Z" />
      </svg>
    )
  }

  if (name === 'danger') {
    return (
      <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" className={common}>
        <path d="M12 3 2.8 20h18.4L12 3Z" />
        <path d="M12 9v5m0 3h.01" />
      </svg>
    )
  }

  if (name === 'devices') {
    return (
      <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" className={common}>
        <path d="M4 7.5 12 3l8 4.5v9L12 21l-8-4.5v-9Z" />
        <path d="m4.5 7.8 7.5 4.3 7.5-4.3M12 12v8.5" />
      </svg>
    )
  }

  return (
    <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" className={common}>
      <path d="M6 2.8h8l4 4V21H6a2 2 0 0 1-2-2V4.8a2 2 0 0 1 2-2Z" />
      <path d="M14 3v5h5M8 12h8M8 16h8" />
    </svg>
  )
}
