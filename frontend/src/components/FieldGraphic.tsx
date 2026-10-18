import { useId } from 'react'
import './field-graphic.css'

type GraphicKind = 'signal' | 'memory' | 'consensus' | 'vision'

/** Decorative geometry only; never represents measured telemetry. */
export function FieldGraphic({ kind = 'signal', className = '' }: { kind?: GraphicKind; className?: string }) {
  const id = useId().replace(/:/g, '')
  return (
    <svg className={`ev-field-graphic ev-graphic-${kind} ${className}`} viewBox="0 0 480 220" fill="none" aria-hidden="true" focusable="false">
      <defs>
        <linearGradient id={`${id}-terrain`} x1="0" y1="0" x2="480" y2="220" gradientUnits="userSpaceOnUse"><stop stopColor="#236a70" stopOpacity=".04" /><stop offset="1" stopColor="#d5c5ac" stopOpacity=".22" /></linearGradient>
      </defs>
      <path d="M0 180Q90 55 170 119T330 88T480 45V220H0Z" fill={`url(#${id}-terrain)`} />
      <g className="ev-contour-lines" stroke="currentColor" strokeOpacity=".19" strokeWidth="1">
        {Array.from({ length: 10 }, (_, i) => <path key={i} d={`M-20 ${205-i*15} Q80 ${65-i*12} 173 ${134-i*9} T334 ${94-i*10} T500 ${30-i*8}`} />)}
      </g>
      {kind === 'memory' ? <g className="ev-memory-merge" stroke="currentColor"><rect className="ev-memory-input ev-memory-input-a" x="80" y="65" width="48" height="57" rx="8" /><rect className="ev-memory-input ev-memory-input-b" x="80" y="126" width="48" height="40" rx="8" /><path d="M145 95H205L245 116M145 145H205L245 116" strokeDasharray="3 6" /><rect x="265" y="75" width="95" height="82" rx="12" fill="#d5c5ac" fillOpacity=".18" /><path d="M285 98H339M285 113H327M285 128H333" /></g> : kind === 'vision' ? <g stroke="currentColor"><rect x="115" y="36" width="250" height="150" rx="12" strokeOpacity=".4" /><path d="M150 70H175M150 70V95M330 70H305M330 70V95M150 150H175M150 150V125M330 150H305M330 150V125" strokeWidth="2" /><path className="ev-scan-line" d="M150 105H330" stroke="#d5c5ac" strokeWidth="2" /></g> : <g stroke="currentColor">
        <path d={kind === 'consensus' ? 'M90 60Q175 60 240 112H390M90 165Q175 165 240 112' : 'M60 160Q140 150 190 112T300 118T420 48'} strokeOpacity=".4" />
        <path className="ev-signal-trace" d={kind === 'consensus' ? 'M90 60Q175 60 240 112H390M90 165Q175 165 240 112' : 'M60 160Q140 150 190 112T300 118T420 48'} stroke="#d5c5ac" strokeWidth="2.5" strokeDasharray="9 180" />
        {(kind === 'consensus' ? [[90,60],[90,165],[240,112],[390,112]] : [[60,160],[190,112],[300,118],[420,48]]).map(([x,y], i) => <g key={i}><circle cx={x} cy={y} r="12" fill="#236a70" fillOpacity=".13" /><circle cx={x} cy={y} r="4" fill={i === 2 ? '#d5c5ac' : 'currentColor'} stroke="none" /></g>)}
      </g>}
    </svg>
  )
}
