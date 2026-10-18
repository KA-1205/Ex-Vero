import { useEffect, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'

export function WorkspaceBar() {
  const { pathname } = useLocation()
  const image = pathname.startsWith('/image/')
  const [motion, setMotion] = useState(() => {
    try { return localStorage.getItem('ex-vero-motion') !== 'off' } catch { return true }
  })
  useEffect(() => {
    document.documentElement.dataset.evMotion = motion ? 'on' : 'off'
    try { localStorage.setItem('ex-vero-motion', motion ? 'on' : 'off') } catch { /* Preferences remain usable without storage. */ }
  }, [motion])
  const section = pathname.includes('/devices/') ? 'Device console' : pathname.endsWith('/devices') ? 'Devices' : pathname.endsWith('/conflict-theater') ? 'Conflict Theater' : pathname.endsWith('/command') ? 'Reports' : 'Overview'
  return <div className="ev-workspace-bar"><a className="ev-skip-link" href="#workspace-content">Skip to content</a><div className="ev-breadcrumb"><span>{image ? 'Image' : 'Numeric'} fleet</span><span aria-hidden="true">/</span><strong>{section}</strong></div><div className="ev-workspace-tools"><button type="button" className="ev-motion-toggle" aria-pressed={motion} onClick={() => setMotion(!motion)}>Motion {motion ? 'on' : 'off'}</button><div className="ev-workspace-switch" aria-label="Data workspace"><Link to="/numeric/overview" aria-current={!image ? 'page' : undefined}>Numeric</Link><Link to="/image/overview" aria-current={image ? 'page' : undefined}>Image</Link></div></div></div>
}
