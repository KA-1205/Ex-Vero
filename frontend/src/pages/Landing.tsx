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
    <header className="ev-landing-nav"><Link to="/" className="navbar-brand">EX–VERO <span>✧</span></Link><a href="#how-it-works">How it works</a><Link className="ev-button" to="/login">Open workspace ↗</Link></header>
    <main>
      <section className="ev-landing-hero"><div className="ev-landing-copy"><div className="ev-eyebrow">OFFLINE-FIRST / EDGE MEMORY</div><h1>Every signal.<br /><em>A clearer picture.</em></h1><p>An edge memory kernel that decides what is worth remembering, what is worth trusting, and what is worth sending to the cloud.</p><div className="ev-landing-actions"><Link to="/login" className="ev-button">Explore the console ↗</Link><a href="#how-it-works" className="ev-button ev-button-secondary">Follow a signal</a></div><div className="ev-landing-caption">NUMERIC SIGNALS · VISUAL EVIDENCE · EXPLAINABLE DECISIONS</div></div><div className="ev-landing-art"><FieldGraphic /><div className="ev-art-label">OBSERVE → REMEMBER → AGREE</div></div></section>
      <section className="ev-landing-steps" id="how-it-works" aria-label="How it works">{steps.map((step) => <article className="glass-card" key={step.number}><span className="ev-eyebrow">{step.number} / THE MEMORY LOOP</span><FieldGraphic kind={step.kind} /><h2>{step.title}</h2><p>{step.text}</p></article>)}</section>
      <section className="ev-workspace-intro"><div><div className="ev-eyebrow">TWO WAYS TO EXPLORE</div><h2>One kernel. Different perspectives.</h2><p>Follow sensor readings in Numeric, or inspect camera evidence in Image.</p></div><Link to="/login" className="ev-button ev-button-secondary">Choose a workspace ↗</Link></section>
    </main>
    <footer className="ev-landing-footer"><span>EX–VERO / EDGE MEMORY KERNEL</span><span>Selective memory. Shared understanding.</span></footer>
  </div>
}
