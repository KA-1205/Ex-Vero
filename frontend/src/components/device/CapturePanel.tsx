import { useRef, useState } from 'react'
import { captureFact } from '../../api/client'
import type { CaptureResponse } from '../../types'
import { Panel } from '../primitives'

export function CapturePanel({ deviceId }: { deviceId: string }) {
  const [text, setText] = useState('')
  const [zone, setZone] = useState('')
  const [entity, setEntity] = useState('')
  const [corroborationKey, setCorroborationKey] = useState('')
  const [imagePreview, setImagePreview] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [justSubmitted, setJustSubmitted] = useState(false)
  const [file, setFile] = useState<File | null>(null)
  const [result, setResult] = useState<CaptureResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const fileInput = useRef<HTMLInputElement>(null)

  function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (!file) return
    setFile(file)
    const reader = new FileReader()
    reader.onload = () => setImagePreview(reader.result as string)
    reader.readAsDataURL(file)
  }

  async function submit() {
    if ((!text.trim() && !imagePreview) || !zone.trim() || !corroborationKey.trim()) return
    setSubmitting(true)
    setError(null)
    try {
      const response = await captureFact({ device_id: deviceId, value: text.trim(), corroboration_key: corroborationKey.trim(), zone: zone.trim(), entity: entity.trim() || undefined, reporter_device_id: deviceId, file: file ?? undefined, caption: text.trim() || undefined })
      setResult(response)
      setText(''); setImagePreview(null); setFile(null)
      if (fileInput.current) fileInput.current.value = ''
      setJustSubmitted(true)
      setTimeout(() => setJustSubmitted(false), 3000)
    } catch (e) { setError(e instanceof Error ? e.message : 'Capture failed') }
    finally { setSubmitting(false) }
  }

  return (
    <Panel title="CAPTURE — MULTIMODAL INPUT">
      <div className="p-3 space-y-3 max-w-xl">
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Describe what you observed…"
          rows={4}
          className="w-full bg-base-sunken border border-line px-2 py-1.5 text-sm outline-none focus-visible:border-good resize-none"
        />
        <div className="grid grid-cols-2 gap-2">
          <label className="space-y-1 text-[10px] font-mono text-ink-faint">ZONE<input value={zone} onChange={(e) => setZone(e.target.value)} placeholder="C" className="block w-full bg-base-sunken border border-line px-2 py-1 text-xs text-ink outline-none focus-visible:border-good" /></label>
          <label className="space-y-1 text-[10px] font-mono text-ink-faint">CORROBORATION KEY<input value={corroborationKey} onChange={(e) => setCorroborationKey(e.target.value)} placeholder="zone_c.hazard" className="block w-full bg-base-sunken border border-line px-2 py-1 text-xs text-ink outline-none focus-visible:border-good" /></label>
          <label className="col-span-2 space-y-1 text-[10px] font-mono text-ink-faint">ENTITY (OPTIONAL)<input value={entity} onChange={(e) => setEntity(e.target.value)} placeholder="hazard, supply, access…" className="block w-full bg-base-sunken border border-line px-2 py-1 text-xs text-ink outline-none focus-visible:border-good" /></label>
        </div>
        <div className="flex items-center gap-3">
          <input ref={fileInput} type="file" accept="image/*" onChange={onFile} className="hidden" id="capture-file" />
          <label
            htmlFor="capture-file"
            className="px-3 py-1.5 text-xs font-mono border border-line hover:border-ink-faint cursor-pointer"
          >
            ATTACH IMAGE
          </label>
          {imagePreview && (
            <img src={imagePreview} className="w-12 h-12 object-cover border border-line" />
          )}
        </div>
        <button
          onClick={submit}
          disabled={submitting || (!text.trim() && !imagePreview) || !zone.trim() || !corroborationKey.trim()}
          className="px-4 py-1.5 text-xs font-mono border border-good text-good hover:bg-good hover:text-base disabled:opacity-50"
        >
          {submitting ? 'SUBMITTING…' : 'SUBMIT FACT'}
        </button>
        {justSubmitted && (
          <div className="text-xs font-mono text-good">
            CAPTURED — decision feed updated
          </div>
        )}
        {result && <div className="border border-line bg-base-sunken p-2 text-xs space-y-1"><div><span className="font-mono text-ink-faint">VERDICT </span><strong>{result.verdict}</strong></div><div><span className="font-mono text-ink-faint">REASON </span>{result.reason}</div>{result.conflicts.map((conflict) => <div key={`${conflict.new_point_id}-${conflict.existing_point_id}`} className="border-t border-line pt-1"><span className="font-mono text-pending">POSSIBLE CONFLICT · {conflict.score.toFixed(4)}</span><div>{conflict.existing_value}</div></div>)}</div>}
        {error && <div role="alert" className="text-xs font-mono text-alert">{error}</div>}
      </div>
    </Panel>
  )
}
