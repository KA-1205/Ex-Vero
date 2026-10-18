import { WorkspaceBar } from '../../components/WorkspaceBar'
import { Outlet } from 'react-router-dom'
import { TopBar } from './TopBar'
import { NetworkModeProvider as ImageNetworkModeProvider } from '../context/NetworkModeContext'

export function DemoLayout() {
  return (
    <ImageNetworkModeProvider>
      <div className="ev-shell">
        <TopBar />
        <div className="ev-workspace"><WorkspaceBar /><main id="workspace-content" className="ev-workspace-content">
          <Outlet />
        </main></div>
      </div>
    </ImageNetworkModeProvider>
  )
}
