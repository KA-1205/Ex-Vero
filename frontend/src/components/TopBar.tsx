import { useNetworkMode } from '../context/NetworkModeContext'
import { Sidebar } from './Sidebar'

export function TopBar() {
  const { mode, setMode } = useNetworkMode()
  return <Sidebar workspace="numeric" mode={mode} setMode={setMode} />
}
