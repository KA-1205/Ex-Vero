import { useNetworkMode } from '../context/NetworkModeContext'
import { Sidebar } from '../../components/Sidebar'

export function TopBar() {
  const { mode, setMode } = useNetworkMode()
  return <Sidebar workspace='image' mode={mode} setMode={setMode} />
}
