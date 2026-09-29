import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { NetworkModeProvider } from './context/NetworkModeContext'
import { DemoLayout } from './components/DemoLayout'
import { Landing } from './pages/Landing'
import { Login } from './pages/Login'
import { OverviewDashboard } from './pages/OverviewDashboard'
import { DevicesPage } from './pages/DevicesPage'
import { DeviceConsole } from './pages/DeviceConsole'
import { ConflictTheater } from './pages/ConflictTheater'
import { CommandDashboard } from './pages/CommandDashboard'
import { DemoLayout as ImageDemoLayout } from './image/components/DemoLayout'
import { OverviewDashboard as ImageOverviewDashboard } from './image/pages/OverviewDashboard'
import { DevicesPage as ImageDevicesPage } from './image/pages/DevicesPage'
import { DeviceConsole as ImageDeviceConsole } from './image/pages/DeviceConsole'
import { ConflictTheater as ImageConflictTheater } from './image/pages/ConflictTheater'
import { CommandDashboard as ImageCommandDashboard } from './image/pages/CommandDashboard'

export default function App() {
  return (
    <NetworkModeProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<Landing />} />
          <Route path="/login" element={<Login />} />
          <Route path="/numeric">
            <Route path="overview" element={<DemoLayout />}>
              <Route index element={<OverviewDashboard />} />
              <Route path="devices" element={<DevicesPage />} />
              <Route path="devices/:deviceId" element={<DeviceConsole />} />
              <Route path="conflict-theater" element={<ConflictTheater />} />
              <Route path="command" element={<CommandDashboard />} />
            </Route>
          </Route>
          <Route path="/image">
            <Route path="overview" element={<ImageDemoLayout />}>
              <Route index element={<ImageOverviewDashboard />} />
              <Route path="devices" element={<ImageDevicesPage />} />
              <Route path="devices/:deviceId" element={<ImageDeviceConsole />} />
              <Route path="conflict-theater" element={<ImageConflictTheater />} />
              <Route path="command" element={<ImageCommandDashboard />} />
            </Route>
          </Route>
        </Routes>
      </BrowserRouter>
    </NetworkModeProvider>
  )
}
