import { useEffect, useMemo, useRef, useState } from 'react'
import type { PointerEvent, WheelEvent } from 'react'
import type { ConnectivityState, DeviceSummary } from '../types'

const TILE_SIZE = 256
const DELHI = { latitude: 28.6139, longitude: 77.2090 }
type Point = { x: number; y: number }
type Center = { latitude: number; longitude: number }
type StatusFilter = 'ALL' | ConnectivityState

function project(latitude: number, longitude: number, zoom: number): Point {
  const scale = TILE_SIZE * 2 ** zoom
  const lat = Math.max(-85.0511, Math.min(85.0511, latitude)) * Math.PI / 180
  return {
    x: (longitude + 180) / 360 * scale,
    y: (1 - Math.asinh(Math.tan(lat)) / Math.PI) / 2 * scale,
  }
}

function unproject(point: Point, zoom: number): Center {
  const scale = TILE_SIZE * 2 ** zoom
  return {
    longitude: point.x / scale * 360 - 180,
    latitude: Math.atan(Math.sinh(Math.PI * (1 - 2 * point.y / scale))) * 180 / Math.PI,
  }
}

function modulo(value: number, divisor: number) {
  return ((value % divisor) + divisor) % divisor
}

function formatLastSync(value: string | null) {
  if (!value) return 'No sync recorded'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? 'No sync recorded' : `Last sync: ${date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`
}

function statusColor(state: ConnectivityState) {
  if (state === 'ONLINE') return '#10A77A'
  if (state === 'DEGRADED') return '#D98B18'
  return '#8296AA'
}

export function FleetMap({ devices, loading }: { devices: DeviceSummary[]; loading: boolean }) {
  const mapRef = useRef<HTMLDivElement>(null)
  const dragRef = useRef<{ pointerX: number; pointerY: number; center: Point } | null>(null)
  const [size, setSize] = useState({ width: 0, height: 0 })
  const [zoom, setZoom] = useState(12)
  const [center, setCenter] = useState<Center>(DELHI)
  const [filter, setFilter] = useState<StatusFilter>('ALL')
  const [selectedId, setSelectedId] = useState<string | null>(null)

  useEffect(() => {
    const element = mapRef.current
    if (!element) return
    const observer = new ResizeObserver(([entry]) => {
      setSize({ width: entry.contentRect.width, height: entry.contentRect.height })
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [])

  const centerPoint = project(center.latitude, center.longitude, zoom)
  const left = centerPoint.x - size.width / 2
  const top = centerPoint.y - size.height / 2
  const tileCount = 2 ** zoom
  const firstTileX = Math.floor(left / TILE_SIZE)
  const lastTileX = Math.floor((left + size.width) / TILE_SIZE)
  const firstTileY = Math.max(0, Math.floor(top / TILE_SIZE))
  const lastTileY = Math.min(tileCount - 1, Math.floor((top + size.height) / TILE_SIZE))
  const tiles = useMemo(() => {
    const list: { x: number; y: number; left: number; top: number }[] = []
    for (let x = firstTileX - 1; x <= lastTileX + 1; x++) {
      for (let y = firstTileY - 1; y <= lastTileY + 1; y++) {
        if (y < 0 || y >= tileCount) continue
        list.push({
          x: modulo(x, tileCount),
          y,
          left: x * TILE_SIZE - left,
          top: y * TILE_SIZE - top,
        })
      }
    }
    return list
  }, [firstTileX, firstTileY, lastTileX, lastTileY, left, top, tileCount])

  const visibleDevices = devices.filter((device) =>
    device.latitude != null && device.longitude != null &&
    (filter === 'ALL' || device.connectivity === filter),
  )

  const changeZoom = (next: number) => setZoom(Math.max(3, Math.min(18, next)))

  const handlePointerDown = (event: PointerEvent<HTMLDivElement>) => {
    if ((event.target as HTMLElement).closest('button, select, a')) return
    event.currentTarget.setPointerCapture(event.pointerId)
    dragRef.current = { pointerX: event.clientX, pointerY: event.clientY, center: centerPoint }
  }

  const handlePointerMove = (event: PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current
    if (!drag) return
    setCenter(unproject({
      x: drag.center.x - (event.clientX - drag.pointerX),
      y: drag.center.y - (event.clientY - drag.pointerY),
    }, zoom))
  }

  const handlePointerUp = (event: PointerEvent<HTMLDivElement>) => {
    dragRef.current = null
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
  }

  const handleWheel = (event: WheelEvent<HTMLDivElement>) => {
    event.preventDefault()
    changeZoom(zoom + (event.deltaY < 0 ? 1 : -1))
  }

  const recenter = () => {
    setCenter(DELHI)
    setZoom(12)
    setFilter('ALL')
  }

  return (
    <div
      ref={mapRef}
      className="relative h-full min-h-[220px] w-full overflow-hidden bg-[#E7F0F7] select-none touch-none"
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={handlePointerUp}
      onPointerCancel={handlePointerUp}
      onWheel={handleWheel}
      aria-label="Interactive map of fleet devices in New Delhi"
    >
      {tiles.map((tile) => (
        <img
          key={`${zoom}/${tile.x}/${tile.y}`}
          src={`https://tile.openstreetmap.org/${zoom}/${tile.x}/${tile.y}.png`}
          alt=""
          draggable={false}
          className="pointer-events-none absolute h-64 w-64 max-w-none"
          style={{ left: tile.left, top: tile.top, filter: 'saturate(.55) sepia(.08) hue-rotate(165deg)' }}
        />
      ))}

      {visibleDevices.map((device) => {
        const point = project(device.latitude!, device.longitude!, zoom)
        const markerLeft = point.x - left
        const markerTop = point.y - top
        const percentage = Math.round(device.memory_used / device.memory_cap * 100)
        const color = statusColor(device.connectivity)
        const selected = selectedId === device.id
        return (
          <div key={device.id} className="group absolute z-10" style={{ left: markerLeft, top: markerTop }}>
            <button
              type="button"
              aria-label={`${device.name}, ${device.connectivity}`}
              onClick={() => setSelectedId(selected ? null : device.id)}
              className="-translate-x-1/2 -translate-y-1/2 rounded-full p-1 focus-visible:outline-2"
            >
              <span
                className={`flex h-8 w-8 items-center justify-center rounded-full border-[3px] bg-white shadow-[0_1px_6px_rgba(16,42,67,0.35)] ${selected ? 'ring-2 ring-[#1F6FB2]' : ''}`}
                style={{ borderColor: color, boxShadow: `0 0 0 4px ${color}33, 0 1px 6px rgba(16,42,67,0.35)` }}
              >
                <StatusGlyph state={device.connectivity} color={color} />
              </span>
            </button>
            <div className={`${selected ? 'block' : 'hidden group-hover:block group-focus-within:block'} absolute bottom-5 left-1/2 z-30 w-52 -translate-x-1/2 border border-line bg-white/95 p-2.5 text-ink shadow-lg`}>
              <div className="text-xs font-semibold">{device.id.toUpperCase()}</div>
              <div className="text-[11px] text-ink-dim">{device.name}</div>
              <div className="mt-1 flex items-center gap-1.5 text-[11px]" style={{ color }}><span>●</span>{device.connectivity}<span className="ml-auto text-ink-dim">{percentage}% memory</span></div>
              <div className="mt-1 font-mono text-[10px] text-ink-dim">{formatLastSync(device.last_sync_at)}</div>
              <div className="absolute -bottom-1 left-1/2 h-2 w-2 -translate-x-1/2 rotate-45 border-b border-r border-line bg-white" />
            </div>
          </div>
        )
      })}

      <div className="absolute left-3 top-3 z-20">
        <select
          value={filter}
          onChange={(event) => setFilter(event.target.value as StatusFilter)}
          onPointerDown={(event) => event.stopPropagation()}
          className="border border-line bg-white/95 px-2 py-1.5 text-[11px] text-ink shadow-sm outline-none"
          aria-label="Filter devices by connectivity"
        >
          <option value="ALL">All Devices</option>
          <option value="ONLINE">Online</option>
          <option value="DEGRADED">Degraded</option>
          <option value="OFFLINE">Offline</option>
        </select>
      </div>

      <div className="absolute right-3 top-3 z-20 flex flex-col overflow-hidden border border-line bg-white/95 shadow-sm">
        <MapControl label="Zoom in" onClick={() => changeZoom(zoom + 1)}>+</MapControl>
        <MapControl label="Zoom out" onClick={() => changeZoom(zoom - 1)}>−</MapControl>
        <MapControl label="Recenter on Delhi" onClick={recenter}>◎</MapControl>
        <MapControl label="Toggle fullscreen" onClick={() => {
          if (!document.fullscreenElement) void mapRef.current?.requestFullscreen()
          else void document.exitFullscreen()
        }}>⛶</MapControl>
      </div>

      {loading && <div className="absolute inset-0 z-20 flex items-center justify-center bg-white/55 font-mono text-xs text-ink-dim">Loading fleet locations…</div>}
      {!loading && visibleDevices.length === 0 && <div className="absolute left-1/2 top-1/2 z-20 -translate-x-1/2 -translate-y-1/2 bg-white/90 px-3 py-2 font-mono text-[10px] text-ink-dim">No devices with location data for this filter.</div>}

      <div className="absolute inset-x-0 bottom-0 z-20 flex flex-wrap items-center justify-between gap-2 bg-white/90 px-3 py-1.5">
        <div className="flex flex-wrap gap-3 text-[10px] text-ink-dim">
          <Legend state="ONLINE" color={statusColor('ONLINE')} />
          <Legend state="DEGRADED" color={statusColor('DEGRADED')} />
          <Legend state="OFFLINE" color={statusColor('OFFLINE')} />
        </div>
        <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer" onPointerDown={(event) => event.stopPropagation()} className="text-[9px] text-[#315C86] underline">© OpenStreetMap contributors</a>
      </div>
    </div>
  )
}

function StatusGlyph({ state, color }: { state: ConnectivityState; color: string }) {
  if (state === 'DEGRADED') return <svg viewBox="0 0 20 20" className="h-4 w-4" fill="none" stroke={color} strokeWidth="2"><path d="M10 2 18 17H2L10 2Z" /><path d="M10 7v5m0 2h.01" /></svg>
  if (state === 'OFFLINE') return <svg viewBox="0 0 20 20" className="h-4 w-4" fill="none" stroke={color} strokeWidth="2"><circle cx="10" cy="10" r="7" /><path d="m5 15 10-10" /></svg>
  return <svg viewBox="0 0 20 20" className="h-4 w-4" fill={color}><circle cx="10" cy="10" r="4" /></svg>
}

function Legend({ state, color }: { state: ConnectivityState; color: string }) {
  return <span className="flex items-center gap-1.5"><i className="h-2 w-2 rounded-full" style={{ backgroundColor: color }} />{state}</span>
}

function MapControl({ label, onClick, children }: { label: string; onClick: () => void; children: string }) {
  return <button type="button" title={label} aria-label={label} onClick={onClick} onPointerDown={(event) => event.stopPropagation()} className="flex h-9 w-9 items-center justify-center border-b border-line text-lg leading-none text-ink hover:bg-base-sunken last:border-b-0">{children}</button>
}
