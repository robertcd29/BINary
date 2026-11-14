// Aplicatia soferului: navigatie pas cu pas pe ruta zilei si lista opririlor.
import { useCallback, useEffect, useRef, useState } from 'react'
import { Check, Truck } from 'lucide-react'
import { CircleMarker, MapContainer, Polyline, Popup, TileLayer, useMap } from 'react-leaflet'

import {
  getDriverRoute, markAlertCollected, markStopEmptied, sendDriverPosition,
  type DriverRoute, type NavStop, type RouteStop
} from '../api'
import { fillColor, formatDistance, TIMISOARA, useGeolocation } from '../lib'
import { NavCard, NavLayer, type NavState, useNavigation } from '../Navigatie'

export const TRUCK_COLORS = ['#2b59d9', '#7a4bc2', '#0e8c82']
const SEND_EVERY_MS = 5000

function FitRoute({ route }: { route: [number, number][] }) {
  const map = useMap()
  useEffect(() => {
    if (route.length > 1) map.fitBounds(route, { padding: [28, 28] })
  }, [route, map])
  return null
}

export function useDriver(truckId: string) {
  const geo = useGeolocation(true)
  const navigation = useNavigation(truckId, geo.position, true)
  const [route, setRoute] = useState<DriverRoute | null>(null)
  const [error, setError] = useState('')

  const loadRoute = useCallback(() => getDriverRoute(truckId)
    .then(r => { setRoute(r); setError('') })
    .catch(e => { setRoute(null); setError(e instanceof Error ? e.message : 'Nu am putut încărca ruta.') }), [truckId])

  useEffect(() => { loadRoute() }, [loadRoute])

  // pozitia ajunge la dispecer, cel mult o data la 5 secunde
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

  return (
    <div className="screen screen-pad driver">
      {!n.started && (
        <div className="route-strip">
          <div><strong>{d.route ? d.route.opriri.filter(o => !o.facuta).length : '–'}</strong><span>opriri rămase</span></div>
          <div><strong>{d.route ? formatDistance(d.route.distanta_m) : '–'}</strong><span>traseu total</span></div>
          <div><strong>{d.route ? `${Math.round(d.route.incarcare_l).toLocaleString('ro-RO')} L` : '–'}</strong><span>de colectat</span></div>
        </div>
      )}

      <NavCard n={n} onComplete={(s: NavStop) => d.complete(s)} />

      <section className={`map-panel${n.started ? ' navigating' : ''}${n.tilted ? ' tilted' : ''}`}>
        <div className="map-viewport">
          <MapContainer
            center={[TIMISOARA.lat, TIMISOARA.lng]}
            zoom={13}
            scrollWheelZoom={false}
            zoomControl={false}
            // leaflet-rotate: harta se poate roti pe directia de mers; rotirea o face doar navigatia
            {...({ rotate: true, touchRotate: false, shiftKeyRotate: false, rotateControl: false } as object)}
          >
            <TileLayer attribution="© OpenStreetMap" url="https://tile.openstreetmap.org/{z}/{x}/{y}.png" className="tiles-soft" />
            {d.route && <FitRoute route={d.route.traseu} />}
            {d.route && d.route.traseu.length > 1 && (
              <Polyline positions={d.route.traseu} pathOptions={{ color, weight: 5, opacity: 0.55 }} />
            )}
            {d.route?.opriri.map((s, i) => (
              <CircleMarker key={s.id} center={[s.lat, s.lng]} radius={9}
                pathOptions={{
                  color: '#fff', weight: 3,
                  fillColor: s.facuta ? '#9aa19e' : s.tip === 'depozitare' ? '#ef5a17' : color,
                  fillOpacity: 1
                }}>
                <Popup><strong>{i + 1}. {s.nume}</strong><br />{s.adresa ?? ''}</Popup>
              </CircleMarker>
            ))}
            <NavLayer n={n} />
          </MapContainer>
        </div>
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
