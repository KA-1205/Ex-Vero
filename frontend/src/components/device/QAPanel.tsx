import { useEffect, useState } from 'react'
import { queryDevice } from '../../api/client'
import type { QueryResult } from '../../types'
import { MonoValue, Panel } from '../primitives'

export function QAPanel({ deviceId }: { deviceId: string }) {
  const [question, setQuestion] = useState('')
  const [pending, setPending] = useState(false)
  const [stage, setStage] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [history, setHistory] = useState<(QueryResult & { question: string })[]>([])

  useEffect(() => {
    if (!pending) {
      setStage('')
      return
    }
    setStage('Querying local Qdrant Edge shard…')
    const t1 = setTimeout(() => setStage('Retrieving dense + BM25 matches…'), 1800)
    const t2 = setTimeout(() => setStage('Synthesizing answer with on-device LLM (qwen2.5:1.5b)…'), 4500)
    return () => { clearTimeout(t1); clearTimeout(t2) }
  }, [pending])

  async function submit() {
    if (!question.trim() || pending) return
    setPending(true)
    const q = question
    setError(null)
    try {
      const result = await queryDevice(deviceId, q)
      setHistory((h) => [{ ...result, question: q }, ...h])
      setQuestion('')
    } catch (e) { setError(e instanceof Error ? e.message : 'Query failed') }
    finally { setPending(false) }
  }

  return (
    <Panel title="ASK THIS DEVICE" className="ev-query-panel h-full">
      <div className="flex flex-col h-full">
        <div className="p-2 border-b border-line flex gap-2">
          <input
            aria-label="Question for this device"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && submit()}
            placeholder="Ask this device…"
            className="flex-1 bg-base-sunken border border-line px-2 py-1.5 text-sm outline-none focus-visible:border-good"
          />
          <button
            onClick={submit}
            disabled={pending}
            className="px-3 py-1.5 text-xs font-mono border border-line hover:border-good hover:text-good disabled:opacity-50"
          >
            {pending ? '…' : 'Ask'}
          </button>
        </div>
        <div className="flex-1 overflow-y-auto p-2 space-y-3">
          {pending && (
            <div className="border border-good/50 bg-good/5 p-3 rounded text-xs space-y-1.5 font-mono">
              <div className="flex items-center justify-between text-good">
                <span className="font-semibold flex items-center gap-2">
                  <span className="inline-block w-2 h-2 rounded-full bg-good animate-ping" />
                  {stage}
                </span>
                <span className="text-[10px] bg-good/20 text-good px-1.5 py-0.5 rounded font-bold">ON-DEVICE RAG</span>
              </div>
              <div className="text-[11px] text-ink-dim">
                Zero network call — answering from local vector store & edge weights.
              </div>
            </div>
          )}
          {history.length === 0 && !pending && (
            <div className="text-ink-faint text-xs font-mono py-6 text-center">
              no queries yet — ask something above
            </div>
          )}
          {history.map((h, i) => (
            <div key={i} className="border border-line p-2 bg-base-sunken">
              <div className="text-xs text-ink-dim mb-1">{h.question}</div>
              <div className="text-sm mb-2">{h.answer}</div>
              <div className="text-[10px] text-ink-faint font-mono mb-2">MODEL {h.model}</div>
              {h.sources.length > 0 && <div className="mb-2 border-t border-line pt-2 space-y-1">
                <div className="font-mono text-[10px] text-ink-faint">SOURCES</div>
                {h.sources.map((source) => <div key={source.id} className="text-[11px] text-ink-dim">
                  <span className="font-mono text-ink-faint">#{source.id} · {source.consensus_state} · {source.score.toFixed(4)}</span>
                  <div>{source.value}</div>
                </div>)}
              </div>}
              {h.results.length > 0 && <details className="mb-2 text-[11px] text-ink-dim"><summary className="cursor-pointer font-mono text-ink-faint">{h.results.length} RETRIEVED RESULTS</summary>{h.results.map((result) => <div key={result.id} className="border-t border-line py-1">#{result.id} · {result.value ?? result.payload?.value ?? ''} <span className="font-mono">{result.score?.toFixed(4)}</span></div>)}</details>}
              <div className="flex items-center justify-between text-[11px]">
                <span
                  className={`font-mono px-1.5 py-0.5 border ${
                    h.answer_path === 'offline' ? 'border-good text-good' : h.answer_path === 'online' ? 'border-pending text-pending' : 'border-line text-ink-dim'
                  }`}
                >
                  {h.answer_path.toUpperCase()} PATH
                </span>
                <MonoValue className="text-ink-dim">{h.latency_ms.toFixed(0)} ms</MonoValue>
              </div>
            </div>
          ))}
          {error && <div role="alert" className="text-xs text-alert font-mono">{error}</div>}
        </div>
      </div>
    </Panel>
  )
}
