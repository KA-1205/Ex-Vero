import { Outlet } from 'react-router-dom'
import { TopBar } from './TopBar'

export function DemoLayout() {
  return (
    <div className="h-screen flex overflow-hidden">
      <TopBar />
      <main className="flex-1 min-w-0 min-h-0 overflow-y-auto">
        <Outlet />
      </main>
    </div>
  )
}
