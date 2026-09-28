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
    const result = await queryDevice(deviceId, q)
    setHistory((h) => [{ ...result, question: q }, ...h])
    setQuestion('')
    setPending(false)
  }

  return (
    <Panel title="INSTANT Q&A" className="h-full">
      <div className="flex flex-col h-full">
        <div className="p-2 border-b border-line flex gap-2">
          <input
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
            {pending ? '…' : 'RUN'}
          </button>
        </div>
        <div className="flex-1 overflow-y-auto p-2 space-y-3">
          {history.length === 0 && (
            <div className="text-ink-faint text-xs font-mono py-6 text-center">
              no queries yet — ask something above
            </div>
          )}
          {history.map((h, i) => (
            <div key={i} className="border border-line p-2 bg-base-sunken">
              <div className="text-xs text-ink-dim mb-1">{h.question}</div>
              <div className="text-sm mb-2">{h.answer}</div>
              <div className="flex items-center justify-between text-[11px]">
                <span
                  className={`font-mono px-1.5 py-0.5 border ${
                    h.served_by === 'edge' ? 'border-good text-good' : 'border-pending text-pending'
                  }`}
                >
                  {h.served_by === 'edge' ? 'ANSWERED ON DEVICE' : 'ANSWERED VIA CLOUD'}
                </span>
                <MonoValue className="text-ink-dim">{h.latency_ms.toFixed(0)} ms</MonoValue>
              </div>
            </div>
          ))}
        </div>
      </div>
    </Panel>
  )
}
