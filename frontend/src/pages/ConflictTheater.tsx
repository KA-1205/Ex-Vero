import { useEffect, useState } from 'react'
import { useNetworkMode } from '../context/NetworkModeContext'
import { mockConsensusEvents, USE_MOCKS } from '../api/mock'
import { fetchDevices, injectConflict, setRogueMode, subscribeConsensusEvents } from '../api/client'
import type { ConsensusEvent, DeviceSummary } from '../types'
import { MonoValue, Panel } from '../components/primitives'

const DEVICE_A = { id: 'dev-01', name: 'Paramedic Tablet 01', value: 'Zone C — gas leak, active, evacuate' }
const DEVICE_B = { id: 'dev-03', name: 'Field Pi Node 03', value: 'Zone C — gas leak, contained, monitor only' }

function DeviceSide({ name, id, value, reconnected }: { name: string; id: string; value: string; reconnected: boolean }) {
  return (
    <div className="glass-card flex-1 p-3">
      <div className="flex items-center justify-between mb-2">
        <div>
          <div className="text-sm font-medium">{name}</div>
          <div className="text-[11px] text-ink-faint font-mono">{id}</div>
        </div>
        <span
          className={`font-mono text-[11px] px-1.5 py-0.5 border ${
            reconnected ? 'border-good text-good' : 'border-alert text-alert'
          }`}
        >
          {reconnected ? 'ONLINE' : 'OFFLINE'}
        </span>
      </div>
      <div className="rounded-md border border-[#AABBC8] bg-white/25 p-2 text-sm backdrop-blur-sm">{value}</div>
    </div>
  )
}

export function ConflictTheater() {
  const { setMode } = useNetworkMode()
  const [devices, setDevices] = useState<DeviceSummary[]>([])
  const [reconnected, setReconnected] = useState(false)
  const [resolved, setResolved] = useState<ConsensusEvent | null>(null)
  const [resolving, setResolving] = useState(false)
  const [rogue, setRogue] = useState(false)
  const [message, setMessage] = useState('')

  useEffect(() => {
    void fetchDevices().then(setDevices).catch((error) => console.error('Unable to load conflict devices', error))
    return subscribeConsensusEvents((event) => { setResolved(event); setResolving(false) })
  }, [])

  const deviceA = devices[0] ?? DEVICE_A
  const deviceB = devices.find((device) => device.id !== deviceA.id) ?? DEVICE_B

  async function reconnect() {
    setResolving(true)
    try { setMode('full') } catch (error) { setMessage(error instanceof Error ? error.message : 'Reconnect failed') }
    setReconnected(true)
    if (USE_MOCKS) window.setTimeout(() => { setResolved(mockConsensusEvents()[0]); setResolving(false) }, 1200)
  }

  function reset() {
    setReconnected(false)
    setResolved(null)
    setMode('offline')
  }

  async function inject() {
    setMessage('')
    try { await injectConflict('zone_c.hazard', { [deviceA.id]: DEVICE_A.value, [deviceB.id]: DEVICE_B.value }); setMessage('Conflict injected. Both devices hold different values.') }
    catch (error) { setMessage(error instanceof Error ? error.message : 'Conflict injection failed') }
  }

  async function toggleRogue() {
    const next = !rogue
    try { await setRogueMode(deviceB.id, next); setRogue(next); setMessage(next ? `${deviceB.id} set to rogue mode` : `${deviceB.id} rogue mode cleared`) }
    catch (error) { setMessage(error instanceof Error ? error.message : 'Rogue mode update failed') }
  }

  return (
    <div className="p-4 flex flex-col gap-4 h-full">
      <div className="flex items-center justify-between">
        <h1 className="text-sm font-mono text-ink-dim tracking-wide">CONFLICT THEATER</h1>
        <div className="flex gap-2">
          <button
            onClick={reset}
            className="px-3 py-1.5 text-xs font-mono border border-line text-ink-dim hover:text-ink"
          >
            RESET (GO OFFLINE)
          </button>
          <button onClick={inject} className="px-3 py-1.5 text-xs font-mono border border-line text-ink-dim hover:text-ink">INJECT CONFLICT</button>
          <button onClick={toggleRogue} className={`px-3 py-1.5 text-xs font-mono border ${rogue ? 'border-alert text-alert' : 'border-line text-ink-dim'}`}>{rogue ? 'ROGUE ON' : 'ROGUE DEVICE'}</button>
          <button
            onClick={reconnect}
            disabled={resolving}
            className="px-4 py-1.5 text-xs font-mono border border-good text-good hover:bg-good hover:text-base disabled:opacity-50"
          >
            {resolving ? 'RECONNECTING…' : 'RECONNECT'}
          </button>
        </div>
      </div>

      <div className="flex gap-3">
        <DeviceSide name={deviceA.name} id={deviceA.id} value={DEVICE_A.value} reconnected={reconnected} />
        <DeviceSide name={deviceB.name} id={deviceB.id} value={DEVICE_B.value} reconnected={reconnected} />
      </div>
      {message && <div role="status" className="text-xs font-mono text-ink-dim">{message}</div>}

      <Panel title="CONSENSUS FOLD RESULT" className="flex-1">
        <div className="p-4">
          {!resolved && !resolving && (
            <div className="text-ink-faint text-sm font-mono text-center py-10">
              both devices offline, disagreeing on zone_c.hazard_status — press RECONNECT
            </div>
          )}
          {resolving && (
            <div className="text-pending text-sm font-mono text-center py-10 animate-pulse">
              folding claims over corroboration_key = zone_c.hazard_status…
            </div>
          )}
          {resolved && (
            <div className="space-y-4">
              <div className="flex items-center gap-3">
                <span
                  className={`font-mono text-xs px-2 py-1 border ${
                    resolved.outcome === 'CONFIRMED'
                      ? 'border-good text-good'
                      : resolved.outcome === 'DISPUTED'
                      ? 'border-alert text-alert'
                      : 'border-pending text-pending'
                  }`}
                >
                  {resolved.outcome}
                </span>
                <MonoValue className="text-sm">confidence {resolved.confidence.toFixed(2)}</MonoValue>
              </div>
              <div className="grid grid-cols-2 gap-3">
                {resolved.claims.map((c) => (
                  <div key={c.device_id} className="glass-card p-2 text-xs">
                    <div className="font-mono text-ink-faint">{c.device_id}</div>
                    <div className="text-ink my-1">{c.value}</div>
                    <div className="text-ink-dim">
                      trust weight <MonoValue>{c.trust_score.toFixed(2)}</MonoValue>
                    </div>
                  </div>
                ))}
              </div>
              <div className="border-t border-line pt-3 text-sm text-ink-dim">
                {resolved.resolution_summary}
              </div>
            </div>
          )}
        </div>
      </Panel>
    </div>
  )
}
