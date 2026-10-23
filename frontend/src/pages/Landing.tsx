import { Link } from 'react-router-dom'
import { FieldGraphic } from '../components/FieldGraphic'
import './landing.css'

const steps = [
  { number: '01', kind: 'signal' as const, title: 'Observe with context.', text: 'Numeric signals and visual evidence, connected to where they came from.' },
  { number: '02', kind: 'memory' as const, title: 'Remember what matters.', text: 'Keep useful observations close to the edge, with their decision trail intact.' },
  { number: '03', kind: 'consensus' as const, title: 'Build shared understanding.', text: 'Compare independent reports and inspect how competing evidence is resolved.' },
]

export function Landing() {
  return <div className="ev-landing">
    <header className="ev-landing-nav">
      <Link to="/" className="navbar-brand">EX–VERO <span>✧</span></Link>
      <a href="#how-it-works">How it works</a>
      <div className="flex items-center gap-2">
        <Link className="ev-button" to="/numeric/overview">Numeric Fleet ↗</Link>
        <Link className="ev-button ev-button-secondary" to="/image/overview">Vision Fleet ↗</Link>
      </div>
    </header>
    <main>
      <section className="ev-landing-hero">
        <div className="ev-landing-copy">
          <div className="ev-eyebrow">OFFLINE-FIRST / EDGE MEMORY · BUILT ON QDRANT</div>
          <h1>Every signal.<br /><em>A clearer picture.</em></h1>
          <p>An offline-first edge memory platform that decides what is worth remembering, what is worth trusting, and what is worth sending to the cloud — powered by Qdrant Edge on device and Qdrant Server at the hub.</p>
          <div className="ev-landing-actions">
            <Link to="/numeric/overview" className="ev-button">Launch Numeric Fleet ↗</Link>
            <Link to="/image/overview" className="ev-button ev-button-secondary">Launch Vision Fleet ↗</Link>
          </div>
          <div className="ev-landing-caption">100% VECTOR-NATIVE · NO SQL · EXPLAINABLE EDGE TRIAGE · BAYESIAN CONSENSUS</div>
        </div>
        <div className="ev-landing-art">
          <FieldGraphic />
          <div className="ev-art-label">OBSERVE → REMEMBER → AGREE</div>
        </div>
      </section>
      <section className="ev-landing-steps" id="how-it-works" aria-label="How it works">
        {steps.map((step) => (
          <article className="glass-card" key={step.number}>
            <span className="ev-eyebrow">{step.number} / THE MEMORY LOOP</span>
            <FieldGraphic kind={step.kind} />
            <h2>{step.title}</h2>
            <p>{step.text}</p>
          </article>
        ))}
      </section>
      <section className="ev-workspace-intro">
        <div>
          <div className="ev-eyebrow">TWO WAYS TO EXPLORE</div>
          <h2>One kernel. Different perspectives.</h2>
          <p>Follow emergency sensor signals in Numeric, or inspect camera feeds and cross-modal search in Vision.</p>
        </div>
        <div className="flex gap-2">
          <Link to="/numeric/overview" className="ev-button">Open Numeric ↗</Link>
          <Link to="/image/overview" className="ev-button ev-button-secondary">Open Vision ↗</Link>
        </div>
      </section>
    </main>
    <footer className="ev-landing-footer">
      <span>EX–VERO / EDGE MEMORY KERNEL</span>
      <span>Selective memory. Shared understanding.</span>
    </footer>
  </div>
}
