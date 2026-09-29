import { useEffect, useState } from 'react'
import { fetchCloudDashboard, fetchMemoryRecords, fetchRecallBenchmark, fetchResolverBenchmark, retractPoint } from '../api/client'
import { USE_MOCKS } from '../api/mock'
import type { CloudFact } from '../types'
import { MonoValue, Panel } from '../components/primitives'

export function CommandDashboard() {
  const [facts, setFacts] = useState<CloudFact[]>([])
  const [deviceTrust, setDeviceTrust] = useState<Record<string, number>>({})
  const [resolver, setResolver] = useState<{ resolver_accuracy: number; lww_accuracy: number; scenarios: number } | null>(null)
  const [recall, setRecall] = useState<{ dense_recall_at_5: number; hybrid_recall_at_5: number; labeled_queries: number } | null>(null)
  const [busyFact, setBusyFact] = useState<string | null>(null)
  const [retracted, setRetracted] = useState<string[]>([])
  const [actionError, setActionError] = useState<string | null>(null)

  useEffect(() => {
    const refresh = () => fetchCloudDashboard().then((state) => { setFacts(state.facts); setDeviceTrust(state.device_trust) }).catch((error) => console.error('Unable to load cloud state', error))
    void refresh()
    void fetchResolverBenchmark().then(setResolver).catch((error) => console.error('Unable to load resolver benchmark', error))
    void fetchRecallBenchmark().then(setRecall).catch((error) => console.error('Unable to load recall benchmark', error))
    const id = setInterval(() => void refresh(), 8000)
    return () => clearInterval(id)
  }, [])

  const visibleFacts = facts.filter((fact) => !retracted.includes(fact.corroboration_key))
  const confirmed = visibleFacts.filter((f) => f.status === 'CONFIRMED')
  const disputed = visibleFacts.filter((f) => f.status === 'DISPUTED')

  async function retractFact(fact: CloudFact) {
    setBusyFact(fact.corroboration_key); setActionError(null)
    try {
      if (USE_MOCKS) {
        setRetracted((items) => [...items, fact.corroboration_key])
        return
      }
      const memories = await Promise.all(fact.corroborating_devices.map((deviceId) => fetchMemoryRecords(deviceId, { q: fact.summary, limit: 100 }).then((rows) => ({ deviceId, rows }))))
      const source = memories.flatMap(({ deviceId, rows }) => rows.filter((row) => row.corroboration_key === fact.corroboration_key && row.content_preview === fact.summary).map((row) => ({ deviceId, row })))[0]
      if (!source) throw new Error('No source memory point was found for this cloud fact.')
      await retractPoint(source.deviceId, source.row.id)
      const latest = await fetchCloudDashboard()
      setFacts(latest.facts); setDeviceTrust(latest.device_trust)
    } catch (error) { setActionError(error instanceof Error ? error.message : 'Retraction failed') }
    finally { setBusyFact(null) }
  }

  return (
    <div className="p-4 flex flex-col gap-3 h-full">
      <div className="flex items-center gap-4 text-xs font-mono">
        <span className="text-ink-dim">TRUSTED FACTS</span>
        <MonoValue className="text-good">{confirmed.length} confirmed</MonoValue>
        <MonoValue className="text-alert">{disputed.length} disputed</MonoValue>
      </div>
      <div className="grid grid-cols-2 gap-2 text-[11px] font-mono xl:grid-cols-4">
        <span className="glass-card px-2 py-1 text-ink-dim">{USE_MOCKS ? 'DEMO · ' : ''}RESOLVER <MonoValue className="text-good">{resolver ? `${(resolver.resolver_accuracy * 100).toFixed(0)}%` : '—'}</MonoValue></span>
        <span className="glass-card px-2 py-1 text-ink-dim">LWW BASELINE <MonoValue className="text-alert">{resolver ? `${(resolver.lww_accuracy * 100).toFixed(0)}%` : '—'}</MonoValue> · {resolver?.scenarios ?? '—'} cases</span>
        <span className="glass-card px-2 py-1 text-ink-dim">DENSE R@5 <MonoValue>{recall ? `${(recall.dense_recall_at_5 * 100).toFixed(0)}%` : '—'}</MonoValue> · {recall?.labeled_queries ?? '—'} queries</span>
        <span className="glass-card px-2 py-1 text-ink-dim">HYBRID R@5 <MonoValue className="text-good">{recall ? `${(recall.hybrid_recall_at_5 * 100).toFixed(0)}%` : '—'}</MonoValue></span>
      </div>
      <div className="flex flex-wrap gap-3 text-[11px] font-mono">{Object.entries(deviceTrust).map(([id, trust]) => <span key={id} className="border border-line px-2 py-1 text-ink-dim">{id} TRUST <MonoValue className={trust < 0.5 ? 'text-alert' : 'text-good'}>{trust.toFixed(2)}</MonoValue></span>)}</div>
      <Panel title="MERGED TRUSTED PICTURE — CLOUD VIEW" className="flex-1">
        <div className="divide-y divide-line">
          {visibleFacts.map((f) => (
            <div key={f.id} className="p-3 flex items-start justify-between gap-4">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className="text-[11px] font-mono text-ink-faint">{f.zone}</span>
                  <span
                    className={`font-mono text-[10px] px-1.5 py-0.5 border ${
                      f.status === 'CONFIRMED' ? 'border-good text-good' : 'border-alert text-alert'
                    }`}
                  >
                    {f.status}
                  </span>
                </div>
                <div className="text-sm mt-1">{f.summary}</div>
                <div className="text-[11px] text-ink-faint font-mono mt-1">
                  corroborated by {f.corroborating_devices.join(', ')}
                </div>
              </div>
              <div className="text-right shrink-0 flex flex-col items-end gap-2">
                <MonoValue className="text-sm">{f.confidence.toFixed(2)}</MonoValue>
                <div className="text-[11px] text-ink-faint">
                  {new Date(f.last_updated).toLocaleTimeString()}
                </div>
                <button onClick={() => void retractFact(f)} disabled={busyFact === f.corroboration_key} className="border border-alert px-2 py-1 font-mono text-[9px] text-alert disabled:opacity-50">{busyFact === f.corroboration_key ? 'RETRACTING…' : USE_MOCKS ? 'SIMULATE RETRACTION' : 'RETRACT'}</button>
              </div>
            </div>
          ))}
          {visibleFacts.length === 0 && (
            <div className="text-ink-faint text-xs font-mono py-8 text-center">
              no trusted facts yet — waiting on consensus fold
            </div>
          )}
        </div>
      </Panel>
      {USE_MOCKS && <div className="font-mono text-[10px] text-ink-faint">DEMO DATA · benchmark and retraction actions are simulated</div>}
      {actionError && <div role="alert" className="text-xs font-mono text-alert">{actionError}</div>}
    </div>
  )
}
