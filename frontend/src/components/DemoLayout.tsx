import { Outlet } from 'react-router-dom'
import { TopBar } from './TopBar'

export function DemoLayout() {
  return (
    <div className="min-h-screen flex flex-col">
      <TopBar />
      <main className="flex-1 min-h-0">
        <Outlet />
      </main>
    </div>
  )
}
