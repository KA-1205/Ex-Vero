import { WorkspaceBar } from './WorkspaceBar'
import { Outlet } from 'react-router-dom'
import { TopBar } from './TopBar'

export function DemoLayout() {
  return (
    <div className="ev-shell">
      <TopBar />
      <div className="ev-workspace"><WorkspaceBar /><main id="workspace-content" className="ev-workspace-content">
        <Outlet />
      </main></div>
    </div>
  )
}
