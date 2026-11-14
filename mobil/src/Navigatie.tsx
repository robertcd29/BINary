// Navigatie pas cu pas pentru sofer, ca un GPS: instructiunea urmatoare, distanta pana la viraj,
// voce in romana, harta care urmareste camionul si recalcularea traseului cand iese de pe el.
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import L from 'leaflet'
import { CircleMarker, Polyline, useMap } from 'react-leaflet'
import {
  ArrowUp, ArrowUpLeft, ArrowUpRight, Compass, CornerUpLeft, CornerUpRight, Flag, LocateFixed,
  Navigation2, Play, Square, Trash2, TriangleAlert, Undo2, Volume2, VolumeX
} from 'lucide-react'

type RotatableMap = L.Map & { setBearing?: (deg: number) => void }
type ViewMode = '3d' | '2d' | 'nord'
const VIEW_LABEL: Record<ViewMode, string> = {
  '3d': 'înclinată, pe direcția de mers', '2d': 'de sus, pe direcția de mers', nord: 'de sus, nordul în sus'
}

import {
  getNavigation, isTransient, sendDriverPosition,
  type Coordinates, type LaneDir, type Navigation, type NavStep, type NavStop
} from './api'

const OFF_ROUTE_M = 50          // mai departe de atat de traseu = a luat-o pe alt drum
const OFF_ROUTE_FOR_MS = 4000   // ...si a ramas departe atata timp (un salt de GPS nu recalculeaza)
const BAD_GPS_M = 50            // o pozitie cu precizie mai slaba de atat nu declanseaza recalcularea
const PRE_ANNOUNCE_M = 450      // pe drumurile lungi, manevra se anunta o data si de la aceasta distanta
const MOVE_HEADING_M = 8        // directia de mers reala: din pozitii GPS aflate la cel putin atatia metri
const HEADING_FRESH_MS = 20_000 // ...si nu mai vechi de atat (oprit la semafor de mult = nu mai stim sigur)
const DEPOT_STOP_MS = 4000      // la depou: navigatia se opreste singura dupa anuntul vocal
const RESUME_MAX_MS = 4 * 3600_000 // navigatia pornita se reia singura dupa o reincarcare a paginii, pana la 4 ore
const REROUTE_EVERY_MS = 10_000 // nu recalculam mai des de atat
const ARRIVED_M = 30            // la 30 m de oprire consideram ca a ajuns
const ANNOUNCE_M = 150          // virajul se anunta cu vocea de la aceasta distanta
const LANES_M = 500             // benzile apar (si se anunta) de la aceasta distanta de intersectie
const TRUCK_SPEED_MS = 5.5      // ~20 km/h, viteza medie a unui camion de gunoi prin oras
const SIM_STEP_M = 20           // simulare: cati metri inainteaza camionul la fiecare pas (~180 km/h, ca demo-ul sa nu dureze)
const SIM_TICK_MS = 400
const LOOK_AHEAD_M = 25         // directia de mers se ia spre un punct aflat atat de departe pe traseu
const NAV_ZOOM = 18           // zoom-ul in navigatie: de aproape, ca strada si intersectia sa se vada bine
const TRUCK_SCREEN_Y = 0.62     // camionul sta la 62% din inaltimea hartii: vezi drumul din fata, deasupra cardului de jos

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

/** Sensul giratoriu vazut de sus, ca pe indicator: intrarea jos, se merge in sens invers acelor de
 *  ceasornic (ca in Romania), iar sageata iese pe directia iesirii; numarul iesirii in mijloc. */
function RoundaboutIcon({ iesire, unghi = 0, size }: { iesire?: number; unghi?: number; size: number }) {
  const c = 12, r = 5.2, R = 11.2
  const at = (deg: number, rad: number) => [c + rad * Math.sin((deg * Math.PI) / 180), c - rad * Math.cos((deg * Math.PI) / 180)]
  // unghi = 180 - cat din cerc parcurge; limitat ca o iesire imediata sau o intoarcere sa se vada clar
  const a = Math.max(-170, Math.min(135, unghi))
  const sweep = 180 - a                                        // cat din cerc parcurge, in grade
  const [qx, qy] = at(a, r)
  const [ox, oy] = at(a, R - 1.5)
  const head = (d: number) => at(a, R - 1.5).map((v, i) => v + 4 * [Math.sin(((a + d) * Math.PI) / 180), -Math.cos(((a + d) * Math.PI) / 180)][i])
  const [h1x, h1y] = head(145), [h2x, h2y] = head(-145)
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx={c} cy={c} r={r} strokeWidth="1.6" opacity="0.4" />
      <path d={`M${c} 23V${c + r}`} strokeWidth="2.4" />
      <path d={`M${c} ${c + r}A${r} ${r} 0 ${sweep > 180 ? 1 : 0} 0 ${qx.toFixed(2)} ${qy.toFixed(2)}`} strokeWidth="2.4" />
      <path d={`M${qx.toFixed(2)} ${qy.toFixed(2)}L${ox.toFixed(2)} ${oy.toFixed(2)}M${h1x.toFixed(2)} ${h1y.toFixed(2)}L${ox.toFixed(2)} ${oy.toFixed(2)}L${h2x.toFixed(2)} ${h2y.toFixed(2)}`} strokeWidth="2.4" />
      {!!iesire && <text x={c} y={c + 2.3} textAnchor="middle" fontSize="6.5" fontWeight="800" fill="currentColor" stroke="none">{iesire}</text>}
    </svg>
  )
}

function ManeuverIcon({ tip, size = 34, step }: { tip: NavStep['tip']; size?: number; step?: NavStep | null }) {
  switch (tip) {
    case 'giratoriu':
    case 'iesire-giratoriu': return <RoundaboutIcon iesire={step?.iesire} unghi={step?.unghi} size={size} />
    case 'dreapta': return <CornerUpRight size={size} />
    case 'stanga': return <CornerUpLeft size={size} />
    case 'usor-dreapta': return <ArrowUpRight size={size} />
    case 'usor-stanga': return <ArrowUpLeft size={size} />
    case 'intoarcere': return <Undo2 size={size} />
    case 'sosire': return <Flag size={size} />
    default: return <ArrowUp size={size} />
  }
}

export function speak(text: string) {
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

// pe iPhone vederea 3D (harta culcata cu CSS) cere multa memorie: pornim in 2D si nu reluam singuri 3D
// dupa o reincarcare, ca un telefon care nu o duce sa nu intre intr-o bucla de blocari
const IOS = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1)
const DEFAULT_VIEW: ViewMode = IOS ? '2d' : '3d'

type SavedNav = { started: boolean; voice: boolean; viewMode: ViewMode; at: number }
const navKey = (truckId: string) => `binary.navigatie.${truckId}`
function loadSaved(truckId: string): SavedNav | null {
  try {
    const v = JSON.parse(localStorage.getItem(navKey(truckId)) || 'null') as SavedNav | null
    return v && Date.now() - v.at < RESUME_MAX_MS ? v : null
  } catch { return null }
}

export function useNavigation(truckId: string, gps: Coordinates | null, enabled: boolean, accuracy: number | null = null) {
  // navigatia pornita e tinuta minte pe telefon: daca pagina se reincarca (ecran stins, telefonul
  // inchide tab-ul din fundal, login reinnoit) soferul revine direct in navigatie
  const [saved] = useState(() => loadSaved(truckId))
  const [nav, setNav] = useState<Navigation | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [started, setStarted] = useState(false)
  const [voice, setVoice] = useState(saved?.voice ?? true)
  const [follow, setFollow] = useState(true)
  const [simulating, setSimulating] = useState(false)
  const [simPos, setSimPos] = useState<Coordinates | null>(null)

  // 3d = inclinata si rotita ca la Waze, 2d = rotita pe directia de mers, nord = nordul in sus
  const [viewMode, setViewMode] = useState<ViewMode>(
    saved?.viewMode && !(IOS && saved.viewMode === '3d') ? saved.viewMode : DEFAULT_VIEW)
  const lastHeading = useRef(0)

  const position = simulating ? simPos : gps
  const positionRef = useRef(position)
  positionRef.current = position
  const lastReroute = useRef(0)

  // directia reala de mers, din ultimele pozitii (nu din traseu: cand a iesit de pe traseu, traseul minte)
  const moveAnchor = useRef<Coordinates | null>(null)
  const moveHeading = useRef<{ deg: number; at: number } | null>(null)
  useEffect(() => {
    if (!position) return
    const a = moveAnchor.current
    if (!a) { moveAnchor.current = position; return }
    if (meters([a.lat, a.lng], [position.lat, position.lng]) >= MOVE_HEADING_M) {
      moveHeading.current = { deg: bearing(a, position), at: Date.now() }
      moveAnchor.current = position
    }
  }, [position])

  const hasNav = useRef(false)
  const refresh = useCallback(async (from?: Coordinates | null) => {
    setLoading(true)
    try {
      const h = moveHeading.current
      const heading = h && Date.now() - h.at < HEADING_FRESH_MS ? h.deg : null
      setNav(await getNavigation(truckId, from ?? positionRef.current, heading))
      hasNav.current = true
      setError('')
    } catch (e) {
      // serverul a cazut cateva secunde (ex. 502 la un deploy): soferul ramane pe navigatia pe care o are
      if (hasNav.current && isTransient(e)) return
      // alta eroare: cu o navigatie deja pe ecran, ea ramane, iar mesajul apare mic in card
      setError(e instanceof Error ? e.message : 'Nu s-a putut calcula navigația.')
    } finally {
      setLoading(false)
    }
  }, [truckId])

  useEffect(() => {
    if (!enabled) return
    hasNav.current = false
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
  const offSince = useRef<number | null>(null)
  useEffect(() => {
    if (!position || !leg) return
    const badGps = !simulating && accuracy !== null && accuracy > BAD_GPS_M
    if (progress.off < OFF_ROUTE_M || badGps) {
      if (progress.off < OFF_ROUTE_M) offSince.current = null
      return
    }
    // doar daca a ramas departe de traseu cateva secunde (nu la un salt de GPS intre blocuri)
    if (offSince.current === null) offSince.current = Date.now()
    if (started && Date.now() - offSince.current < OFF_ROUTE_FOR_MS) return
    if (Date.now() - lastReroute.current < REROUTE_EVERY_MS) return
    lastReroute.current = Date.now()
    offSince.current = null
    if (started && voice) speak('Recalculez traseul')
    refresh(position)
  }, [started, position, leg, progress.off, voice, refresh, accuracy, simulating])

  // vocea: fiecare manevra se anunta o data cand se apropie, iar sosirea o data
  const announced = useRef(new Set<string>())
  useEffect(() => { announced.current.clear() }, [leg])
  useEffect(() => {
    if (!started || !voice || !next) return
    const key = `${stepIndex + 1}`
    const lung = (next.la_m - (current?.la_m ?? 0)) > PRE_ANNOUNCE_M + 250   // drum lung pana la manevra
    if (toNext <= ANNOUNCE_M && !announced.current.has(key) && !arrived) {
      announced.current.add(key)
      speak(next.tip === 'sosire' ? `Ajungi la ${next.strada}` : next.text)
    } else if (next.benzi_text && toNext <= LANES_M && toNext > ANNOUNCE_M + 50 && !announced.current.has(`b${key}`)) {
      announced.current.add(`b${key}`)
      announced.current.add(`p${key}`)
      speak(`${next.benzi_text}, apoi ${next.text.charAt(0).toLowerCase()}${next.text.slice(1)}`)
    } else if (lung && next.tip !== 'sosire' && toNext <= PRE_ANNOUNCE_M && toNext > ANNOUNCE_M + 100 && !announced.current.has(`p${key}`)) {
      announced.current.add(`p${key}`)
      speak(inDistance(next, Math.round(toNext / 50) * 50))
    }
  }, [started, voice, next, current, stepIndex, toNext, arrived])
  useEffect(() => {
    if (started && voice && arrived && stop && !announced.current.has('sosire')) {
      announced.current.add('sosire')
      speak(stop.tip === 'depou' ? 'Ai ajuns la depou. Traseul de azi e gata.' : `Ai ajuns la ${stop.nume}`)
    }
  }, [started, voice, arrived, stop])

  // inapoi la depou, cu toate opririle facute: navigatia (si simularea) se opresc singure
  const atDepot = started && arrived && stop?.tip === 'depou'
  useEffect(() => {
    if (!atDepot) return
    const id = setTimeout(() => {
      resumePending.current = false
      setSimulating(false)
      setStarted(false)
    }, DEPOT_STOP_MS)
    return () => clearTimeout(id)
  }, [atDepot])

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

  function start(resumed = false) {
    setStarted(true)
    setFollow(true)
    // harta in ecran, sub bara de instructiuni de sus
    setTimeout(() => document.querySelector('.map-panel')?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 100)
    if (voice) speak(resumed ? 'Navigație reluată' : current ? `Navigație pornită. ${current.text}` : 'Navigație pornită')
  }

  // reluare dupa o reincarcare a paginii: cand navigatia a sosit de la server
  const resumePending = useRef(!!saved?.started)
  useEffect(() => {
    if (resumePending.current && nav && leg && !started) {
      resumePending.current = false
      start(true)
    }
  })

  // ce tinem minte pe telefon (si "cand", ca o navigatie uitata de ieri sa nu se reia singura)
  useEffect(() => {
    const save = () => {
      try {
        if (started) localStorage.setItem(navKey(truckId), JSON.stringify({ started, voice, viewMode, at: Date.now() }))
        else if (!resumePending.current) localStorage.removeItem(navKey(truckId))
      } catch { /* fara storage: navigatia merge, doar nu se reia singura */ }
    }
    save()
    if (!started) return
    const id = setInterval(save, 30_000)
    return () => clearInterval(id)
  }, [started, voice, viewMode, truckId])

  // ecranul ramane aprins cat timp merge navigatia (altfel telefonul se blocheaza si poate inchide pagina)
  useEffect(() => {
    if (!started || !('wakeLock' in navigator)) return
    type Lock = { release: () => Promise<void> }
    let lock: Lock | null = null
    let alive = true
    const take = () => {
      if (document.visibilityState !== 'visible') return
      ;(navigator as Navigator & { wakeLock: { request: (t: 'screen') => Promise<Lock> } }).wakeLock
        .request('screen').then(l => { if (alive) lock = l; else l.release() }).catch(() => { /* refuzat: nimic grav */ })
    }
    take()
    // blocarea se pierde cand pagina trece in fundal; o reluam cand revine
    document.addEventListener('visibilitychange', take)
    return () => { alive = false; document.removeEventListener('visibilitychange', take); lock?.release().catch(() => {}) }
  }, [started])

  return {
    nav, leg, stop, error, loading, started, voice, follow, simulating, position, heading,
    viewMode, cycleView: () => setViewMode(m => (m === '3d' ? '2d' : m === '2d' ? 'nord' : '3d')),
    rotated: started && follow && viewMode !== 'nord',
    tilted: started && follow && viewMode === '3d',
    current, next, then: leg?.pasi[stepIndex + 2] ?? null, toNext, remaining, arrived, etaMin, stepIndex,
    refresh, start,
    stop_: () => { resumePending.current = false; setStarted(false); setSimulating(false); window.speechSynthesis?.cancel() },
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
  const [failed, setFailed] = useState('')

  if (!n.nav || !n.leg || !n.stop) {
    return (
      <section className="panel nav-card">
        <p className="empty">{n.error || (n.loading ? 'Calculez navigația...' : 'Nu există navigație pentru acest camion.')}</p>
        {n.error && !n.loading && <button className="secondary-button" onClick={() => n.refresh()}>Încearcă din nou</button>}
      </section>
    )
  }

  const ramase = n.nav.opriri.filter(o => o.tip !== 'depou').length

  // toate opririle facute si camionul e la depou: ziua s-a terminat, nu mai e nimic de pornit
  if (!n.started && ramase === 0 && n.remaining <= ARRIVED_M * 2) {
    return (
      <section className="panel nav-card preview">
        <span className="sheet-handle" aria-hidden="true" />
        <div className="nav-destination">
          <span className="dest-badge depou" aria-hidden="true"><Flag size={20} /></span>
          <div>
            <small>Toate opririle sunt făcute</small>
            <strong>Traseul de azi e gata</strong>
            <small>Camionul e la depou</small>
          </div>
        </div>
      </section>
    )
  }

  async function done() {
    if (!n.stop) return
    setBusy(true)
    setFailed('')
    try {
      await onComplete(n.stop)
    } catch (e) {
      // fara mesaj, butonul doar revenea ca inainte si parea ca nu face nimic
      setFailed(e instanceof Error ? e.message : 'Nu am putut salva. Încearcă din nou.')
      setBusy(false)
      return
    }
    try {
      await n.refresh(n.position)
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className={`panel nav-card${n.started ? '' : ' preview'}`}>
      {!n.started && <span className="sheet-handle" aria-hidden="true" />}
      <div className="nav-destination">
        {!n.started && (
          <span className={`dest-badge ${n.stop.tip}`} aria-hidden="true">
            {n.stop.tip === 'depou' ? <Flag size={20} /> : n.stop.tip === 'depozitare' ? <TriangleAlert size={20} /> : <Trash2 size={20} />}
          </span>
        )}
        <div>
          <small>{n.stop.tip === 'depou' ? 'Ultima etapă' : `${n.started ? 'Următoarea oprire' : 'Prima oprire'} · ${ramase} rămase`}</small>
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
          <button className="primary-button" onClick={() => n.start()}><Navigation2 size={17} /> Pornește navigația</button>
        ) : n.stop.tip !== 'depou' ? (
          <button className="primary-button" disabled={busy} onClick={done}>
            {busy ? 'Se salvează…' : `${n.stop.tip === 'depozitare' ? 'Am colectat' : 'Am golit ghena'} · următoarea`}
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

      {failed && <p className="nav-error" role="alert">{failed}</p>}
      {!failed && n.error && <p className="nav-error" role="status">{n.error}</p>}

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
const TILT_TRUCK_FROM_BOTTOM = 0.34 // vederea 3D: camionul la 34% din inaltime, de jos (deasupra cardului cu oprirea)
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
  const cam = useRef({ pos: null as Coordinates | null, heading: 0, bearing: 0, last: null as Coordinates | null, size: 0 })
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
      // sub 0,2 grade nu se vede: nu mai redesenam harta pentru atat (pe telefon conteaza)
      if (Math.abs(angleDelta(c.bearing, nb)) > 0.2) {
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
        // harta se muta doar cand diferenta se vede (peste ~0,5 px), nu la fiecare cadru
        const last = c.last
        if (!last || c.size !== size.y || meters([last.lat, last.lng], [center.lat, center.lng]) > 0.5 * mPerPx) {
          c.last = center
          c.size = size.y
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

// ---------- benzi: marcajele de pe asfalt, ca pe indicatoarele de deasupra drumului ----------

const LANE_PATHS: Record<LaneDir, string> = {
  through: 'M12 22V5 M7.5 9.5 12 5l4.5 4.5',
  left: 'M12 22v-8a4 4 0 0 0-4-4H5 M8.5 6.5 5 10l3.5 3.5',
  sharp_left: 'M12 22V9 M12 9 5.5 15.5 M5.5 10.5v5h5',
  right: 'M12 22v-8a4 4 0 0 1 4-4h3 M15.5 6.5 19 10l-3.5 3.5',
  sharp_right: 'M12 22V9 M12 9l6.5 6.5 M18.5 10.5v5h-5',
  slight_left: 'M12 22v-7L6.5 8.5 M6.5 13.5v-5h5',
  slight_right: 'M12 22v-7l5.5-6.5 M17.5 13.5v-5h-5',
  reverse: 'M12 22V10a3.5 3.5 0 0 0-7 0v5 M2 12l3 3.5L8 12',
}

// ce sageti de pe banda corespund manevrei (cele aprinse)
const GOOD_DIRS: Record<NavStep['tip'], LaneDir[]> = {
  stanga: ['left', 'sharp_left'], 'usor-stanga': ['slight_left', 'left', 'through'],
  dreapta: ['right', 'sharp_right'], 'usor-dreapta': ['slight_right', 'right', 'through'],
  inainte: ['through', 'slight_left', 'slight_right'], intoarcere: ['reverse', 'left'], start: [], sosire: [],
  giratoriu: [], 'iesire-giratoriu': [],
}

function goodDirs(tip: NavStep['tip'], dirs: LaneDir[]) {
  const ok = dirs.filter(d => GOOD_DIRS[tip].includes(d))
  return ok.length ? [ok[0]] : dirs
}

function LaneIcon({ dirs, good }: { dirs: LaneDir[]; good: LaneDir[] }) {
  // sagetile care nu ne trebuie se deseneaza intai, estompate; cea buna deasupra
  const order = [...dirs].sort((a, b) => Number(good.includes(a)) - Number(good.includes(b)))
  return (
    <svg viewBox="0 0 24 24" width="30" height="30" fill="none" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {order.map(d => <path key={d} d={LANE_PATHS[d]} stroke={good.includes(d) ? '#fff' : '#ffffff47'} />)}
    </svg>
  )
}

// ---------- bara de sus, fixa, ca la Waze ----------

const MANEVRA: Record<NavStep['tip'], string> = {
  start: 'Pornește', inainte: 'Continuă înainte', 'usor-dreapta': 'Ușor la dreapta', dreapta: 'Virează la dreapta',
  'usor-stanga': 'Ușor la stânga', stanga: 'Virează la stânga', intoarcere: 'Întoarce-te', sosire: 'Destinația',
  giratoriu: 'Sens giratoriu', 'iesire-giratoriu': 'Ieși din giratoriu'
}

function actiune(step: NavStep) {
  if (step.tip === 'giratoriu' && step.iesire) return `Sens giratoriu · ieșirea ${step.iesire}`
  if (step.tip === 'iesire-giratoriu' && step.iesire) return `Ieșirea ${step.iesire} din giratoriu`
  return MANEVRA[step.tip]
}

export function NavTopBar({ n }: { n: NavState }) {
  const on = n.started && !!n.leg && !!n.stop

  // continutul paginii coboara sub bara cat timp e afisata
  useEffect(() => {
    document.body.classList.toggle('nav-active', on)
    return () => document.body.classList.remove('nav-active')
  }, [on])
  // ...exact cat e de inalta bara (creste cand apar benzile), ca sa nu acopere nimic
  const bar = useRef<HTMLDivElement>(null)
  useLayoutEffect(() => {
    if (bar.current) document.body.style.setProperty('--nav-bar-h', `${bar.current.offsetHeight}px`)
  })

  if (!on || !n.stop) return null

  if (n.arrived || !n.next) {
    return (
      <div ref={bar} className="nav-topbar arrived" role="status" aria-live="polite">
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
  const showLanes = !!next.benzi && n.toNext <= LANES_M
  const showThen = !showLanes && n.then && n.then.tip !== 'sosire' && n.then.tip !== 'iesire-giratoriu' && next.distanta_m <= 400

  return (
    <div ref={bar} className="nav-topbar" role="status" aria-live="polite">
      <div className="nav-topbar-main">
        <div className="nav-topbar-icon"><ManeuverIcon tip={next.tip} step={next} size={44} /></div>
        <div className="nav-topbar-text">
          <span className="nav-topbar-distance">{formatDistance(n.toNext)}</span>
          <span className="nav-topbar-action">{actiune(next)}</span>
          <strong>{strada}</strong>
        </div>
        <div className="nav-topbar-eta">
          <strong>{n.etaMin} min</strong>
          <small>{formatDistance(n.remaining)}</small>
        </div>
      </div>
      {showLanes && next.benzi && (
        <div className="lanes" aria-label={next.benzi_text}>
          <div className="lanes-road">
            {next.benzi.map((b, i) => (
              <span key={i} className={`lane${b.buna ? ' good' : ''}`}>
                <LaneIcon dirs={b.directii} good={b.buna ? goodDirs(next.tip, b.directii) : []} />
              </span>
            ))}
          </div>
          <span className="lanes-text">{next.benzi_text}</span>
        </div>
      )}
      {showThen && n.then && (
        <div className="nav-topbar-then">
          Apoi <ManeuverIcon tip={n.then.tip} step={n.then} size={16} /> {n.then.strada}
        </div>
      )}
    </div>
  )
}
