import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import {
  Camera,
  Check,
  ChevronRight,
  ClipboardList,
  MapPin,
  Navigation,
  Recycle,
  Truck,
  UserRound,
  AlertCircle,
  LocateFixed
} from 'lucide-react'

import {
  CircleMarker,
  MapContainer,
  Polyline,
  TileLayer,
  Popup,
  useMap
} from 'react-leaflet'

import {
  DEMO_MODE,
  getDriverRoute,
  markAlertCollected,
  markStopEmptied,
  sendDriverPosition,
  submitReport,
  type Coordinates,
  type DriverRoute,
  type Report,
  type RouteStop,
  type Role,
  type NavStop
} from './api'
import { NavCard, NavLayer, NavTopBar, useNavigation } from './Navigatie'


function MapFocus({ point, route }: { point: Coordinates | null; route: Coordinates[] }) {
  const map = useMap()
  const centeredOnGps = useRef(false)

  // tot traseul in ecran cand se incarca ruta
  useEffect(() => {
    if (route.length > 1) {
      map.fitBounds(route.map(p => [p.lat, p.lng] as [number, number]), { padding: [24, 24] })
    }
  }, [route, map])

  // pe pozitia soferului doar o data: altfel fiecare citire GPS ar trage harta inapoi cand o misti
  useEffect(() => {
    if (point && !centeredOnGps.current) {
      centeredOnGps.current = true
      map.flyTo([point.lat, point.lng], 16, { duration: 0.8 })
    }
  }, [point, map])

  // panoul isi schimba marimea dupa ce apare harta (titlu, statistici), deci recalculam dalele
  useEffect(() => {
    const t = setTimeout(() => map.invalidateSize(), 200)
    return () => clearTimeout(t)
  }, [map])

  return null
}


export default function App() {
  const [role, setRole] = useState<Role>('citizen')
  const [reports, setReports] = useState<Report[]>([])
  const [description, setDescription] = useState('')
  const [photo, setPhoto] = useState<File | undefined>()
  const fileInputRef = useRef<HTMLInputElement>(null)

  const [location, setLocation] = useState<Coordinates | null>(null)
  const [gpsMessage, setGpsMessage] = useState(
    'Locația nu a fost încă preluată.'
  )

  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [driverId, setDriverId] = useState('0')
  const [routePlan, setRoutePlan] = useState<DriverRoute | null>(null)
  const [stops, setStops] = useState<RouteStop[]>([])
  const [driverPosition, setDriverPosition] = useState<Coordinates | null>(null)
  const [routeLoading, setRouteLoading] = useState(false)


  useEffect(() => {
    if (role !== 'driver') return

    let cancelled = false
    setRouteLoading(true)
    setNotice('')

    getDriverRoute(driverId)
      .then(route => {
        if (cancelled) return
        setRoutePlan(route)
        setStops(route.stopDetails)
      })
      .catch(error => {
        if (!cancelled) setNotice(
          error instanceof Error ? error.message : 'Nu s-a putut încărca ruta.'
        )
      })
      .finally(() => {
        if (!cancelled) setRouteLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [role, driverId])


  useEffect(() => {
    if (role !== 'driver' || !navigator.geolocation) return

    const id = navigator.geolocation.watchPosition(
      p => {
        const pos = {
          lat: p.coords.latitude,
          lng: p.coords.longitude
        }

        setDriverPosition(pos)

        sendDriverPosition(driverId, pos)
          .catch(e => setNotice(e.message))
      },
      e => setNotice(`Nu am putut prelua GPS-ul: ${e.message}`),
      {
        enableHighAccuracy: true,
        maximumAge: 5000,
        timeout: 15000
      }
    )

    return () => navigator.geolocation.clearWatch(id)
  }, [role, driverId])


  const mapCenter = useMemo(
    () => driverPosition ?? routePlan?.geometry[0] ?? location ?? {
      lat: 45.7538,
      lng: 21.2257
    },
    [driverPosition, routePlan, location]
  )


  function getLocation() {
    if (!navigator.geolocation) {
      setGpsMessage('Browserul nu suportă localizarea.')
      return
    }

    setGpsMessage('Se caută poziția GPS...')

    navigator.geolocation.getCurrentPosition(
      p => {
        setLocation({
          lat: p.coords.latitude,
          lng: p.coords.longitude
        })

        setGpsMessage(
          `Locație preluată (precizie aproximativă ${Math.round(p.coords.accuracy)} m).`
        )
      },
      e => setGpsMessage(`Nu am putut prelua locația: ${e.message}`),
      {
        enableHighAccuracy: true,
        timeout: 15000,
        maximumAge: 0
      }
    )
  }


  async function sendReport(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()

    if (!location) {
      setNotice('Preia locația GPS înainte de trimitere.')
      return
    }

    if (!photo) {
      setNotice('Adaugă o fotografie înainte de trimitere.')
      return
    }

    setBusy(true)
    setNotice('')

    try {
      const result = await submitReport({
        description,
        location,
        photo
      })

      const report: Report = {
        id: result.id,
        description,
        location,
        photoName: photo.name,
        status: result.status,
        createdAt: new Date().toLocaleString('ro-RO')
      }

      setReports(prev => [report, ...prev])
      setDescription('')
      setPhoto(undefined)

      if (fileInputRef.current) {
        fileInputRef.current.value = ''
      }

      setNotice(
        DEMO_MODE
          ? 'Raportare salvată doar în sesiunea demonstrativă. Nu a fost trimisă la server.'
          : 'Raportarea a fost trimisă către server.'
      )
    } catch (err) {
      setNotice(
        err instanceof Error ? err.message : 'Eroare la trimitere.'
      )
    } finally {
      setBusy(false)
    }
  }


  const navigation = useNavigation(driverId, driverPosition, role === 'driver')

  // butonul din cardul de navigatie: aceeasi actiune ca in lista de opriri
  async function completeNavStop(stop: NavStop) {
    const inList = stops.find(item => item.id === stop.id)
    if (inList) return completeStop(inList)
    if (stop.tip === 'depozitare') await markAlertCollected(stop.id)
    else await markStopEmptied(stop.id)
  }


  async function completeStop(stop: RouteStop) {
    try {
      if (stop.type === 'alerta') {
        await markAlertCollected(stop.id)
      } else {
        await markStopEmptied(stop.id)
      }

      setStops(prev =>
        prev.map(item =>
          item.id === stop.id
            ? { ...item, done: true, fill: 0 }
            : item
        )
      )

      setNotice(
        DEMO_MODE
          ? `${stop.name}: modificare demonstrativă, nesalvată pe server.`
          : `${stop.name} a fost actualizată.`
      )
    } catch (err) {
      setNotice(
        err instanceof Error
          ? err.message
          : 'Nu s-a putut actualiza oprirea.'
      )
    }
  }


  return (
    <div className="app-shell">
      {role === 'driver' && <NavTopBar n={navigation} />}
      <header className="topbar">
        <div className="brand">
          <div className="brand-icon"><Recycle size={22} /></div>
          <div>
            <strong>BINary</strong>
            <small>Oraș mai curat, împreună.</small>
          </div>
        </div>
        <div className="demo-pill">
          {DEMO_MODE ? 'MOD DEMO – RUTĂ TEO' : 'CONECTAT LA SERVER'}
        </div>
      </header>

      <main className="content">
        <section className="welcome">
          <p className="eyebrow">BINary · aplicație mobilă</p>
          <h1>{role === 'citizen' ? 'Raportează. Contribuie.' : 'Ruta ta de astăzi.'}</h1>
          <p>
            {role === 'citizen'
              ? 'Ai observat o problemă? Trimite-ne o fotografie și locația.'
              : 'Urmărește traseul calculat și actualizează opririle.'}
          </p>
        </section>

        <div className="role-switch" aria-label="Alege rolul">
          <button
            className={role === 'citizen' ? 'selected' : ''}
            onClick={() => { setRole('citizen'); setNotice('') }}
          >
            <UserRound size={17} /> Cetățean
          </button>
          <button
            className={role === 'driver' ? 'selected' : ''}
            onClick={() => { setRole('driver'); setNotice('') }}
          >
            <Truck size={17} /> Șofer
          </button>
        </div>

        {notice && (
          <div className="notice">
            <AlertCircle size={17} /> {notice}
          </div>
        )}

        {role === 'citizen' ? (
          <>
            <section className="panel">
              <div className="panel-heading">
                <div><span className="step">01</span><h2>Raportează o problemă</h2></div>
                <ClipboardList size={21} />
              </div>

              <form onSubmit={sendReport} className="form">
                <label htmlFor="description">Descriere</label>
                <textarea
                  id="description"
                  rows={3}
                  placeholder="Ex.: tomberon deteriorat, deșeuri împrăștiate..."
                  value={description}
                  onChange={e => setDescription(e.target.value)}
                  required
                />

                <label>Fotografie</label>
                <button
                  type="button"
                  className="upload"
                  onClick={() => fileInputRef.current?.click()}
                >
                  <Camera size={20} />
                  <span>{photo ? photo.name : 'Adaugă o fotografie'}</span>
                  <ChevronRight size={18} />
                </button>

                <input
                  ref={fileInputRef}
                  id="photo"
                  type="file"
                  accept="image/*"
                  capture="environment"
                  onChange={e => setPhoto(e.target.files?.[0])}
                  style={{ display: 'none' }}
                />

                {photo && (
                  <img
                    className="preview"
                    src={URL.createObjectURL(photo)}
                    alt="Previzualizare fotografie"
                  />
                )}

                <label>Locația problemei</label>
                <button type="button" className="location-button" onClick={getLocation}>
                  <LocateFixed size={18} /> Preia locația GPS
                </button>
                <p className="hint"><MapPin size={14} />{gpsMessage}</p>

                {location && (
                  <div className="coords">
                    {location.lat.toFixed(6)}, {location.lng.toFixed(6)}
                  </div>
                )}

                <button className="primary" type="submit" disabled={busy}>
                  {busy ? 'Se salvează...' : 'Trimite raportarea'}
                  <ChevronRight size={18} />
                </button>
              </form>
            </section>

            <section className="panel">
              <div className="panel-heading">
                <div><span className="step">02</span><h2>Raportările mele</h2></div>
                <ClipboardList size={21} />
              </div>

              {reports.length === 0 ? (
                <p className="empty">Nu ai trimis încă nicio raportare. Raportările vor apărea aici.</p>
              ) : (
                reports.map(report => (
                  <article className="report-item" key={report.id}>
                    <div>
                      <strong>{report.description}</strong>
                      <small>{report.createdAt} · {report.id} · {report.photoName}</small>
                    </div>
                    <span className="status">{report.status}</span>
                  </article>
                ))
              )}
            </section>
          </>
        ) : (
          <>
            <section className="panel driver-controls">
              <div>
                <label htmlFor="driverId">Camion</label>
                <select
                  id="driverId"
                  value={driverId}
                  onChange={e => setDriverId(e.target.value)}
                >
                  <option value="0">Camion 0</option>
                  <option value="1">Camion 1</option>
                  <option value="2">Camion 2</option>
                </select>
              </div>
              <div className="gps-state">
                <span className={driverPosition ? 'dot live' : 'dot'} />
                {driverPosition ? 'GPS activ' : 'Aștept permisiunea GPS'}
              </div>
            </section>

            <NavCard n={navigation} onComplete={completeNavStop} />

            <section className={`map-panel${navigation.started ? ' navigating' : ''}${navigation.tilted ? ' tilted' : ''}`}>
              <div className="map-title">
                <div><span className="step">01</span><h2>Traseul camionului {driverId}</h2></div>
                <span>{stops.filter(stop => !stop.done).length} opriri rămase</span>
              </div>

              {routePlan && (
                <div className="route-stats">
                  <div><strong>{(routePlan.distance_m / 1000).toFixed(2)} km</strong><small>Distanță calculată</small></div>
                  <div><strong>{routePlan.load_l.toLocaleString('ro-RO')} L</strong><small>Încărcătură estimată</small></div>
                  <div><strong>{stops.length}</strong><small>Opriri</small></div>
                </div>
              )}

              {routeLoading && <p className="empty">Se încarcă ruta...</p>}

              {/* fereastra prin care se vede harta; in vederea 3D harta e mai mare si inclinata in ea */}
              <div className="map-viewport">
              <MapContainer
                key={driverId}
                center={[mapCenter.lat, mapCenter.lng]}
                zoom={14}
                scrollWheelZoom={false}
                // leaflet-rotate: harta se poate roti pe directia de mers; rotirea o face doar navigatia
                {...({ rotate: true, touchRotate: false, shiftKeyRotate: false, rotateControl: false } as object)}
              >
                <TileLayer
                  attribution="&copy; OpenStreetMap contributors"
                  url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
                />
                <MapFocus point={driverPosition} route={routePlan?.geometry ?? []} />

                {routePlan && routePlan.geometry.length > 0 && (
                  <Polyline
                    positions={routePlan.geometry.map(point => [point.lat, point.lng])}
                    pathOptions={{ color: '#24845d', weight: 5 }}
                  />
                )}

                {stops.map(stop => (
                  <CircleMarker
                    key={stop.id}
                    center={[stop.location.lat, stop.location.lng]}
                    radius={9}
                    pathOptions={{
                      color: stop.done ? '#24845d' : stop.type === 'alerta' ? '#dc4545' : '#d28a27',
                      fillColor: stop.done ? '#24845d' : stop.type === 'alerta' ? '#dc4545' : '#d28a27',
                      fillOpacity: 0.9
                    }}
                  >
                    <Popup>
                      <strong>{stop.name}</strong><br />
                      {stop.type === 'alerta' ? 'Depozitare ilegală' : 'Pubelă'}<br />
                      {stop.address}<br />
                      {stop.type === 'pubela' ? `Umplere: ${stop.fill}%` : ''}
                    </Popup>
                  </CircleMarker>
                ))}

                {/* dupa traseul verde si opriri, ca etapa curenta (albastra) sa fie deasupra */}
                <NavLayer n={navigation} />

                {driverPosition && !navigation.started && (
                  <CircleMarker
                    center={[driverPosition.lat, driverPosition.lng]}
                    radius={7}
                    pathOptions={{ color: '#2463eb', fillColor: '#2463eb', fillOpacity: 1 }}
                  >
                    <Popup>Poziția ta</Popup>
                  </CircleMarker>
                )}
              </MapContainer>
              </div>
            </section>

            <section className="panel">
              <div className="panel-heading">
                <div><span className="step">02</span><h2>Opririle zilei</h2></div>
                <Navigation size={21} />
              </div>

              {stops.length === 0 ? (
                <p className="empty">Nu există detalii pentru opririle acestei rute.</p>
              ) : (
                stops.map((stop, index) => (
                  <article className="stop" key={stop.id}>
                    <div className="stop-number">{index + 1}</div>
                    <div className="stop-info">
                      <strong>{stop.type === 'alerta' ? '⚠️ ' : '🗑️ '}{stop.name}</strong>
                      <small>{stop.address}</small>
                      {stop.type === 'pubela' && (
                        <>
                          <div className="fill-line"><span style={{ width: `${stop.fill}%` }} /></div>
                          <small>Umplere: {stop.fill}%</small>
                        </>
                      )}
                    </div>
                    <button
                      className={stop.done ? 'done-button' : 'empty-button'}
                      disabled={stop.done}
                      onClick={() => completeStop(stop)}
                    >
                      {stop.done ? <><Check size={15} /> Finalizat</> : stop.type === 'alerta' ? 'Colectată' : 'Golită'}
                    </button>
                  </article>
                ))
              )}
            </section>
          </>
        )}

        <footer>
          BINary · {DEMO_MODE ? 'Date demonstrative din rutarea Teo' : 'Date live'}
        </footer>
      </main>
    </div>
  )
}
