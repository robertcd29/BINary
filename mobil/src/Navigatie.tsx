// Navigatie pas cu pas pentru sofer, ca un GPS: instructiunea urmatoare, distanta pana la viraj,
// voce in romana, harta care urmareste camionul si recalcularea traseului cand iese de pe el.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import L from 'leaflet'
import { CircleMarker, Polyline, useMap } from 'react-leaflet'
import {
  ArrowUp, ArrowUpLeft, ArrowUpRight, Compass, CornerUpLeft, CornerUpRight, Flag, LocateFixed,
  Navigation2, Play, Square, Undo2, Volume2, VolumeX
} from 'lucide-react'

type RotatableMap = L.Map & { setBearing?: (deg: number) => void }
type ViewMode = '3d' | '2d' | 'nord'
const VIEW_LABEL: Record<ViewMode, string> = {
  '3d': 'înclinată, pe direcția de mers', '2d': 'de sus, pe direcția de mers', nord: 'de sus, nordul în sus'
}

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
const LOOK_AHEAD_M = 25         // directia de mers se ia spre un punct aflat atat de departe pe traseu
const NAV_ZOOM = 17
const TRUCK_SCREEN_Y = 0.72     // camionul sta la 72% din inaltimea hartii: vezi mai mult drum in fata

// ---------- geometrie ----------

function bearing(a: Coordinates, b: Coordinates) {
  const r = Math.PI / 180
  const y = Math.sin((b.lng - a.lng) * r) * Math.cos(b.lat * r)
  const x = Math.cos(a.lat * r) * Math.sin(b.lat * r) -
    Math.sin(a.lat * r) * Math.cos(b.lat * r) * Math.cos((b.lng - a.lng) * r)
  return (Math.atan2(y, x) / r + 360) % 360
}

/** Punctul aflat la `m` metri de `p`, in directia `deg`. */
function ahead(p: Coordinates, deg: number, m: number): Coordinates {
  const r = (deg * Math.PI) / 180
  return {
    lat: p.lat + (m * Math.cos(r)) / 111_320,
    lng: p.lng + (m * Math.sin(r)) / (111_320 * Math.cos((p.lat * Math.PI) / 180))
  }
}

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

  // 3d = inclinata si rotita ca la Waze, 2d = rotita pe directia de mers, nord = nordul in sus
  const [viewMode, setViewMode] = useState<ViewMode>('3d')
  const lastHeading = useRef(0)

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

  // directia de mers: din traseu, privind 25 m inainte (mai stabil decat busola telefonului)
  const heading = useMemo(() => {
    if (!leg || cum.length < 2) return 0
    const a = pointAt(leg.geometrie, cum, progress.along)
    const b = pointAt(leg.geometrie, cum, Math.min(cum[cum.length - 1], progress.along + LOOK_AHEAD_M))
    if (meters([a.lat, a.lng], [b.lat, b.lng]) < 1) return lastHeading.current
    lastHeading.current = bearing(a, b)
    return lastHeading.current
  }, [leg, cum, progress.along])

  let stepIndex = 0              // ultimul pas a carui manevra a fost deja trecuta
  leg?.pasi.forEach((s, i) => { if (s.la_m <= progress.along + 5) stepIndex = i })
  const current = leg?.pasi[stepIndex] ?? null
  const next = leg?.pasi[stepIndex + 1] ?? null
  const toNext = next ? Math.max(0, next.la_m - progress.along) : 0
  const remaining = leg ? Math.max(0, leg.distanta_m - progress.along) : 0
  // ajuns = chiar langa oprire, nu doar "la capatul traseului" (un GPS departe de traseu s-ar
  // proiecta oriunde pe el, inclusiv la capat)
  const arrived = !!leg && !!position && !!stop &&
    remaining <= ARRIVED_M && meters([position.lat, position.lng], [stop.lat, stop.lng]) <= ARRIVED_M * 2
  const etaMin = Math.max(1, Math.round(remaining / TRUCK_SPEED_MS / 60))

  // e departe de traseu (a luat-o pe alt drum, sau GPS-ul a aparut abia dupa calcul):
  // recalculam de unde e acum
  useEffect(() => {
    if (!position || !leg || progress.off < OFF_ROUTE_M) return
    if (Date.now() - lastReroute.current < REROUTE_EVERY_MS) return
    lastReroute.current = Date.now()
    if (started && voice) speak('Recalculez traseul')
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
    // harta in ecran, sub bara de instructiuni de sus
    setTimeout(() => document.querySelector('.map-panel')?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 100)
    if (voice) speak(current ? `Navigație pornită. ${current.text}` : 'Navigație pornită')
  }

  return {
    nav, leg, stop, error, loading, started, voice, follow, simulating, position, heading,
    viewMode, cycleView: () => setViewMode(m => (m === '3d' ? '2d' : m === '2d' ? 'nord' : '3d')),
    rotated: started && follow && viewMode !== 'nord',
    tilted: started && follow && viewMode === '3d',
    current, next, then: leg?.pasi[stepIndex + 2] ?? null, toNext, remaining, arrived, etaMin, stepIndex,
    refresh, start,
    stop_: () => { setStarted(false); setSimulating(false); window.speechSynthesis?.cancel() },
    setVoice, setFollow,
    toggleSim: () => {
      setSimulating(s => !s)
      if (!started) start()
      setFollow(true)
    }
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
      {!n.started && <div className={`nav-instruction${n.arrived ? ' arrived' : ''}`}>
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
      </div>}

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
        {n.started && (
          <button
            className="icon-button view-button"
            onClick={n.cycleView}
            aria-label={`Vedere: ${VIEW_LABEL[n.viewMode]}. Apasă pentru următoarea`}
            title={VIEW_LABEL[n.viewMode]}
          >
            <Compass size={16} />
            <span>{n.viewMode === 'nord' ? 'N' : n.viewMode.toUpperCase()}</span>
          </button>
        )}
        {!n.follow && n.started && (
          <button className="icon-button" onClick={() => n.setFollow(true)} aria-label="Centrează pe camion">
            <LocateFixed size={18} />
          </button>
        )}
      </div>

      <div className="nav-footer">
        <button className="link-button" onClick={n.toggleSim}>
          {n.simulating ? <><Square size={13} /> Oprește simularea</> : <><Play size={13} /> Simulare demo</>}
        </button>
        <a
          className="link-button"
          href={`https://www.google.com/maps/dir/?api=1&destination=${n.stop.lat},${n.stop.lng}&travelmode=driving`}
          target="_blank" rel="noreferrer"
        >
          Google Maps ↗
        </a>
      </div>
    </section>
  )
}

// ---------- harta: camera animata, ca la Waze ----------

const POS_SMOOTH_S = 0.45       // cat de repede ajunge camionul desenat la pozitia GPS (secunde)
const HEADING_SMOOTH_S = 0.6    // cat de lin se roteste harta la viraje
const NORTH_SMOOTH_S = 0.4      // revenirea la nordul in sus
const TILT_TRUCK_FROM_BOTTOM = 0.2  // vederea 3D: camionul la 20% din inaltime, de jos
const NAV_PANE = 'navigatie'

const TRUCK_ICON = L.divIcon({
  className: '',
  html: `<div class="truck-arrow"><svg viewBox="0 0 24 24" width="34" height="34">
    <path d="M12 2 L20 21 L12 16.5 L4 21 Z"/></svg></div>`,
  iconSize: [34, 34],
  iconAnchor: [17, 17]
})

/** Diferenta cea mai scurta intre doua unghiuri, in grade (-180..180). */
function angleDelta(from: number, to: number) {
  return ((to - from + 540) % 360) - 180
}

/** Straturile de navigatie de pe harta si camera care urmareste camionul, animata la fiecare cadru. */
export function NavLayer({ n }: { n: NavState }) {
  const map = useMap() as RotatableMap
  const latest = useRef(n)
  latest.current = n
  const cam = useRef({ pos: null as Coordinates | null, heading: 0, bearing: 0, center: '' })
  const zoomed = useRef(false)

  useEffect(() => {
    const stopFollow = () => latest.current.setFollow(false)
    map.on('dragstart', stopFollow)
    return () => { map.off('dragstart', stopFollow) }
  }, [map])

  // harta isi schimba marimea cand porneste navigatia si cand se inclina (dupa tranzitia CSS)
  useEffect(() => {
    const ids = [60, 480].map(ms => setTimeout(() => map.invalidateSize({ pan: false }), ms))
    return () => ids.forEach(clearTimeout)
  }, [map, n.started, n.tilted])

  useEffect(() => {
    const marker = L.marker([0, 0], { icon: TRUCK_ICON, interactive: false, zIndexOffset: 3000 })
    let raf = 0
    let last = performance.now()

    const frame = (now: number) => {
      const dt = Math.min(0.1, (now - last) / 1000)
      last = now
      const s = latest.current
      const c = cam.current

      // camionul desenat aluneca spre pozitia GPS, iar directia lui se roteste lin
      if (s.position && (s.started || s.simulating)) {
        const p = s.position
        if (!c.pos || meters([c.pos.lat, c.pos.lng], [p.lat, p.lng]) > 300) {
          c.pos = { ...p }
          c.heading = s.heading
        } else {
          const k = 1 - Math.exp(-dt / POS_SMOOTH_S)
          c.pos = { lat: c.pos.lat + (p.lat - c.pos.lat) * k, lng: c.pos.lng + (p.lng - c.pos.lng) * k }
          c.heading = (c.heading + angleDelta(c.heading, s.heading) * (1 - Math.exp(-dt / HEADING_SMOOTH_S)) + 360) % 360
        }
        if (!map.hasLayer(marker)) marker.addTo(map)
        marker.setLatLng([c.pos.lat, c.pos.lng])
      } else if (map.hasLayer(marker)) {
        marker.remove()
        c.pos = null
      }

      // rotirea hartii: legata de directia lina a camionului, sau revine lin la nord
      const want = s.rotated ? -c.heading : 0
      const nb = s.rotated ? want : c.bearing + angleDelta(c.bearing, want) * (1 - Math.exp(-dt / NORTH_SMOOTH_S))
      if (Math.abs(angleDelta(c.bearing, nb)) > 0.01) {
        c.bearing = nb
        map.setBearing?.(nb)
      }
      const el = marker.getElement()?.firstElementChild as HTMLElement | null
      if (el) el.style.transform = `rotate(${c.heading + c.bearing}deg)`

      // camera: centrul hartii in fata camionului, ca sa se vada drumul care urmeaza
      if (s.started && s.follow && c.pos) {
        if (!zoomed.current) {
          zoomed.current = true
          map.setZoom(NAV_ZOOM, { animate: false })
        }
        const size = map.getSize()
        const viewport = map.getContainer().parentElement?.clientHeight ?? size.y
        const shiftPx = s.tilted ? size.y / 2 - TILT_TRUCK_FROM_BOTTOM * viewport
          : s.rotated ? (TRUCK_SCREEN_Y - 0.5) * size.y : 0
        const mPerPx = (156_543.03 * Math.cos((c.pos.lat * Math.PI) / 180)) / 2 ** map.getZoom()
        const center = shiftPx ? ahead(c.pos, -c.bearing, shiftPx * mPerPx) : c.pos
        const key = `${center.lat.toFixed(7)},${center.lng.toFixed(7)},${size.y}`
        if (key !== c.center) {
          c.center = key
          map.setView([center.lat, center.lng], map.getZoom(), { animate: false })
        }
      }
      if (!s.started) zoomed.current = false
      raf = requestAnimationFrame(frame)
    }

    raf = requestAnimationFrame(frame)
    return () => {
      cancelAnimationFrame(raf)
      marker.remove()
    }
  }, [map])

  // strat propriu deasupra traseului verde (care se poate incarca dupa); il punem langa celelalte
  // straturi, in panoul care se roteste, altfel n-ar urma rotirea hartii
  if (!map.getPane(NAV_PANE)) {
    const pane = map.createPane(NAV_PANE, map.getPane('overlayPane')?.parentElement ?? undefined)
    pane.style.zIndex = '450'
  }

  if (!n.leg) return null
  return (
    <>
      <Polyline pane={NAV_PANE} positions={n.leg.geometrie} pathOptions={{ color: '#fff', weight: 12, opacity: 0.9 }} />
      <Polyline pane={NAV_PANE} positions={n.leg.geometrie} pathOptions={{ color: '#2463eb', weight: 7 }} />
      {n.next && n.next.tip !== 'sosire' && (
        <CircleMarker pane={NAV_PANE} center={[n.next.lat, n.next.lng]} radius={6}
          pathOptions={{ color: '#fff', weight: 2, fillColor: '#2463eb', fillOpacity: 1 }} />
      )}
    </>
  )
}

// ---------- bara de sus, fixa, ca la Waze ----------

const MANEVRA: Record<NavStep['tip'], string> = {
  start: 'Pornește', inainte: 'Continuă înainte', 'usor-dreapta': 'Ușor la dreapta', dreapta: 'Virează la dreapta',
  'usor-stanga': 'Ușor la stânga', stanga: 'Virează la stânga', intoarcere: 'Întoarce-te', sosire: 'Destinația'
}

export function NavTopBar({ n }: { n: NavState }) {
  const on = n.started && !!n.leg && !!n.stop

  // continutul paginii coboara sub bara cat timp e afisata
  useEffect(() => {
    document.body.classList.toggle('nav-active', on)
    return () => document.body.classList.remove('nav-active')
  }, [on])

  if (!on || !n.stop) return null

  if (n.arrived || !n.next) {
    return (
      <div className="nav-topbar arrived" role="status" aria-live="polite">
        <div className="nav-topbar-main">
          <div className="nav-topbar-icon"><Flag size={40} /></div>
          <div className="nav-topbar-text">
            <span className="nav-topbar-action">Ai ajuns</span>
            <strong>{n.stop.tip === 'depou' ? 'La depou' : n.stop.nume}</strong>
          </div>
        </div>
        {n.stop.tip !== 'depou' && <div className="nav-topbar-then">Golește ghena, apoi apasă „Am golit ghena”</div>}
      </div>
    )
  }

  const next = n.next
  const strada = next.tip === 'sosire' ? n.stop.nume : next.strada
  const showThen = n.then && n.then.tip !== 'sosire' && next.distanta_m <= 400

  return (
    <div className="nav-topbar" role="status" aria-live="polite">
      <div className="nav-topbar-main">
        <div className="nav-topbar-icon"><ManeuverIcon tip={next.tip} size={44} /></div>
        <div className="nav-topbar-text">
          <span className="nav-topbar-distance">{formatDistance(n.toNext)}</span>
          <span className="nav-topbar-action">{MANEVRA[next.tip]}</span>
          <strong>{strada}</strong>
        </div>
        <div className="nav-topbar-eta">
          <strong>{n.etaMin} min</strong>
          <small>{formatDistance(n.remaining)}</small>
        </div>
      </div>
      {showThen && n.then && (
        <div className="nav-topbar-then">
          Apoi <ManeuverIcon tip={n.then.tip} size={16} /> {n.then.strada}
        </div>
      )}
    </div>
  )
}
