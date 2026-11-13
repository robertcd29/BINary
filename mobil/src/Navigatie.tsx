// Navigatie pas cu pas pentru sofer, ca un GPS: instructiunea urmatoare, distanta pana la viraj,
// voce in romana, harta care urmareste camionul si recalcularea traseului cand iese de pe el.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { CircleMarker, Polyline, useMap } from 'react-leaflet'
import {
  ArrowUp, ArrowUpLeft, ArrowUpRight, CornerUpLeft, CornerUpRight, Flag, LocateFixed,
  Navigation2, Play, Square, Undo2, Volume2, VolumeX
} from 'lucide-react'

import {
  getNavigation, sendDriverPosition,
  type Coordinates, type Navigation, type NavStep, type NavStop
} from './api'

const OFF_ROUTE_M = 50          // mai departe de atat de traseu = a luat-o pe alt drum
const REROUTE_EVERY_MS = 10_000 // nu recalculam mai des de atat
const ARRIVED_M = 30            // la 30 m de oprire consideram ca a ajuns
const ANNOUNCE_M = 150          // virajul se anunta cu vocea de la aceasta distanta
const TRUCK_SPEED_MS = 5.5      // ~20 km/h, viteza medie a unui camion de gunoi prin oras
const SIM_STEP_M = 12           // simulare: cati metri inainteaza camionul la fiecare pas
const SIM_TICK_MS = 400

// ---------- geometrie ----------

function meters(a: [number, number], b: [number, number]) {
  const dy = (a[0] - b[0]) * 111_320
  const dx = (a[1] - b[1]) * 111_320 * Math.cos((a[0] * Math.PI) / 180)
  return Math.hypot(dx, dy)
}

/** Distanta parcursa pe traseu pana in punctul cel mai apropiat de p, si cat de departe e p de traseu. */
function project(line: [number, number][], cum: number[], p: [number, number]) {
  let best = { along: 0, off: Infinity }
  const k = 111_320
  const cos = Math.cos((p[0] * Math.PI) / 180)
  for (let i = 0; i < line.length - 1; i++) {
    const [ay, ax] = [line[i][0] * k, line[i][1] * k * cos]
    const [by, bx] = [line[i + 1][0] * k, line[i + 1][1] * k * cos]
    const [py, px] = [p[0] * k, p[1] * k * cos]
    const len2 = (bx - ax) ** 2 + (by - ay) ** 2
    const t = len2 ? Math.max(0, Math.min(1, ((px - ax) * (bx - ax) + (py - ay) * (by - ay)) / len2)) : 0
    const off = Math.hypot(ax + t * (bx - ax) - px, ay + t * (by - ay) - py)
    if (off < best.off) best = { along: cum[i] + t * (cum[i + 1] - cum[i]), off }
  }
  return best
}

function pointAt(line: [number, number][], cum: number[], d: number): Coordinates {
  const i = Math.max(0, cum.findIndex(c => c >= d) - 1)
  if (d >= cum[cum.length - 1]) {
    const last = line[line.length - 1]
    return { lat: last[0], lng: last[1] }
  }
  const seg = cum[i + 1] - cum[i] || 1
  const t = (d - cum[i]) / seg
  return {
    lat: line[i][0] + t * (line[i + 1][0] - line[i][0]),
    lng: line[i][1] + t * (line[i + 1][1] - line[i][1])
  }
}

// ---------- texte ----------

export function formatDistance(m: number) {
  if (m >= 1000) return `${(m / 1000).toLocaleString('ro-RO', { maximumFractionDigits: 1 })} km`
  return `${Math.max(0, Math.round(m / 10) * 10)} m`
}

function inDistance(step: NavStep, m: number) {
  if (step.tip === 'sosire') return `În ${formatDistance(m)} ajungi la ${step.strada}`
  return `În ${formatDistance(m)}, ${step.text.charAt(0).toLowerCase()}${step.text.slice(1)}`
}

function ManeuverIcon({ tip, size = 34 }: { tip: NavStep['tip']; size?: number }) {
  switch (tip) {
    case 'dreapta': return <CornerUpRight size={size} />
    case 'stanga': return <CornerUpLeft size={size} />
    case 'usor-dreapta': return <ArrowUpRight size={size} />
    case 'usor-stanga': return <ArrowUpLeft size={size} />
    case 'intoarcere': return <Undo2 size={size} />
    case 'sosire': return <Flag size={size} />
    default: return <ArrowUp size={size} />
  }
}

function speak(text: string) {
  if (!('speechSynthesis' in window)) return
  window.speechSynthesis.cancel()
  const u = new SpeechSynthesisUtterance(text)
  u.lang = 'ro-RO'
  const ro = window.speechSynthesis.getVoices().find(v => v.lang.startsWith('ro'))
  if (ro) u.voice = ro
  window.speechSynthesis.speak(u)
}

// ---------- logica ----------

export type NavState = ReturnType<typeof useNavigation>

export function useNavigation(truckId: string, gps: Coordinates | null, enabled: boolean) {
  const [nav, setNav] = useState<Navigation | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [started, setStarted] = useState(false)
  const [voice, setVoice] = useState(true)
  const [follow, setFollow] = useState(true)
  const [simulating, setSimulating] = useState(false)
  const [simPos, setSimPos] = useState<Coordinates | null>(null)

  const position = simulating ? simPos : gps
  const positionRef = useRef(position)
  positionRef.current = position
  const lastReroute = useRef(0)

  const refresh = useCallback(async (from?: Coordinates | null) => {
    setLoading(true)
    try {
      setNav(await getNavigation(truckId, from ?? positionRef.current))
      setError('')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Nu s-a putut calcula navigația.')
    } finally {
      setLoading(false)
    }
  }, [truckId])

  useEffect(() => {
    if (!enabled) return
    setNav(null)
    setSimulating(false)
    refresh(null)
  }, [enabled, truckId, refresh])

  const leg = nav?.etape[0] ?? null
  const stop: NavStop | null = nav?.opriri[0] ?? null
  const cum = useMemo(() => {
    if (!leg) return []
    const c = [0]
    for (let i = 1; i < leg.geometrie.length; i++) c.push(c[i - 1] + meters(leg.geometrie[i - 1], leg.geometrie[i]))
    return c
  }, [leg])

  // cat a parcurs din etapa curenta si cat de departe e de traseu
  const progress = useMemo(() => {
    if (!leg || cum.length < 2 || !position) return { along: 0, off: 0 }
    return project(leg.geometrie, cum, [position.lat, position.lng])
  }, [leg, cum, position])

  let stepIndex = 0              // ultimul pas a carui manevra a fost deja trecuta
  leg?.pasi.forEach((s, i) => { if (s.la_m <= progress.along + 5) stepIndex = i })
  const current = leg?.pasi[stepIndex] ?? null
  const next = leg?.pasi[stepIndex + 1] ?? null
  const toNext = next ? Math.max(0, next.la_m - progress.along) : 0
  const remaining = leg ? Math.max(0, leg.distanta_m - progress.along) : 0
  const arrived = !!leg && !!position && remaining <= ARRIVED_M
  const etaMin = Math.max(1, Math.round(remaining / TRUCK_SPEED_MS / 60))

  // a iesit de pe traseu: recalculam de unde e acum
  useEffect(() => {
    if (!started || !position || !leg || progress.off < OFF_ROUTE_M) return
    if (Date.now() - lastReroute.current < REROUTE_EVERY_MS) return
    lastReroute.current = Date.now()
    if (voice) speak('Recalculez traseul')
    refresh(position)
  }, [started, position, leg, progress.off, voice, refresh])

  // vocea: fiecare manevra se anunta o data cand se apropie, iar sosirea o data
  const announced = useRef(new Set<string>())
  useEffect(() => { announced.current.clear() }, [leg])
  useEffect(() => {
    if (!started || !voice || !next) return
    const key = `${stepIndex + 1}`
    if (toNext <= ANNOUNCE_M && !announced.current.has(key) && !arrived) {
      announced.current.add(key)
      speak(next.tip === 'sosire' ? `Ajungi la ${next.strada}` : next.text)
    }
  }, [started, voice, next, stepIndex, toNext, arrived])
  useEffect(() => {
    if (started && voice && arrived && stop && !announced.current.has('sosire')) {
      announced.current.add('sosire')
      speak(stop.tip === 'depou' ? 'Ai ajuns la depou. Traseul de azi e gata.' : `Ai ajuns la ${stop.nume}`)
    }
  }, [started, voice, arrived, stop])

  // simulare pentru demo: camionul merge singur pe traseu, iar dispecerul il vede pe dashboard
  const simDist = useRef(0)
  useEffect(() => { simDist.current = 0 }, [leg])
  useEffect(() => {
    if (!simulating || !leg || cum.length < 2) return
    const total = cum[cum.length - 1]
    setSimPos(pointAt(leg.geometrie, cum, simDist.current))
    let ticks = 0
    const id = setInterval(() => {
      simDist.current = Math.min(total, simDist.current + SIM_STEP_M)
      const p = pointAt(leg.geometrie, cum, simDist.current)
      setSimPos(p)
      if (++ticks % 5 === 0) sendDriverPosition(truckId, p).catch(() => {})
    }, SIM_TICK_MS)
    return () => clearInterval(id)
  }, [simulating, leg, cum, truckId])

  function start() {
    setStarted(true)
    setFollow(true)
    if (voice) speak(current ? `Navigație pornită. ${current.text}` : 'Navigație pornită')
  }

  return {
    nav, leg, stop, error, loading, started, voice, follow, simulating, position,
    current, next, toNext, remaining, arrived, etaMin, stepIndex,
    refresh, start,
    stop_: () => { setStarted(false); setSimulating(false); window.speechSynthesis?.cancel() },
    setVoice, setFollow,
    toggleSim: () => { setSimulating(s => !s); setStarted(true); setFollow(true) }
  }
}

// ---------- interfata ----------

export function NavCard({ n, onComplete }: {
  n: NavState
  onComplete: (stop: NavStop) => Promise<void>
}) {
  const [busy, setBusy] = useState(false)

  if (n.error) return <section className="panel nav-card"><p className="empty">{n.error}</p></section>
  if (!n.nav || !n.leg || !n.stop) {
    return <section className="panel nav-card"><p className="empty">{n.loading ? 'Calculez navigația...' : 'Nu există navigație pentru acest camion.'}</p></section>
  }

  const ramase = n.nav.opriri.filter(o => o.tip !== 'depou').length
  const instructiune = n.arrived || !n.next
    ? null
    : n.started ? inDistance(n.next, n.toNext) : n.current?.text

  async function done() {
    if (!n.stop) return
    setBusy(true)
    try {
      await onComplete(n.stop)
      await n.refresh(n.position)
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="panel nav-card">
      <div className={`nav-instruction${n.arrived ? ' arrived' : ''}`}>
        <div className="nav-arrow">
          <ManeuverIcon tip={n.arrived ? 'sosire' : n.started ? (n.next?.tip ?? 'sosire') : (n.current?.tip ?? 'start')} />
        </div>
        <div>
          {n.arrived ? (
            <strong>{n.stop.tip === 'depou' ? 'Ai ajuns la depou' : `Ai ajuns la ${n.stop.nume}`}</strong>
          ) : (
            <>
              {n.started && n.next && <span className="nav-distance">{formatDistance(n.toNext)}</span>}
              <strong>{instructiune}</strong>
            </>
          )}
          {!n.arrived && n.started && n.current && <small>Acum pe {n.current.strada}</small>}
        </div>
      </div>

      <div className="nav-destination">
        <div>
          <small>{n.stop.tip === 'depou' ? 'Ultima etapă' : `Următoarea oprire · ${ramase} rămase`}</small>
          <strong>{n.stop.tip === 'depou' ? 'Întoarcere la depou' : n.stop.nume}</strong>
          {n.stop.adresa && <small>{n.stop.adresa}</small>}
        </div>
        <div className="nav-eta">
          <strong>{formatDistance(n.remaining)}</strong>
          <small>~{n.etaMin} min</small>
        </div>
      </div>

      <div className="nav-actions">
        {!n.started ? (
          <button className="primary-button" onClick={n.start}><Navigation2 size={17} /> Pornește navigația</button>
        ) : n.stop.tip !== 'depou' ? (
          <button className="primary-button" disabled={busy} onClick={done}>
            {n.stop.tip === 'depozitare' ? 'Am colectat' : 'Am golit ghena'} · următoarea
          </button>
        ) : (
          <button className="secondary-button" onClick={n.stop_}><Square size={15} /> Oprește navigația</button>
        )}
        <button className="icon-button" onClick={() => n.setVoice(!n.voice)} aria-label={n.voice ? 'Oprește vocea' : 'Pornește vocea'}>
          {n.voice ? <Volume2 size={18} /> : <VolumeX size={18} />}
        </button>
        {!n.follow && n.started && (
          <button className="icon-button" onClick={() => n.setFollow(true)} aria-label="Centrează pe camion">
            <LocateFixed size={18} />
          </button>
        )}
      </div>

      <div className="nav-footer">
        <button className="link-button" onClick={n.toggleSim}>
          {n.simulating ? <><Square size={13} /> Oprește simularea</> : <><Play size={13} /> Simulează drumul (demo)</>}
        </button>
        <a
          className="link-button"
          href={`https://www.google.com/maps/dir/?api=1&destination=${n.stop.lat},${n.stop.lng}&travelmode=driving`}
          target="_blank" rel="noreferrer"
        >
          Deschide în Google Maps
        </a>
      </div>
    </section>
  )
}

/** Straturile de navigatie de pe harta: etapa curenta, virajul urmator, camionul si urmarirea lui. */
export function NavLayer({ n }: { n: NavState }) {
  const map = useMap()
  const zoomed = useRef(false)

  useEffect(() => {
    const stopFollow = () => n.setFollow(false)
    map.on('dragstart', stopFollow)
    return () => { map.off('dragstart', stopFollow) }
  }, [map, n.setFollow])

  useEffect(() => {
    if (!n.started || !n.follow || !n.position) return
    if (!zoomed.current) {
      zoomed.current = true
      map.setView([n.position.lat, n.position.lng], 17, { animate: true })
    } else {
      map.panTo([n.position.lat, n.position.lng], { animate: true, duration: 0.3 })
    }
  }, [map, n.started, n.follow, n.position])

  useEffect(() => { if (!n.started) zoomed.current = false }, [n.started])

  if (!n.leg) return null
  return (
    <>
      <Polyline positions={n.leg.geometrie} pathOptions={{ color: '#fff', weight: 10, opacity: 0.9 }} />
      <Polyline positions={n.leg.geometrie} pathOptions={{ color: '#2463eb', weight: 6 }} />
      {n.next && n.next.tip !== 'sosire' && (
        <CircleMarker center={[n.next.lat, n.next.lng]} radius={6}
          pathOptions={{ color: '#fff', weight: 2, fillColor: '#2463eb', fillOpacity: 1 }} />
      )}
      {n.simulating && n.position && (
        <CircleMarker center={[n.position.lat, n.position.lng]} radius={9}
          pathOptions={{ color: '#fff', weight: 3, fillColor: '#2463eb', fillOpacity: 1 }} />
      )}
    </>
  )
}
