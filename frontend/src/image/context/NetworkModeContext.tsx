import { createContext, useCallback, useContext, useState, type ReactNode } from 'react'
import type { NetworkMode } from '../types'
import { setNetworkMode } from '../api/client'

interface NetworkModeContextValue {
  mode: NetworkMode
  setMode: (mode: NetworkMode) => void
}

const NetworkModeContext = createContext<NetworkModeContextValue | null>(null)

export function NetworkModeProvider({ children }: { children: ReactNode }) {
  const [mode, setModeState] = useState<NetworkMode>('full')

  const setMode = useCallback((next: NetworkMode) => {
    setModeState(next)
    void setNetworkMode(next)
  }, [])

  return (
    <NetworkModeContext.Provider value={{ mode, setMode }}>
      {children}
    </NetworkModeContext.Provider>
  )
}

export function useNetworkMode() {
  const ctx = useContext(NetworkModeContext)
  if (!ctx) throw new Error('useNetworkMode must be used within NetworkModeProvider')
  return ctx
}
