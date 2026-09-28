import { Link } from 'react-router-dom'
import { ConnectivityPill, MonoValue } from '../components/primitives'

// Placeholder landing page — built from the same design-system tokens and
// primitives as the rest of the app (base/ink/line/good/alert/pending,
// font-mono, ConnectivityPill, hairline borders, no shadows/gradients) so it
// doesn't fork the visual language. This will later be replaced with a
// 3D-flow-style "about" page; the button top-right into /demo should stay.
const CLAIMS = [
  'CONFIG-DRIVEN MODEL ADAPTER REGISTRY',
  'N-WAY TRUST-WEIGHTED CONSENSUS',
  'EVENT-SOURCED CLOUD MEMORY',
  'EXPLAINABLE DECISION FEED',
]

export function Landing() {
  return (
    <div className="min-h-screen flex flex-col bg-base text-ink">
      <header className="flex items-center justify-between px-4 py-2 border-b border-line">
        <div className="flex items-center gap-3">
          <span className="font-mono text-sm font-semibold tracking-wide text-ink">
            AEGIS EDGE
          </span>
          <span className="text-[11px] text-ink-faint font-mono hidden sm:inline">
            edge memory kernel
          </span>
        </div>
        <Link
          to="/demo"
          className="px-3 py-1.5 text-xs font-mono border border-good text-good hover:bg-good hover:text-base transition-colors"
        >
          VIEW DEMO
        </Link>
      </header>

      <main className="flex-1 flex flex-col items-center justify-center px-6 py-16 gap-10">
        <div className="flex items-center gap-2">
          <ConnectivityPill state="ONLINE" />
          <span className="text-[11px] font-mono text-ink-faint">4 devices in fleet · demo ready</span>
        </div>

        <div className="text-center max-w-2xl">
          <h1 className="font-mono text-5xl sm:text-7xl font-semibold tracking-tight text-ink">
            AEGIS EDGE
          </h1>
          <p className="mt-5 text-sm sm:text-base text-ink-dim font-sans leading-relaxed">
            An offline-first edge memory kernel that decides what's worth remembering,
            what's worth trusting, and what's worth sending to the cloud.
          </p>
        </div>

        <div className="flex flex-wrap justify-center gap-2 max-w-xl">
          {CLAIMS.map((c) => (
            <span
              key={c}
              className="border border-line px-2.5 py-1 text-[10px] font-mono text-ink-dim tracking-wide"
            >
              {c}
            </span>
          ))}
        </div>

        <div className="border border-line bg-base-raised px-5 py-3 flex items-center gap-6 text-xs font-mono">
          <span className="text-ink-faint">RESOLVER ACCURACY</span>
          <MonoValue className="text-good">94%</MonoValue>
          <span className="text-ink-faint">VS LWW BASELINE</span>
          <MonoValue className="text-alert">71%</MonoValue>
        </div>
      </main>

      <footer className="border-t border-line px-4 py-2 text-center">
        <span className="text-[11px] font-mono text-ink-faint">
          problem statement 03 (qdrant) — ai-powered edge memory &amp; intelligence platform
        </span>
      </footer>
    </div>
  )
}