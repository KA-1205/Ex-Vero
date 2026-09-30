import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react'
import type { NetworkMode } from '../types'
import { fetchNetworkMode, setNetworkMode } from '../api/client'

interface NetworkModeContextValue {
  mode: NetworkMode
  setMode: (mode: NetworkMode) => void
}

const NetworkModeContext = createContext<NetworkModeContextValue | null>(null)

export function NetworkModeProvider({ children }: { children: ReactNode }) {
  const [mode, setModeState] = useState<NetworkMode>('full')

  useEffect(() => { void fetchNetworkMode().then(setModeState).catch((error) => console.error('Unable to load network mode', error)) }, [])

  const setMode = useCallback((next: NetworkMode) => {
    const previous = mode
    setModeState(next)
    void setNetworkMode(next).catch((error) => { setModeState(previous); console.error('Unable to change network mode', error) })
  }, [mode])

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
