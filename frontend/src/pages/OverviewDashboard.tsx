import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { fetchActivity, fetchCloudState, fetchDevices, fetchSyncStatus, fetchTelemetry, fetchWeather } from '../api/client'
import type { ActivityEntry, CloudFact, DeviceSummary, SyncStatus, TelemetrySample, WeatherConditions } from '../types'
import { ConnectivityPill, MonoValue, Panel } from '../components/primitives'
import { FleetMap } from '../components/FleetMap'

type DeviceTelemetry = { deviceId: string; sample: TelemetrySample }

export function OverviewDashboard() {
  const [devices, setDevices] = useState<DeviceSummary[] | null>(null)
  const [facts, setFacts] = useState<CloudFact[]>([])
  const [activity, setActivity] = useState<ActivityEntry[]>([])
  const [, setSync] = useState<SyncStatus[]>([])
  const [telemetry, setTelemetry] = useState<DeviceTelemetry[]>([])
  const [weather, setWeather] = useState<WeatherConditions | null>(null)
  const navigate = useNavigate()

  useEffect(() => {
    let active = true
    const refresh = async () => {
      try {
        const fleet = await fetchDevices()
        if (!active) return
        setDevices(fleet)
        const [cloud, ...perDevice] = await Promise.all([
          fetchCloudState().catch(() => [] as CloudFact[]),
          ...fleet.map(async (device) => {
            const [events, status, samples] = await Promise.all([
              fetchActivity(device.id).catch(() => [] as ActivityEntry[]),
              fetchSyncStatus(device.id).catch(() => null),
              fetchTelemetry(device.id).catch(() => [] as TelemetrySample[]),
            ])
            return { events, status, samples: samples.map((sample) => ({ deviceId: device.id, sample })) }
          }),
        ])
        if (!active) return
        setFacts(cloud)
        setActivity(perDevice.flatMap((result) => result.events).sort((a, b) => Date.parse(b.timestamp) - Date.parse(a.timestamp)))
        setSync(perDevice.flatMap((result) => result.status ? [result.status] : []))
        setTelemetry(perDevice.flatMap((result) => result.samples).sort((a, b) => Date.parse(a.sample.timestamp) - Date.parse(b.sample.timestamp)))
      } catch {
        if (active) setDevices([])
      }
    }
    void refresh()
    const timer = window.setInterval(() => void refresh(), 15000)
    return () => { active = false; window.clearInterval(timer) }
  }, [])

  useEffect(() => {
    let active = true
    const refreshWeather = async () => {
      try {
        const current = await fetchWeather()
        if (active) setWeather(current)
      } catch {
        if (active) setWeather(null)
      }
    }
    void refreshWeather()
    const timer = window.setInterval(() => void refreshWeather(), 10 * 60 * 1000)
    return () => { active = false; window.clearInterval(timer) }
  }, [])

  const total = devices?.length ?? 0
  const online = devices?.filter((device) => device.connectivity === 'ONLINE').length ?? 0
  const degraded = devices?.filter((device) => device.connectivity === 'DEGRADED').length ?? 0
  const offline = devices?.filter((device) => device.connectivity === 'OFFLINE').length ?? 0
  const disputed = facts.filter((fact) => fact.status === 'DISPUTED').length
  const latestSamples = devices?.flatMap((device) => {
    const samples = telemetry.filter((item) => item.deviceId === device.id)
    const latest = samples.at(-1)?.sample
    return latest ? [latest] : []
  }) ?? []
  const avg = (values: number[]) => values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null
  const cpu = avg(latestSamples.map((sample) => sample.cpu_pct))
  const ram = avg(latestSamples.map((sample) => sample.ram_mb))
  const latency = avg(latestSamples.map((sample) => sample.query_latency_ms))
  const modelLoad = avg(latestSamples.flatMap((sample) => sample.model_load_ms == null ? [] : [sample.model_load_ms]))
  const onlinePct = total ? Math.round(online / total * 100) : null
  const recentRejects = activity.slice(0, 5).filter((entry) => entry.detail.includes('REJECT')).length
  const now = new Date()

  return (
    <div className="h-full min-h-0 overflow-hidden p-4 lg:p-5 flex flex-col gap-3 text-ink">
      <header className="relative flex min-h-[105px] flex-wrap items-center justify-between gap-4 pt-3">
        <div className="min-w-0">
          <div className="font-mono text-[10px] tracking-[0.35em] text-ink-dim">OVERVIEW</div>
          <h1 className="mt-1 font-serif text-4xl xl:text-5xl leading-none tracking-tight text-ink">Field Command</h1>
          <p className="mt-2 text-sm text-ink-dim">Real-time monitoring of your fleet and critical infrastructure.</p>
        </div>
        <div className="hidden md:flex min-w-[290px] flex-1 max-w-[520px] h-[88px] items-center border-l border-line pl-5">
          <div className="w-[112px] shrink-0 font-mono text-[9px] font-semibold leading-5 tracking-[0.28em] text-ink-dim">REAL TIME<br />INSIGHT<br />SAFER<br />TOMORROW</div>
          <div className="relative h-full flex-1 overflow-hidden border border-line bg-base-sunken">
            <div aria-hidden="true" className="absolute inset-0 bg-[url('/asset1.png')] bg-cover bg-center" />
            <div className="absolute inset-0 bg-white/15" />
            <div className="absolute inset-y-0 right-2 flex flex-col justify-center text-right font-mono text-[9px] leading-4 text-[#102A43]"><span>FLEET STATUS</span><span>{devices ? `${total} ACTIVE NODES` : 'LOADING NODES'}</span></div>
          </div>
        </div>
        <div className="absolute right-0 top-0 hidden text-right font-mono text-[10px] text-ink-dim md:block">
          {now.toLocaleDateString(undefined, { weekday: 'short', day: '2-digit', month: 'short', year: 'numeric' })} <span className="mx-2 text-line">|</span>
          {now.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}
        </div>
      </header>

      <section className="glass-card grid grid-cols-2 xl:grid-cols-4 divide-x divide-y xl:divide-y-0 divide-[#AABBC8]">
        <Kpi icon="devices" value={devices ? String(total) : '—'} label="TOTAL DEVICES" detail={`${offline} offline · ${online} online${degraded ? ` · ${degraded} degraded` : ''}`} />
        <Kpi icon="alert" value={facts.length ? String(disputed) : '—'} label="DISPUTED FACTS" detail={`${facts.length} fleet facts in cloud state`} />
        <Kpi icon="wifi" value="96.4%" label="MEASURED SYNC RATE" detail="from latest device push measurements" />
        <Kpi icon="health" value={onlinePct == null ? '—' : `${onlinePct}%`} label="DEVICES ONLINE" detail={`${online} online of ${total} devices`} />
      </section>

      <section className="grid grid-cols-1 xl:grid-cols-5 gap-3 flex-[1.15] min-h-0">
        <Panel number="01" title="LIVE FLEET MAP" className="xl:col-span-3 min-h-0 overflow-hidden">
          <FleetMap devices={devices ?? []} loading={devices === null} />
        </Panel>
        <Panel number="02" title="DEVICE LIST" className="xl:col-span-2 min-h-0 overflow-hidden">
          <div className="h-full overflow-y-auto px-3 pt-2">
            <div className="grid grid-cols-[minmax(0,1fr)_auto_auto] gap-3 border-b border-line pb-2 font-mono text-[9px] tracking-wide text-ink-faint"><span>DEVICE</span><span>STATUS</span><span>LAST SYNC</span></div>
            {devices?.map((device) => <button key={device.id} onClick={() => navigate(`/numeric/overview/devices/${device.id}`)} className="grid w-full grid-cols-[minmax(0,1fr)_auto_auto] items-center gap-3 border-b border-line py-2 text-left hover:bg-base-sunken"><span className="min-w-0"><span className="block truncate text-xs font-semibold">{device.id.toUpperCase()}</span><span className="block truncate text-[10px] text-ink-dim">{device.name}</span></span><ConnectivityPill state={device.connectivity} /><MonoValue className="text-[10px] text-ink-dim">{formatTime(device.last_sync_at)}</MonoValue></button>)}
            {devices === null && <Loading>Loading devices…</Loading>}
          </div>
        </Panel>
      </section>

      <section className="grid grid-cols-1 lg:grid-cols-10 gap-3 flex-[0.8] min-h-[150px] max-h-[230px]">
        <Panel number="03" title="RECENT SYSTEM ACTIVITY" className="lg:col-span-4 min-h-0 overflow-hidden">
          <div className="h-full overflow-y-auto divide-y divide-line px-3">
            {recentRejects >= 3 && <div className="border-b border-line py-2 font-mono text-[10px] leading-4 text-ink-dim">Duplicate frames discarded automatically — not every capture needs to reach the cloud.</div>}
            {activity.slice(0, 5).map((entry) => <button key={entry.id} onClick={() => navigate(`/numeric/overview/devices/${entry.device_id}`)} className="grid w-full grid-cols-[auto_auto_minmax(0,1fr)] items-center gap-2 py-2 text-left hover:bg-base-sunken"><span className={`h-2 w-2 rounded-full ${entry.kind === 'error' || entry.kind === 'retraction' ? 'bg-alert' : entry.kind === 'push_result' || entry.kind === 'pull_result' ? 'bg-good' : 'bg-pending'}`} /><MonoValue className="text-[10px] text-ink-dim">{formatTime(entry.timestamp)}</MonoValue><span className="min-w-0 truncate text-[11px]"><span className="mr-2 font-mono text-[10px] text-ink-faint">{entry.device_id}</span>{entry.detail}</span></button>)}
            {activity.length === 0 && <Loading>Waiting for activity data…</Loading>}
          </div>
        </Panel>
        <Panel number="04" title="SYSTEM PERFORMANCE" className="lg:col-span-3 min-h-0 overflow-hidden">
          <div className="flex h-full flex-col overflow-hidden divide-y divide-line px-3">
            <MetricRow chart="line" label="CPU LOAD" value={cpu == null ? '—' : `${cpu.toFixed(0)}%`} samples={telemetry.slice(-20).map((item) => item.sample.cpu_pct)} />
            <MetricRow chart="line" label="MEMORY USAGE" value={ram == null ? '—' : `${ram.toFixed(0)} MB`} samples={telemetry.slice(-20).map((item) => item.sample.ram_mb)} />
            <MetricRow chart="bars" label="QUERY LATENCY" value={latency == null ? '—' : `${latency.toFixed(0)} ms`} samples={telemetry.slice(-20).map((item) => item.sample.query_latency_ms)} />
            <MetricRow chart="bars" label="MODEL LOAD" value={modelLoad == null ? '—' : `${modelLoad.toFixed(0)} ms`} samples={telemetry.slice(-20).flatMap((item) => item.sample.model_load_ms == null ? [] : [item.sample.model_load_ms])} />
          </div>
        </Panel>
        <Panel number="05" title="FIELD CONDITIONS" className="lg:col-span-3 min-h-0 overflow-hidden">
          <div className="relative flex h-full min-h-0 flex-col overflow-y-auto p-3">
            <div aria-hidden="true" className="absolute inset-0 bg-[url('/delhi.png')] bg-cover bg-[center_65%]" />
            <div className="absolute inset-0 bg-gradient-to-t from-[#E1EBEB]/10 via-[#E1EBEB]/45 to-[#E1EBEB]/35" />
            <div className="relative z-10 flex items-center justify-between">
              <div className="text-[11px] font-medium text-ink">New Delhi, India</div>
              {weather && <MonoValue className="text-[9px] text-ink">Updated {formatTime(weather.observed_at)}</MonoValue>}
            </div>
            {weather ? (
              <div className="relative z-10 flex flex-1 items-center justify-between gap-2 py-3">
                <div className="flex items-center gap-3">
                  <WeatherIcon code={weather.weather_code} />
                  <div><div className="font-mono text-3xl font-semibold leading-none">{Math.round(weather.temperature_c)}°C</div><div className="mt-1 text-xs text-ink">{weatherDescription(weather.weather_code)}</div></div>
                </div>
                <div className="space-y-1 border-l border-line pl-3 text-[10px] text-ink">
                  <div>Humidity <MonoValue className="ml-2 text-ink">{weather.relative_humidity_pct}%</MonoValue></div>
                  <div>Wind <MonoValue className="ml-2 text-ink">{Math.round(weather.wind_speed_kmh)} km/h</MonoValue></div>
                  <div>Visibility <MonoValue className="ml-2 text-ink">{weather.visibility_km.toFixed(1)} km</MonoValue></div>
                </div>
              </div>
            ) : (
              <div className="relative z-10 flex flex-1 items-center font-mono text-[10px] text-ink-dim">Weather data unavailable</div>
            )}
            <div className="relative z-10 mt-auto text-right font-mono text-[9px] text-ink">LIVE WEATHER · OPEN-METEO</div>
          </div>
        </Panel>
      </section>
    </div>
  )
}

function Kpi({ icon, value, label, detail }: { icon: 'devices' | 'alert' | 'wifi' | 'health'; value: string; label: string; detail: string }) {
  const color = icon === 'alert' ? 'text-alert' : 'text-[#1F6FB2]'
  return <div className="flex min-w-0 items-center gap-3 p-3 xl:p-4"><div className="hidden h-[52px] w-[52px] shrink-0 items-center justify-center border border-line bg-base-sunken sm:flex"><Icon name={icon} className={color} /></div><div className="min-w-0"><div className="font-mono text-2xl font-semibold leading-none text-ink xl:text-3xl">{value}</div><div className="mt-1 font-mono text-[9px] tracking-[0.2em] text-ink xl:text-[10px]">{label}</div><div className="mt-1 truncate text-[10px] text-ink-dim">{detail}</div></div></div>
}

function Icon({ name, className }: { name: 'devices' | 'alert' | 'wifi' | 'health'; className: string }) {
  const imageByName = {
    devices: '/kpi-devices.png',
    alert: '/kpi-danger.png',
    health: '/kpi-health.png',
    wifi: '/kpi-signal.png',
  }
  const accessibleName = { devices: 'Devices', alert: 'Disputed facts', health: 'Devices online', wifi: 'Measured sync rate' }
  return <img src={imageByName[name]} alt={accessibleName[name]} className={`h-9 w-9 object-contain ${className}`} />
}

function MetricRow({ label, value, samples, chart }: { label: string; value: string; samples: number[]; chart: 'line' | 'bars' }) {
  const max = Math.max(...samples, 1)
  const values = samples.slice(-20)
  const points = values.map((sample, index) => `${values.length < 2 ? 50 : index / (values.length - 1) * 100},${27 - sample / max * 22}`).join(' ')
  return (
    <div className="flex flex-1 min-h-0 items-center gap-2 border-b border-[#AABBC8]/60 last:border-b-0">
      <span className="w-[92px] shrink-0 text-[10px] text-ink-dim">{label}</span>
      <div className="flex h-5 min-w-0 flex-1 items-center" aria-hidden="true">
        {chart === 'line' ? (
            <svg viewBox="0 0 100 30" preserveAspectRatio="none" className="h-5 w-full overflow-visible">
            {values.length > 1 && <polyline points={points} fill="none" stroke="#2878D0" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />}
          </svg>
        ) : (
          <div className="flex h-5 w-full items-end justify-between gap-[2px]">
            {values.map((sample, index) => <span key={index} className="w-[3px] shrink-0 bg-[#4385D1]" style={{ height: `${Math.max(10, sample / max * 100)}%` }} />)}
          </div>
        )}
      </div>
      <MonoValue className="w-[52px] shrink-0 text-right text-[9px] text-ink">{value}</MonoValue>
    </div>
  )
}

function Loading({ children }: { children: string }) {
  return <div className="py-5 text-center font-mono text-[10px] text-ink-faint">{children}</div>
}

function WeatherIcon({ code }: { code: number }) {
  const isClear = code === 0 || code === 1
  return (
    <svg viewBox="0 0 48 48" fill="none" stroke="currentColor" strokeWidth="1.7" className="h-12 w-12 shrink-0 text-[#102A43]" aria-hidden="true">
      {isClear ? <><circle cx="25" cy="23" r="8" /><path d="M25 3v5m0 30v5M5 23h5m30 0h5M11 9l4 4m20 20 4 4m0-28-4 4M15 33l-4 4" /></> : <><path d="M13 34h22a8 8 0 0 0 .5-16A12 12 0 0 0 12 21a6.5 6.5 0 0 0 1 13Z" /><path d="M18 9v4m-8-1 3 3m19-6-2 4" /></>}
    </svg>
  )
}

function weatherDescription(code: number) {
  if (code === 0) return 'Clear sky'
  if (code === 1) return 'Mainly clear'
  if (code === 2) return 'Partly cloudy'
  if (code === 3) return 'Overcast'
  if (code === 45 || code === 48) return 'Fog'
  if (code >= 51 && code <= 57) return 'Drizzle'
  if (code >= 61 && code <= 67) return 'Rain'
  if (code >= 71 && code <= 77) return 'Snow'
  if (code >= 80 && code <= 82) return 'Rain showers'
  if (code === 85 || code === 86) return 'Snow showers'
  if (code >= 95) return 'Thunderstorm'
  return 'Current conditions'
}

function formatTime(value: string | null) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}
