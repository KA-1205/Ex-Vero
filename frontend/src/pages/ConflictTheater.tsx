import { FieldGraphic } from '../components/FieldGraphic'
import { useEffect, useState } from 'react'
import { useNetworkMode } from '../context/NetworkModeContext'
import { mockConsensusEvents, USE_MOCKS } from '../api/mock'
import { fetchDevices, injectConflict, mapConsensusEvent, setRogueMode, subscribeConsensusEvents } from '../api/client'
import type { ConsensusEvent, DeviceSummary } from '../types'
import { MonoValue, Panel } from '../components/primitives'

const DEVICE_A = { id: 'dev-01', name: 'Paramedic Tablet 01', value: 'Zone C — gas leak, active, evacuate', connectivity: 'ONLINE' as const }
const DEVICE_B = { id: 'dev-03', name: 'Field Pi Node 03', value: 'Zone C — gas leak, contained, monitor only', connectivity: 'ONLINE' as const }

function DeviceSide({ name, id, value, online }: { name: string; id: string; value: string; online: boolean }) {
  return (
    <div className="glass-card flex-1 p-3">
      <div className="flex items-center justify-between mb-2">
        <div>
          <div className="text-sm font-medium">{name}</div>
          <div className="text-[11px] text-ink-faint font-mono">{id}</div>
        </div>
        <span
          className={`font-mono text-[11px] px-1.5 py-0.5 border ${
            online ? 'border-good text-good' : 'border-alert text-alert'
          }`}
        >
          {online ? 'ONLINE' : 'OFFLINE'}
        </span>
      </div>
      <div className="rounded-md border border-[#C1CFCC] bg-white/25 p-2 text-sm backdrop-blur-sm">{value}</div>
    </div>
  )
}

export function ConflictTheater() {
  const { mode, setMode } = useNetworkMode()
  const [devices, setDevices] = useState<DeviceSummary[]>([])
  const [reconnected, setReconnected] = useState(false)
  const [resolved, setResolved] = useState<ConsensusEvent | null>(null)
  const [resolving, setResolving] = useState(false)
  const [rogue, setRogue] = useState(false)
  const [message, setMessage] = useState('')
  const [pendingConsensus, setPendingConsensus] = useState<ConsensusEvent | null>(null)

  useEffect(() => {
    void fetchDevices().then(setDevices).catch((error) => { console.error('Unable to load conflict devices', error); setMessage('The edge node is unavailable. Device data and resolution need a live connection.') })
    return subscribeConsensusEvents((event) => { setResolved(event); setResolving(false) })
  }, [])

  const deviceA = devices[0] ?? DEVICE_A
  const deviceB = devices.find((device) => device.id !== deviceA.id) ?? DEVICE_B

  const isDevAOnline = reconnected || (mode !== 'offline' && deviceA.connectivity !== 'OFFLINE')
  const isDevBOnline = reconnected || (mode !== 'offline' && deviceB.connectivity !== 'OFFLINE')

  async function reconnect() {
    setResolving(true)
    try {
      setMode('full')
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Reconnect failed')
      setResolving(false)
      return
    }
    setReconnected(true)
    if (pendingConsensus) {
      setResolved(pendingConsensus)
      setResolving(false)
      return
    }
    setResolving(false)
    if (USE_MOCKS) window.setTimeout(() => { setResolved(mockConsensusEvents()[0]); setResolving(false) }, 1200)
  }

  function reset() {
    setReconnected(false)
    setResolved(null)
    setPendingConsensus(null)
    setMode('offline')
  }

  async function inject() {
    setMessage('')
    try {
      const response = await injectConflict('zone_c.hazard', { [deviceA.id]: DEVICE_A.value, [deviceB.id]: DEVICE_B.value })
      if (response.consensus) setPendingConsensus(mapConsensusEvent(response.consensus))
      setMessage('Conflict injected. Click RECONNECT to resolve it.')
    }
    catch (error) { setMessage(error instanceof Error ? error.message : 'Conflict injection failed') }
  }

  async function toggleRogue() {
    const next = !rogue
    try { await setRogueMode(deviceB.id, next); setRogue(next); setMessage(next ? `${deviceB.id} set to rogue mode` : `${deviceB.id} rogue mode cleared`) }
    catch (error) { setMessage(error instanceof Error ? error.message : 'Rogue mode update failed') }
  }

  return (
    <div className="ev-page ev-conflict-page">
      <header className="ev-hero"><div className="ev-hero-copy"><div className="ev-eyebrow">CONFLICT THEATER / EVIDENCE COMPARISON</div><h1 className="ev-page-title">Two perspectives. One shared decision.</h1><p>Compare the sources, inspect their trust, and follow the resolution.</p></div><FieldGraphic kind="consensus" /></header>
      <div className="ev-conflict-toolbar flex flex-wrap items-center justify-between gap-4">
        <h1 className="text-sm font-mono text-ink-dim tracking-wide">CONFLICT THEATER</h1>
        <div className="flex flex-wrap gap-2">
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

      <div className="flex flex-wrap items-center gap-2 p-2.5 bg-base-sunken/60 border border-line rounded text-[11px] font-mono">
        <span className="font-bold text-good">DEMO SEQUENCE:</span>
        <span className={`px-2 py-0.5 rounded ${mode === 'offline' ? 'bg-alert/15 text-alert font-bold' : 'text-ink-dim'}`}>1. Disconnect (RESET)</span>
        <span className="text-ink-faint">→</span>
        <span className={`px-2 py-0.5 rounded ${pendingConsensus ? 'bg-pending/20 text-pending font-bold' : 'text-ink-dim'}`}>2. Inject Disagreement</span>
        <span className="text-ink-faint">→</span>
        <span className={`px-2 py-0.5 rounded ${resolved ? 'bg-good/20 text-good font-bold' : 'text-ink-dim'}`}>3. Reconnect & Bayesian Fold</span>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
        <DeviceSide name={deviceA.name} id={deviceA.id} value={DEVICE_A.value} online={isDevAOnline} />
        <DeviceSide name={deviceB.name} id={deviceB.id} value={DEVICE_B.value} online={isDevBOnline} />
      </div>
      {message && (
        <div role="status" className="text-xs font-mono text-ink-dim flex items-center justify-between border border-line bg-base-sunken/40 px-3 py-1.5 rounded">
          <span>{message}</span>
          {(message.toLowerCase().includes('failed') || message.toLowerCase().includes('error')) && (
            <button
              type="button"
              onClick={() => void reconnect()}
              className="underline text-ink hover:text-good ml-3 font-semibold cursor-pointer"
            >
              Retry Reconnect
            </button>
          )}
        </div>
      )}

      <Panel
        title="CONSENSUS FOLD RESULT"
        action={
          resolved?.corroboration_key ? (
            <span className="font-mono text-[10px] text-ink-dim px-2 py-0.5 border border-line bg-base-sunken rounded mr-2">
              KEY: {resolved.corroboration_key}
            </span>
          ) : undefined
        }
        className="flex-1"
      >
        <div className="p-4">
          {!resolved && !resolving && (
            <div className="text-ink-faint text-sm font-mono text-center py-10">
              {mode === 'offline'
                ? 'both devices offline, disagreeing on zone_c.hazard_status — press RECONNECT'
                : 'both devices online. Click RESET (GO OFFLINE) to simulate a partitioned dispute, or inject a conflict.'}
            </div>
          )}
          {resolving && (
            <div className="text-pending text-sm font-mono text-center py-10 animate-pulse">
              folding claims over corroboration_key = zone_c.hazard_status…
            </div>
          )}
          {resolved && (
            <div className="space-y-4">
              <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line/60 pb-3">
                <div className="flex items-center gap-3">
                  <span
                    className={`font-mono text-xs px-2.5 py-1 border font-semibold ${
                      resolved.outcome === 'CONFIRMED'
                        ? 'border-good text-good bg-good/10'
                        : resolved.outcome === 'DISPUTED'
                        ? 'border-alert text-alert bg-alert/10'
                        : 'border-pending text-pending bg-pending/10'
                    }`}
                  >
                    {resolved.outcome}
                  </span>
                  <span className="text-xs font-mono text-ink-dim">
                    {(resolved.confidence * 100).toFixed(0)}% confidence
                  </span>
                </div>
                <div className="flex items-center gap-2 w-48 sm:w-64">
                  <div className="h-2 flex-1 bg-base-sunken border border-line rounded-full overflow-hidden">
                    <div
                      className={`h-full transition-all duration-500 ${
                        resolved.confidence >= 0.8
                          ? 'bg-good'
                          : resolved.confidence >= 0.5
                          ? 'bg-pending'
                          : 'bg-alert'
                      }`}
                      style={{ width: `${Math.round(resolved.confidence * 100)}%` }}
                    />
                  </div>
                  <MonoValue className="text-xs font-mono text-ink-dim w-9 text-right">
                    {resolved.confidence.toFixed(2)}
                  </MonoValue>
                </div>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                {resolved.claims.map((c) => (
                  <div key={c.device_id} className="glass-card p-3 text-xs flex flex-col justify-between gap-2 border border-line/80">
                    <div>
                      <div className="flex items-center justify-between mb-1.5">
                        <span className="font-mono text-[11px] font-semibold text-ink-dim">{c.device_id}</span>
                        {c.reported_at && (
                          <span className="font-mono text-[10px] text-ink-faint">
                            {new Date(c.reported_at).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
                          </span>
                        )}
                      </div>
                      <div className="text-ink font-mono text-xs bg-base-sunken p-2 rounded border border-line/50">
                        {c.value}
                      </div>
                    </div>
                    <div className="pt-1.5 border-t border-line/30 space-y-1">
                      <div className="flex items-center justify-between text-ink-dim font-mono text-[11px]">
                        <span>trust weight</span>
                        <MonoValue className={c.trust_score >= 0.7 ? 'text-good font-semibold' : c.trust_score >= 0.5 ? 'text-pending font-semibold' : 'text-alert font-semibold'}>
                          {c.trust_score.toFixed(2)}
                        </MonoValue>
                      </div>
                      <div className="h-1.5 w-full bg-base-sunken rounded-full overflow-hidden border border-line/40">
                        <div
                          className={`h-full rounded-full transition-all duration-300 ${c.trust_score >= 0.7 ? 'bg-good' : c.trust_score >= 0.5 ? 'bg-pending' : 'bg-alert'}`}
                          style={{ width: `${Math.min(100, Math.max(8, c.trust_score * 100))}%` }}
                        />
                      </div>
                    </div>
                  </div>
                ))}
              </div>

              <div className="border-t border-line pt-3 text-sm text-ink-dim leading-relaxed">
                <span className="font-mono text-xs text-ink-faint block mb-1">RESOLUTION SUMMARY</span>
                {resolved.resolution_summary}
              </div>
            </div>
          )}
        </div>
      </Panel>
    </div>
  )
}
