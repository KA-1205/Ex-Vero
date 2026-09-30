import { useState } from 'react'
import { queryDevice } from '../../api/client'
import type { QueryResult } from '../../types'
import { MonoValue, Panel } from '../primitives'

export function QAPanel({ deviceId }: { deviceId: string }) {
  const [question, setQuestion] = useState('')
  const [pending, setPending] = useState(false)
  const [history, setHistory] = useState<(QueryResult & { question: string })[]>([])

  async function submit() {
    if (!question.trim() || pending) return
    setPending(true)
    const q = question
    try {
      const result = await queryDevice(deviceId, q)
      setHistory((h) => [{ ...result, question: q }, ...h])
      setQuestion('')
    } catch (err) {
      console.error('Query failed', err)
    } finally {
      setPending(false)
    }
  }

  return (
    <Panel title="CROSS-MODAL SEARCH & Q&A" className="h-full">
      <div className="flex flex-col h-full min-h-0">
        <div className="p-2 border-b border-line flex gap-2">
          <input
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && submit()}
            placeholder="e.g. show me fire in server room A…"
            className="flex-1 bg-base-sunken border border-line px-2 py-1.5 text-xs outline-none focus-visible:border-good rounded"
          />
          <button
            onClick={submit}
            disabled={pending}
            className="px-3 py-1.5 text-xs font-mono border border-line hover:border-good hover:text-good disabled:opacity-50 rounded"
          >
            {pending ? 'SEARCHING…' : 'SEARCH'}
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-2 space-y-4">
          {history.length === 0 && (
            <div className="text-ink-faint text-xs font-mono py-12 text-center">
              Enter a text query to retrieve visual evidence & answer offline
            </div>
          )}

          {history.map((h, i) => {
            const visualResults = (h.results || []).filter(
              (r) => r.modality === 'vision' || r.thumbnail_url
            )
            const hasVisual = visualResults.length > 0

            return (
              <div key={i} className="border border-line p-3 bg-base-sunken/60 rounded-md space-y-2.5">
                {/* User Query Header */}
                <div className="flex items-center justify-between text-xs">
                  <div className="font-medium text-ink flex items-center gap-1.5 truncate">
                    <span className="text-ink-faint font-mono">Q:</span>
                    <span>"{h.question}"</span>
                  </div>
                  <MonoValue className="text-[10px] text-ink-faint shrink-0">
                    {h.latency_ms.toFixed(0)} ms
                  </MonoValue>
                </div>

                {/* Grounded Answer */}
                <div className="text-xs text-ink bg-white/40 p-2 rounded border border-line/60 backdrop-blur-sm">
                  <div className="text-[9px] font-mono uppercase tracking-wider text-ink-faint mb-1">
                    Grounded Answer
                  </div>
                  <div className="leading-relaxed">{h.answer || '—'}</div>
                </div>

                {/* Cross-Modal Matching Photos Grid (Task A6) */}
                {hasVisual && (
                  <div>
                    <div className="text-[10px] font-mono text-ink-faint uppercase tracking-wider mb-1.5 flex items-center justify-between">
                      <span>Matching Visual Memory ({visualResults.length})</span>
                      <span className="text-[9px]">Ranked by CLIP Cosine Similarity</span>
                    </div>

                    <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
                      {visualResults.map((r, rIdx) => {
                        const scorePct = r.score !== undefined ? Math.round(r.score * 100) : null
                        return (
                          <div
                            key={r.id || rIdx}
                            className="group relative rounded border border-line bg-black/5 overflow-hidden flex flex-col"
                          >
                            <div className="relative aspect-video bg-black/10 overflow-hidden">
                              {r.thumbnail_url ? (
                                <img
                                  src={r.thumbnail_url}
                                  alt={r.value}
                                  className="w-full h-full object-cover transition-transform group-hover:scale-105"
                                  loading="lazy"
                                />
                              ) : (
                                <div className="w-full h-full flex items-center justify-center text-[10px] font-mono text-ink-faint">
                                  [PHOTO]
                                </div>
                              )}
                              {scorePct !== null && (
                                <div className="absolute top-1 right-1 px-1 py-0.5 rounded bg-black/70 text-white font-mono text-[9px] font-bold">
                                  {scorePct}% match
                                </div>
                              )}
                            </div>
                            <div className="p-1.5 text-[10px] min-w-0">
                              <div className="truncate font-medium text-ink">
                                {r.value.replace(/^\[Photo captured:\s*/, '').replace(/\]$/, '') || r.corroboration_key}
                              </div>
                              <div className="text-[9px] font-mono text-ink-faint truncate mt-0.5">
                                {r.zone || r.corroboration_key}
                              </div>
                            </div>
                          </div>
                        )
                      })}
                    </div>
                  </div>
                )}

                {/* Sources / Metadata & Path Badge */}
                <div className="flex flex-wrap items-center justify-between gap-2 pt-1 border-t border-line/60 text-[10px]">
                  <span
                    className={`font-mono px-2 py-0.5 border rounded-sm font-medium ${
                      h.served_by === 'edge'
                        ? 'border-good text-good bg-good/10'
                        : 'border-pending text-pending bg-pending/10'
                    }`}
                  >
                    {h.served_by === 'edge' ? 'ANSWERED ON DEVICE (OFFLINE)' : 'ANSWERED VIA CLOUD'}
                  </span>

                  {h.model && (
                    <span className="font-mono text-ink-faint text-[9px]">
                      model: {h.model}
                    </span>
                  )}

                  {h.sources && h.sources.length > 0 && (
                    <span className="font-mono text-ink-faint text-[9px]">
                      {h.sources.length} sources cited
                    </span>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      </div>
    </Panel>
  )
}
