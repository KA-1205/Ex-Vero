import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { NetworkModeProvider } from './context/NetworkModeContext'
import { DemoLayout } from './components/DemoLayout'
import { Landing } from './pages/Landing'
import { OverviewDashboard } from './pages/OverviewDashboard'
import { DevicesPage } from './pages/DevicesPage'
import { DeviceConsole } from './pages/DeviceConsole'
import { ConflictTheater } from './pages/ConflictTheater'
import { CommandDashboard } from './pages/CommandDashboard'

export default function App() {
  return (
    <NetworkModeProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<Landing />} />
          <Route path="/overview" element={<DemoLayout />}>
            <Route index element={<OverviewDashboard />} />
            <Route path="devices" element={<DevicesPage />} />
            <Route path="devices/:deviceId" element={<DeviceConsole />} />
            <Route path="conflict-theater" element={<ConflictTheater />} />
            <Route path="command" element={<CommandDashboard />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </NetworkModeProvider>
  )
}
