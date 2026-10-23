import { FieldGraphic } from '../components/FieldGraphic'
import './access.css'
import { useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'

type DataMode = 'numeric' | 'image'

export function Login() {
  const navigate = useNavigate()
  const [userId, setUserId] = useState('')
  const [password, setPassword] = useState('')
  const [dataMode, setDataMode] = useState<DataMode | null>(null)
  const [message, setMessage] = useState('')

  const chooseMode = (mode: DataMode) => {
    const suffix = Math.floor(100 + Math.random() * 900)
    const prefix = mode === 'numeric' ? 'numeric' : 'image'
    setUserId(`${prefix}-operator-${suffix}`)
    setPassword(`${prefix === 'numeric' ? 'Field' : 'Vision'}Data#${suffix}`)
    setDataMode(mode)
    setMessage('')
  }

  const handleLogin = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (dataMode === 'numeric') {
      navigate('/numeric/overview')
      return
    }
    if (dataMode === 'image') {
      navigate('/image/overview')
      return
    }
    setMessage('Choose a data type below to fill the demo login details.')
  }

  return (
    <div className="ev-access-page relative isolate flex min-h-screen flex-col overflow-hidden text-ink">
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 z-0 bg-[url('/Contour-Map-navbar.svg')] bg-cover bg-[center_65%] opacity-[0.08]"
      />

      <header className="relative z-10 flex items-center justify-between border-b border-line px-4 py-2">
        <Link to="/" className="font-mono text-sm font-semibold tracking-wide text-ink">
          EX-VERO
        </Link>
        <span className="font-mono text-[11px] text-ink-faint">SECURE ACCESS</span>
      </header>

      <main className="ev-access-layout relative z-10">
        <div className="ev-access-intro"><div className="ev-eyebrow">EX–VERO / FIELD WORKSPACE</div><h2>Your evidence.<br />In perspective.</h2><p>Choose a workspace to follow the signals, inspect local memory, and explore the decision trail.</p><FieldGraphic /><span className="ev-eyebrow">NUMERIC SIGNALS / VISUAL EVIDENCE</span></div>
        <section className="glass-card ev-access-form">
          <p className="text-[10px] font-mono uppercase tracking-[0.2em] text-slate-500">FIELD COMMAND</p>
          <h1 className="ev-page-title mt-2">Enter your workspace</h1>
          <p className="mt-1 text-sm text-slate-700">Choose a data workspace to continue.</p>

          <form onSubmit={handleLogin}>
            <label htmlFor="login-user-id" className="mt-5 mb-1.5 block text-[11px] uppercase tracking-widest text-slate-500">USER ID</label>
            <input
              id="login-user-id"
              autoComplete="username"
              autoFocus
              value={userId}
              onChange={(event) => setUserId(event.target.value)}
              className="w-full h-10 px-3 rounded-md bg-slate-50 border-2 border-good ring-2 ring-good/20 outline-none text-sm placeholder:text-slate-400 focus:border-good focus:ring-2 focus:ring-good/20"
              placeholder="Operator / User ID"
              required
            />

            <label htmlFor="login-auth-key" className="mt-4 mb-1.5 block text-[11px] uppercase tracking-widest text-slate-500">AUTHENTICATION KEY</label>
            <input
              id="login-auth-key"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              className="w-full h-10 px-3 rounded-md bg-slate-50 border border-slate-300 text-sm focus:border-2 focus:border-good focus:ring-2 focus:ring-good/20 outline-none"
              placeholder="••••••••••"
              required
            />

            <button type="submit" className="mt-5 w-full h-10 inline-flex items-center justify-center gap-2 rounded-md bg-good hover:bg-[#1d5960] text-white text-sm font-bold uppercase tracking-wide">
              Open workspace
              <svg aria-label="Lock" role="img" viewBox="0 0 16 16" className="h-[14px] w-[14px] fill-current">
                <path d="M12 6h-1V4a3 3 0 0 0-6 0v2H4a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1V7a1 1 0 0 0-1-1ZM6.5 4a1.5 1.5 0 0 1 3 0v2h-3V4Zm2.25 6.72V12h-1.5v-1.28a1.5 1.5 0 1 1 1.5 0Z" />
              </svg>
            </button>
            <div className="mt-2.5 text-center">
              <button
                type="button"
                onClick={() => navigate('/numeric/overview')}
                className="text-[11px] font-mono text-slate-500 hover:text-good underline cursor-pointer"
              >
                Or skip login and launch demo directly →
              </button>
            </div>
          </form>

          <p aria-live="polite" className={`text-xs text-slate-600 ${message ? 'mt-1' : 'hidden'}`}>{message}</p>
          <div className="mt-3 border-t border-slate-200" />
          <p className="mb-3 text-[11px] uppercase tracking-widest text-slate-500">CHOOSE A DEMO WORKSPACE</p>

          <div className="grid grid-cols-2 gap-3">
            <div className="bg-white/55 border border-white/80 rounded-lg shadow-lg ring-1 ring-white/50 backdrop-blur-md p-3 flex flex-col gap-3">
              <div className="flex items-center gap-3">
                <span className="h-9 w-9 shrink-0 rounded-md bg-slate-100 flex items-center justify-center text-slate-800">
                  <svg aria-label="Numeric data chart" role="img" viewBox="0 0 24 24" className="h-[18px] w-[18px] fill-none stroke-current" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M4 20V4m0 16h17M8 16v-4m4 4V8m4 8v-6m4 6V5" />
                  </svg>
                </span>
                <span className="whitespace-nowrap text-xs font-medium uppercase tracking-wide text-slate-900">NUMERIC DATA</span>
              </div>
              <button type="button" aria-pressed={dataMode === 'numeric'} aria-label="Select Numeric workspace" onClick={() => chooseMode('numeric')} className="w-full py-1.5 rounded-md border border-slate-800 bg-white hover:bg-slate-50 text-[9px] font-medium uppercase tracking-wide text-slate-900">
                Select workspace
              </button>
            </div>

            <div className="bg-white/55 border border-white/80 rounded-lg shadow-lg ring-1 ring-white/50 backdrop-blur-md p-3 flex flex-col gap-3">
              <div className="flex items-center gap-3">
                <span className="h-9 w-9 shrink-0 rounded-md bg-slate-100 flex items-center justify-center text-slate-800">
                  <svg aria-label="Image data" role="img" viewBox="0 0 24 24" className="h-[18px] w-[18px] fill-none stroke-current" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
                    <rect x="3" y="5" width="14" height="14" rx="1.5" />
                    <path d="m5.5 16 3.5-4 2.5 2 2-2 2.5 4M17 9h3a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1h-9" />
                    <circle cx="8" cy="9" r="1" />
                  </svg>
                </span>
                <span className="text-xs font-medium uppercase tracking-wide text-slate-900">IMAGE DATA</span>
              </div>
              <button type="button" aria-pressed={dataMode === 'image'} aria-label="Select Image workspace" onClick={() => chooseMode('image')} className="w-full py-1.5 rounded-md border border-slate-800 bg-white hover:bg-slate-50 text-[9px] font-medium uppercase tracking-wide text-slate-900">
                Select workspace
              </button>
            </div>
          </div>

          <div className="my-2 border-t border-slate-200" />
          <footer className="flex items-center justify-center gap-2 text-center text-[11px] text-slate-700">
            <svg aria-label="Authorized use notice" role="img" viewBox="0 0 20 20" className="h-[14px] w-[14px] shrink-0 fill-none stroke-current" strokeWidth="1.7" strokeLinejoin="round">
              <path d="M10 2.5 18 17H2L10 2.5Z" />
              <path d="M10 7v4m0 2.5h.01" />
            </svg>
            <span>FOR AUTHORIZED USE ONLY. Unapproved access is a violation.</span>
          </footer>
        </section>
      </main>
    </div>
  )
}
