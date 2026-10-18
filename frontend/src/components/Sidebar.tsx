import { useState } from 'react'
import { Link, NavLink } from 'react-router-dom'
import type { NetworkMode } from '../types'
import './sidebar.css'

const navigation = [
  { path: '', label: 'Overview', glyph: '◈' },
  { path: '/devices', label: 'Devices', glyph: '⬡' },
  { path: '/conflict-theater', label: 'Conflict Theater', glyph: '⇌' },
  { path: '/command', label: 'Reports', glyph: '▤' },
]

export function Sidebar({ workspace, mode, setMode }: { workspace: 'numeric' | 'image'; mode: NetworkMode; setMode: (mode: NetworkMode) => void }) {
  const [open, setOpen] = useState(false)
  const base = `/${workspace}/overview`
  return (
    <header className={`ev-sidebar ${open ? 'ev-sidebar-open' : ''}`}>
      <div className="ev-sidebar-brand"><Link to="/" className="navbar-brand">EX–VERO <span aria-hidden="true">✧</span></Link><p>EDGE MEMORY KERNEL</p><button type="button" className="ev-menu-toggle" aria-label="Toggle navigation" aria-expanded={open} aria-controls="workspace-navigation" onClick={() => setOpen(!open)}>{open ? 'Close' : 'Menu'}</button></div>
      <div className="ev-sidebar-body" id="workspace-navigation">
        <div className="ev-sidebar-caption">WORKSPACE / {workspace.toUpperCase()}</div>
        <nav aria-label={`${workspace} workspace`}>
          {navigation.map((item) => <NavLink key={item.path} to={base + item.path} end={!item.path} className={({ isActive }) => isActive ? 'ev-nav-active' : ''} onClick={() => setOpen(false)}><span aria-hidden="true">{item.glyph}</span>{item.label}</NavLink>)}
        </nav>
        <div className="ev-sidebar-bottom"><div className="ev-sidebar-caption">NETWORK SIMULATION</div><div className="ev-network-modes">{([{ mode: 'offline', label: 'Offline' }, { mode: 'degraded', label: 'Cellular' }, { mode: 'full', label: 'Wi-Fi' }] as const).map((item) => <button key={item.mode} type="button" aria-pressed={mode === item.mode} onClick={() => setMode(item.mode)}>{item.label}</button>)}</div><p className="ev-sidebar-note">Selective memory.<br />Shared understanding.</p></div>
      </div>
    </header>
  )
}
