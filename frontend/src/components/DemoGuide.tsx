import { useState } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'

interface Step {
  title: string
  path: string
  description: string
  badge: string
}

const STEPS: Step[] = [
  {
    title: 'Fleet Overview',
    path: '/numeric/overview',
    description: 'Monitor real-time node connectivity, local weather conditions, and aggregate system performance across the edge mesh.',
    badge: '01 / 04',
  },
  {
    title: 'Device Inspection',
    path: '/numeric/overview/devices',
    description: 'Drill down into individual edge nodes to inspect local vector memories, Q&A reasoning, and offline capture logs.',
    badge: '02 / 04',
  },
  {
    title: 'Conflict Theater',
    path: '/numeric/overview/conflict-theater',
    description: 'Partition the network, inject diverging facts across nodes, and observe trust-weighted consensus fold upon reconnect.',
    badge: '03 / 04',
  },
  {
    title: 'Command Center',
    path: '/numeric/overview/command',
    description: 'Review the merged cloud truth, verified consensus outcomes, and compare hybrid recall against dense-only baselines.',
    badge: '04 / 04',
  },
]

export function DemoGuide() {
  const [open, setOpen] = useState(() => {
    return typeof window !== 'undefined' ? !sessionStorage.getItem('exvero_demo_guide_dismissed') : false
  })
  const navigate = useNavigate()
  const location = useLocation()

  const [selectedStep, setSelectedStep] = useState<number | null>(null)

  const activeStepIndex = STEPS.findIndex((s) => location.pathname === s.path || location.pathname.startsWith(`${s.path}/`))
  const currentStepIndex = selectedStep ?? (activeStepIndex !== -1 ? activeStepIndex : 0)
  const step = STEPS[currentStepIndex]

  function next() {
    if (currentStepIndex < STEPS.length - 1) {
      const nextIndex = currentStepIndex + 1
      setSelectedStep(nextIndex)
      navigate(STEPS[nextIndex].path)
    }
  }

  function prev() {
    if (currentStepIndex > 0) {
      const prevIndex = currentStepIndex - 1
      setSelectedStep(prevIndex)
      navigate(STEPS[prevIndex].path)
    }
  }

  function dismiss() {
    setOpen(false)
    sessionStorage.setItem('exvero_demo_guide_dismissed', 'true')
  }

  if (!open) {
    return (
      <div className="fixed bottom-4 right-4 z-50">
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="flex items-center gap-2 px-3 py-1.5 bg-[#0D2A47] text-white border border-[#315C86] shadow-lg rounded text-xs font-mono hover:bg-[#123B60] transition-colors cursor-pointer"
          title="Open interactive walkthrough"
        >
          <span className="w-2 h-2 rounded-full bg-good animate-pulse" />
          <span>GUIDED TOUR</span>
        </button>
      </div>
    )
  }

  return (
    <div className="bg-[#0B1E32]/95 text-white border-b border-[#234C7D] px-4 py-2.5 flex items-center justify-between gap-4 z-40 backdrop-blur-md shadow-sm">
      <div className="flex items-center gap-3 min-w-0">
        <span className="shrink-0 font-mono text-[10px] tracking-wider px-2 py-0.5 bg-[#163751] text-[#72B7EF] border border-[#315C86] rounded">
          {step.badge}
        </span>
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-mono text-xs font-semibold tracking-wide text-white">{step.title}</span>
            <span className="text-[11px] text-[#AABBC8] hidden md:inline">— {step.description}</span>
          </div>
          <div className="text-[11px] text-[#AABBC8] md:hidden truncate">{step.description}</div>
        </div>
      </div>

      <div className="flex items-center gap-2 shrink-0">
        <button
          type="button"
          onClick={() => navigate(step.path)}
          className="px-2.5 py-1 text-[11px] font-mono border border-[#315C86] hover:bg-[#163751] rounded text-[#E1EBEB]"
        >
          View Page
        </button>
        <button
          type="button"
          onClick={prev}
          disabled={currentStepIndex === 0}
          className="px-2 py-1 text-[11px] font-mono border border-[#315C86] hover:bg-[#163751] rounded text-[#E1EBEB] disabled:opacity-40 disabled:hover:bg-transparent"
        >
          Prev
        </button>
        <button
          type="button"
          onClick={next}
          disabled={currentStepIndex === STEPS.length - 1}
          className="px-2.5 py-1 text-[11px] font-mono border border-good text-good bg-good/10 hover:bg-good hover:text-base rounded disabled:opacity-40 disabled:hover:bg-transparent"
        >
          Next
        </button>
        <button
          type="button"
          onClick={dismiss}
          className="ml-1 text-[#AABBC8] hover:text-white text-xs px-1.5 py-1 font-mono"
          title="Dismiss walkthrough"
        >
          ✕
        </button>
      </div>
    </div>
  )
}
