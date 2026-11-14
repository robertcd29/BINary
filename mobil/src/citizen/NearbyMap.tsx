// Harta cetateanului: ghenele cu gradul de umplere live si cea mai apropiata ghena care mai are loc.
import { useEffect, useMemo, useState } from 'react'
import L from 'leaflet'
import { CircleMarker, MapContainer, Marker, TileLayer, useMap } from 'react-leaflet'
import { LocateFixed, Navigation } from 'lucide-react'

import { getBins, type Bin, type Coordinates } from '../api'
import { distanceM, fillColor, formatDistance, TIMISOARA, type GeoState } from '../lib'

const REFRESH_MS = 30_000
const HAS_ROOM = 85            // peste acest prag nu mai trimitem oamenii la ghena

function pin(b: Bin, selected: boolean) {
  const u = Math.round(b.umplere ?? 0)
  return L.divIcon({
    className: '',
    html: `<div class="bin-pin${selected ? ' selected' : ''}" style="--c:${fillColor(u)}"><i></i>${u}%</div>`,
    iconSize: [52, 26],
    iconAnchor: [26, 26]
  })
}

function FlyTo({ point }: { point: Coordinates | null }) {
  const map = useMap()
  useEffect(() => {
    if (point) map.flyTo([point.lat, point.lng], 16, { duration: 0.6 })
  }, [point, map])
  return null
}

export default function NearbyMap({ geo }: { geo: GeoState }) {
  const [bins, setBins] = useState<Bin[]>([])
  const [error, setError] = useState('')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [focus, setFocus] = useState<Coordinates | null>(null)

  useEffect(() => {
    let alive = true
    const load = () => getBins()
      .then(b => { if (alive) { setBins(b); setError('') } })
      .catch(e => alive && setError(e instanceof Error ? e.message : 'Nu am putut încărca ghenele.'))
    load()
    const id = setInterval(load, REFRESH_MS)
    return () => { alive = false; clearInterval(id) }
  }, [])

  // cea mai apropiata ghena cu loc (sau, fara GPS, cea mai goala)
  const nearest = useMemo(() => {
    const free = bins.filter(b => (b.umplere ?? 0) < HAS_ROOM)
    if (!free.length) return null
    if (!geo.position) return [...free].sort((a, b) => a.umplere - b.umplere)[0]
    return [...free].sort((a, b) => distanceM(geo.position!, a) - distanceM(geo.position!, b))[0]
  }, [bins, geo.position])

  const selected = bins.find(b => b.id === selectedId) ?? nearest
  const isNearest = selected?.id === nearest?.id
  const full = bins.filter(b => (b.umplere ?? 0) >= HAS_ROOM).length

  return (
    <div className="screen screen-map">
      <MapContainer center={[TIMISOARA.lat, TIMISOARA.lng]} zoom={13} zoomControl={false} className="full-map">
        <TileLayer attribution="© OpenStreetMap" url="https://tile.openstreetmap.org/{z}/{x}/{y}.png" className="tiles" />
        <FlyTo point={focus} />
        {bins.map(b => (
          <Marker
            key={b.id}
            position={[b.lat, b.lng]}
            icon={pin(b, b.id === selected?.id)}
            eventHandlers={{ click: () => setSelectedId(b.id) }}
            zIndexOffset={b.id === selected?.id ? 1000 : 0}
          />
        ))}
        {geo.position && (
          <>
            <CircleMarker center={[geo.position.lat, geo.position.lng]} radius={16}
              pathOptions={{ stroke: false, fillColor: '#2b59d9', fillOpacity: 0.15 }} />
            <CircleMarker center={[geo.position.lat, geo.position.lng]} radius={7}
              pathOptions={{ color: '#fff', weight: 3, fillColor: '#2b59d9', fillOpacity: 1 }} />
          </>
        )}
      </MapContainer>

      <div className="map-top">
        <div className="map-summary">
          <strong>{bins.length ? `${bins.length - full} din ${bins.length}` : '–'}</strong> ghene au loc acum
          {full > 0 && <span className="pill full">{full} pline</span>}
        </div>
        <button className="fab" onClick={() => geo.position ? setFocus({ ...geo.position }) : geo.request()} aria-label="Arată locația mea">
          <LocateFixed size={20} />
        </button>
      </div>

      <section className="sheet">
        {error && <p className="sheet-note error">{error}</p>}
        {!error && !selected && <p className="sheet-note">{bins.length ? 'Toate ghenele sunt pline acum. Revino puțin mai târziu.' : 'Se încarcă ghenele…'}</p>}
        {selected && (
          <>
            <div className="sheet-eyebrow">
              {isNearest ? (geo.position ? 'Cea mai apropiată ghenă cu loc' : 'Ghena cu cel mai mult loc') : 'Ghenă selectată'}
            </div>
            <div className="sheet-row">
              <div className="sheet-main">
                <h2>{selected.nume || selected.id}</h2>
                <p>{selected.adresa || 'Timișoara'}</p>
              </div>
              {geo.position && <div className="sheet-dist">{formatDistance(distanceM(geo.position, selected))}</div>}
            </div>
            <div className="fill-meter">
              <span style={{ width: `${selected.umplere}%`, background: fillColor(selected.umplere) }} />
            </div>
            <div className="sheet-meta">
              <span>Umplere {Math.round(selected.umplere)}%</span>
              <span>{selected.umplere >= HAS_ROOM ? 'Plină: alege alta' : selected.umplere >= 60 ? 'Se umple' : 'Are loc'}</span>
            </div>
            <a
              className="btn btn-primary btn-block"
              href={`https://www.google.com/maps/dir/?api=1&destination=${selected.lat},${selected.lng}&travelmode=walking`}
              target="_blank" rel="noreferrer"
              onClick={() => setFocus({ lat: selected.lat, lng: selected.lng })}
            >
              <Navigation size={18} /> Du-mă acolo
            </a>
            {geo.error && !geo.position && <p className="sheet-note">{geo.error}</p>}
          </>
        )}
      </section>
    </div>
  )
}
