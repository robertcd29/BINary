// Aplicatia soferului: navigatie pas cu pas pe ruta zilei si lista opririlor.
import { useCallback, useEffect, useRef, useState } from 'react'
import { Check, Truck } from 'lucide-react'
import L from 'leaflet'
import { MapContainer, Marker, Polyline, Popup, useMap } from 'react-leaflet'
import BaseMap from '../BaseMap'

import {
  getDriverRoute, markAlertCollected, markStopEmptied, sendDriverPosition,
  type DriverRoute, type NavStop, type RouteStop
} from '../api'
import { fillColor, formatDistance, TIMISOARA, useGeolocation } from '../lib'
import { NavCard, NavLayer, type NavState, speak, useNavigation } from '../Navigatie'

export const TRUCK_COLORS = ['#2b59d9', '#7a4bc2', '#0e8c82', '#b5487a', '#5b6b1f', '#c46a1b']   // ca pe dashboard, un camion pe culoare
const SEND_EVERY_MS = 2000     // pozitia ajunge la dispecer la fiecare 2 secunde
const ROUTE_CHECK_MS = 10_000  // cat de des verificam daca dispecerul a pus opriri noi pe ruta

/** Incadreaza ruta in harta, lasand loc pentru statisticile de sus si cardul de jos.
 *  Doar cand se schimba opririle (nu la fiecare reincarcare), ca sa nu mute harta mutata de sofer. */
function FitRoute({ route, stopsKey, off }: { route: [number, number][]; stopsKey: string; off: boolean }) {
  const map = useMap()
  const latest = useRef(route)
  latest.current = route
  useEffect(() => {
    if (off || latest.current.length < 2) return
    const id = setTimeout(() => {
      map.invalidateSize({ pan: false })
      map.fitBounds(latest.current, { paddingTopLeft: [28, 96], paddingBottomRight: [28, 250], maxZoom: 16 })
    }, 80)
    return () => clearTimeout(id)
  }, [stopsKey, off, map])
  return null
}

/** Pin numerotat pentru o oprire: urmatoarea e mai mare si inchisa la culoare, cele facute sunt gri cu bifa. */
function stopIcon(label: string, kind: 'next' | 'todo' | 'done' | 'dump', color: string) {
  const size = kind === 'next' ? 34 : 26
  return L.divIcon({
    className: '',
    html: `<div class="stop-pin ${kind}" style="--c:${color}">${label}</div>`,
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
  })
}

const CHECK_SVG = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>'

export function useDriver(truckId: string) {
  const geo = useGeolocation(true)
  const navigation = useNavigation(truckId, geo.position, true)
  const [route, setRoute] = useState<DriverRoute | null>(null)
  const [error, setError] = useState('')

  const loadRoute = useCallback(() => getDriverRoute(truckId)
    .then(r => { setRoute(r); setError('') })
    .catch(e => { setRoute(null); setError(e instanceof Error ? e.message : 'Nu am putut încărca ruta.') }), [truckId])

  useEffect(() => { loadRoute() }, [loadRoute])

  // un raport nou (sau o reordonare) schimba opririle: reincarcam ruta si navigatia
  const stopsKey = route ? route.opriri.filter(o => !o.facuta).map(o => o.id).join(',') : null
  const lastKey = useRef<string | null>(null)
  const refreshNav = navigation.refresh
  useEffect(() => {
    const id = setInterval(loadRoute, ROUTE_CHECK_MS)
    return () => clearInterval(id)
  }, [loadRoute])
  useEffect(() => {
    if (stopsKey === null) return
    const before = lastKey.current
    lastKey.current = stopsKey
    if (before === null || before === stopsKey) return
    const noi = stopsKey.split(',').filter(id => !before.split(',').includes(id))
    if (noi.length && navigation.started && navigation.voice) {
      speak('Rută actualizată: o oprire nouă')
    }
    refreshNav()
  }, [stopsKey, refreshNav, navigation.started, navigation.voice])

  // pozitia ajunge la dispecer, cel mult o data la 2 secunde
  const lastSent = useRef(0)
  useEffect(() => {
    if (!geo.position || Date.now() - lastSent.current < SEND_EVERY_MS) return
    lastSent.current = Date.now()
    sendDriverPosition(truckId, geo.position).catch(() => { /* reincercam la urmatoarea pozitie */ })
  }, [geo.position, truckId])

  async function complete(stop: { id: string; tip: string }) {
    if (stop.tip === 'depozitare') await markAlertCollected(stop.id)
    else await markStopEmptied(stop.id)
    await loadRoute()
  }

  return { geo, navigation, route, error, complete, loadRoute }
}

export type DriverState = ReturnType<typeof useDriver>

export function DriverNav({ d, truckId }: { d: DriverState; truckId: string }) {
  const n: NavState = d.navigation
  const color = TRUCK_COLORS[Number(truckId) % TRUCK_COLORS.length]
  const r = d.route
  const ramase = r ? r.opriri.filter(o => !o.facuta) : []
  const nextId = n.stop?.id ?? ramase[0]?.id
  const stopsKey = ramase.map(o => o.id).join(',')

  return (
    // inainte de navigatie: harta intre bara de sus si cea de jos, cu statisticile si cardul peste ea;
    // in timpul navigatiei harta ocupa tot ecranul, sus bara cu instructiunea, jos urmatoarea oprire
    <div className={`driver${n.started ? ' fullscreen' : ''}`}>
      <section className={`map-panel${n.started ? ' navigating' : ''}${n.tilted ? ' tilted' : ''}`}>
        <div className="map-viewport">
          <MapContainer
            center={[TIMISOARA.lat, TIMISOARA.lng]}
            zoom={13}
            zoomSnap={0.25}   // incadrarea rutei poate folosi si zoom intre niveluri, ca ruta sa umple harta
            scrollWheelZoom={false}
            zoomControl={false}
            attributionControl={false}
            // leaflet-rotate: harta se poate roti pe directia de mers; rotirea o face doar navigatia
            {...({ rotate: true, touchRotate: false, shiftKeyRotate: false, rotateControl: false } as object)}
          >
            <BaseMap padding={0.35} />
            {r && <FitRoute route={r.traseu} stopsKey={stopsKey} off={n.started} />}
            {r && r.traseu.length > 1 && (
              <>
                <Polyline positions={r.traseu} pathOptions={{ color: '#fff', weight: 9, opacity: 0.9 }} />
                <Polyline positions={r.traseu} pathOptions={{ color, weight: 5, opacity: 0.9 }} />
              </>
            )}
            {r?.opriri.map((s, i) => {
              const kind = s.facuta ? 'done' : s.id === nextId ? 'next' : s.tip === 'depozitare' ? 'dump' : 'todo'
              return (
                <Marker key={s.id} position={[s.lat, s.lng]} zIndexOffset={kind === 'next' ? 1000 : kind === 'done' ? -100 : 0}
                  icon={stopIcon(s.facuta ? CHECK_SVG : String(i + 1), kind, color)}>
                  <Popup><strong>{i + 1}. {s.nume}</strong><br />{s.tip === 'depozitare' ? 'Depozitare ilegală' : s.adresa ?? ''}</Popup>
                </Marker>
              )
            })}
            <NavLayer n={n} />
          </MapContainer>
        </div>

        {!n.started && r && (
          <div className="route-chips">
            <div><strong>{ramase.length}<small> / {r.opriri.length}</small></strong><span>opriri rămase</span></div>
            <div><strong>{formatDistance(r.distanta_m)}</strong><span>traseu total</span></div>
            <div><strong>{Math.round(r.incarcare_l).toLocaleString('ro-RO')} L</strong><span>de colectat</span></div>
          </div>
        )}

        <NavCard n={n} onComplete={(s: NavStop) => d.complete(s)} />
      </section>
    </div>
  )
}

export function DriverStops({ d }: { d: DriverState }) {
  const [busy, setBusy] = useState<string | null>(null)
  const r = d.route

  if (d.error) return <div className="screen screen-pad"><div className="empty"><Truck size={26} /><strong>Nicio rută</strong><p>{d.error}</p></div></div>
  if (!r) return <div className="screen screen-pad"><p className="muted">Se încarcă ruta…</p></div>

  const done = r.opriri.filter(o => o.facuta).length

  async function finish(s: RouteStop) {
    setBusy(s.id)
    try { await d.complete(s); await d.navigation.refresh() } finally { setBusy(null) }
  }

  return (
    <div className="screen screen-pad">
      <header className="screen-head">
        <h1>Opririle zilei</h1>
        <p>{done} din {r.opriri.length} gata · {formatDistance(r.distanta_m)} în total</p>
      </header>
      <div className="progress-lg"><span style={{ width: `${r.opriri.length ? (done / r.opriri.length) * 100 : 0}%` }} /></div>

      {!r.opriri.length && <div className="empty"><strong>Nicio oprire azi</strong><p>Camionul rămâne în depou.</p></div>}
      <ol className="stops">
        {r.opriri.map((s, i) => (
          <li key={s.id} className={`card stop${s.facuta ? ' done' : ''}`}>
            <span className="stop-n">{s.facuta ? <Check size={16} /> : i + 1}</span>
            <div className="stop-body">
              <strong>{s.nume}</strong>
              <span>{s.tip === 'depozitare' ? 'Depozitare ilegală' : s.adresa ?? s.id}</span>
              {s.tip === 'pubela' && s.umplere != null && (
                <div className="stop-fill">
                  <div className="fill-meter small"><span style={{ width: `${s.umplere}%`, background: fillColor(s.umplere) }} /></div>
                  <small>{Math.round(s.umplere)}%</small>
                </div>
              )}
            </div>
            {s.facuta
              ? <span className="pill ok">Gata</span>
              : <button className="btn btn-small" disabled={busy === s.id} onClick={() => finish(s)}>
                  {s.tip === 'depozitare' ? 'Colectată' : 'Golită'}
                </button>}
          </li>
        ))}
      </ol>
    </div>
  )
}
