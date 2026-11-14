// Raportarea unei depozitari ilegale: o poza + locatia. Serverul verifica poza (Gemini)
// si, daca e o depozitare reala, o pune direct pe ruta unui camion.
import { useEffect, useRef, useState } from 'react'
import { Camera, Check, CircleAlert, MapPin, RotateCcw, ShieldCheck } from 'lucide-react'
import { CircleMarker, MapContainer, TileLayer, useMap } from 'react-leaflet'

import { submitReport, type Coordinates, type ReportResult } from '../api'
import { saveReport, type GeoState } from '../lib'

function Recenter({ point }: { point: Coordinates }) {
  const map = useMap()
  useEffect(() => { map.setView([point.lat, point.lng], 17, { animate: false }) }, [point, map])
  return null
}

export default function ReportFlow({ geo, onDone }: { geo: GeoState; onDone: () => void }) {
  const [photo, setPhoto] = useState<File | null>(null)
  const [preview, setPreview] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState<ReportResult | null>(null)
  const input = useRef<HTMLInputElement>(null)

  useEffect(() => () => { if (preview) URL.revokeObjectURL(preview) }, [preview])

  function pick(f: File | undefined) {
    if (!f) return
    setPhoto(f)
    setPreview(URL.createObjectURL(f))
    setError('')
  }

  function reset() {
    setPhoto(null)
    setPreview(null)
    setResult(null)
    setError('')
    if (input.current) input.current.value = ''
  }

  async function send() {
    if (!photo || !geo.position) return
    setBusy(true)
    setError('')
    try {
      const r = await submitReport(photo, geo.position)
      setResult(r)
      if (r.acceptat) {
        saveReport({ id: r.alerta.id, createdAt: new Date().toISOString(), descriere: r.descriere,
          lat: geo.position.lat, lng: geo.position.lng })
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Nu am putut trimite raportul.')
    } finally {
      setBusy(false)
    }
  }

  if (result) {
    return (
      <div className="screen screen-pad">
        <div className={`result ${result.acceptat ? 'ok' : 'no'}`}>
          <div className="result-icon">{result.acceptat ? <Check size={30} /> : <CircleAlert size={30} />}</div>
          <h1>{result.acceptat ? 'Mulțumim! Raport acceptat.' : 'Nu pare o depozitare de deșeuri'}</h1>
          <p>{result.acceptat ? result.descriere : result.motiv}</p>
          {result.acceptat && (
            <div className="result-next">
              {result.alerta.status === 'pe_ruta'
                ? 'A fost adăugată deja pe ruta unui camion de azi.'
                : result.alerta.status === 'duplicat'
                  ? 'Altcineva a raportat-o deja. Raportul tău îi crește prioritatea.'
                  : 'Dispeceratul a primit-o și o planifică pentru colectare.'}
            </div>
          )}
          <div className="result-actions">
            {result.acceptat
              ? <button className="btn btn-primary btn-block" onClick={onDone}>Vezi rapoartele mele</button>
              : <button className="btn btn-primary btn-block" onClick={reset}><RotateCcw size={18} /> Încearcă altă poză</button>}
            <button className="btn btn-ghost btn-block" onClick={reset}>Raportează altceva</button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="screen screen-pad">
      <header className="screen-head">
        <h1>Raportează o depozitare</h1>
        <p>Fă o poză. Restul îl facem noi: verificăm imaginea și trimitem un camion.</p>
      </header>

      <input ref={input} type="file" accept="image/*" capture="environment" hidden onChange={e => pick(e.target.files?.[0])} />

      {preview ? (
        <div className="photo">
          <img src={preview} alt="Poza pentru raport" />
          <button className="photo-retake" onClick={() => input.current?.click()}><RotateCcw size={16} /> Refă poza</button>
        </div>
      ) : (
        <button className="photo-drop" onClick={() => input.current?.click()}>
          <span className="photo-drop-icon"><Camera size={28} /></span>
          <strong>Fotografiază deșeurile</strong>
          <span>sau alege o poză din galerie</span>
        </button>
      )}

      <div className="card location">
        <div className="location-row">
          <MapPin size={18} />
          <div>
            <strong>{geo.position ? 'Locația ta' : 'Aflăm locația…'}</strong>
            <span>{geo.position
              ? `Precizie ±${geo.accuracy ?? '?'} m`
              : geo.error ?? 'Permite accesul la locație când îți cere telefonul.'}</span>
          </div>
          {geo.error && <button className="btn btn-small" onClick={geo.request}>Reîncearcă</button>}
        </div>
        {geo.position && (
          <MapContainer center={[geo.position.lat, geo.position.lng]} zoom={17} zoomControl={false}
            dragging={false} scrollWheelZoom={false} doubleClickZoom={false} touchZoom={false}
            attributionControl={false} className="mini-map">
            <TileLayer url="https://tile.openstreetmap.org/{z}/{x}/{y}.png" className="tiles" />
            <Recenter point={geo.position} />
            <CircleMarker center={[geo.position.lat, geo.position.lng]} radius={8}
              pathOptions={{ color: '#fff', weight: 3, fillColor: '#ef5a17', fillOpacity: 1 }} />
          </MapContainer>
        )}
      </div>

      <div className="trust">
        <ShieldCheck size={18} />
        <span>Fiecare poză e verificată automat. Rapoartele false sunt respinse, iar cele repetate din același loc se grupează.</span>
      </div>

      {error && <p className="form-error">{error}</p>}

      <div className="sticky-action">
        <button className="btn btn-primary btn-block btn-lg" disabled={!photo || !geo.position || busy} onClick={send}>
          {busy ? 'Verificăm poza…' : !photo ? 'Adaugă o poză' : !geo.position ? 'Așteptăm locația…' : 'Trimite raportul'}
        </button>
      </div>
    </div>
  )
}
