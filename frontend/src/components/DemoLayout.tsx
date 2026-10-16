import { Outlet } from 'react-router-dom'
import { TopBar } from './TopBar'
import { DemoGuide } from './DemoGuide'

export function DemoLayout() {
  return (
    <div className="h-screen flex overflow-hidden">
      <TopBar />
      <div className="flex-1 min-w-0 min-h-0 flex flex-col overflow-hidden">
        <DemoGuide />
        <main className="flex-1 min-w-0 min-h-0 overflow-y-auto">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
