import { useRef, useState } from 'react'
import { captureFact } from '../../api/client'
import { Panel } from '../primitives'

export function CapturePanel({ deviceId }: { deviceId: string }) {
  const [text, setText] = useState('')
  const [imagePreview, setImagePreview] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [justSubmitted, setJustSubmitted] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)

  function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (!file) return
    const reader = new FileReader()
    reader.onload = () => setImagePreview(reader.result as string)
    reader.readAsDataURL(file)
  }

  async function submit() {
    if (!text.trim() && !imagePreview) return
    setSubmitting(true)
    await captureFact({
      device_id: deviceId,
      modality: imagePreview ? 'vision' : 'text',
      text: text || undefined,
      image_data_url: imagePreview ?? undefined,
      corroboration_key: 'server_room_A.fire_status',
      zone: 'server_room_A',
    })
    setText('')
    setImagePreview(null)
    if (fileInput.current) fileInput.current.value = ''
    setSubmitting(false)
    setJustSubmitted(true)
    setTimeout(() => setJustSubmitted(false), 2000)
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
          disabled={submitting}
          className="px-4 py-1.5 text-xs font-mono border border-good text-good hover:bg-good hover:text-base disabled:opacity-50"
        >
          {submitting ? 'SUBMITTING…' : 'SUBMIT FACT'}
        </button>
        {justSubmitted && (
          <div className="text-xs font-mono text-good">
            captured — check the Live Decision Feed for the verdict
          </div>
        )}
      </div>
    </Panel>
  )
}
