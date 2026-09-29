import { Outlet } from 'react-router-dom'
import { TopBar } from './TopBar'
import { NetworkModeProvider as ImageNetworkModeProvider } from '../context/NetworkModeContext'

export function DemoLayout() {
  return (
    <ImageNetworkModeProvider>
      <div className="h-screen flex overflow-hidden">
        <TopBar />
        <main className="flex-1 min-w-0 min-h-0 overflow-y-auto">
          <Outlet />
        </main>
      </div>
    </ImageNetworkModeProvider>
  )
}
