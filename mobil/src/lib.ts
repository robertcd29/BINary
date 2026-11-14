import { useEffect, useState } from 'react'
import type { Coordinates } from './api'

export function distanceM(a: Coordinates, b: Coordinates) {
  const dy = (a.lat - b.lat) * 111_320
  const dx = (a.lng - b.lng) * 111_320 * Math.cos((a.lat * Math.PI) / 180)
  return Math.hypot(dx, dy)
}

export function formatDistance(m: number) {
  if (m >= 1000) return `${(m / 1000).toLocaleString('ro-RO', { maximumFractionDigits: 1 })} km`
  return `${Math.max(0, Math.round(m / 10) * 10)} m`
}

export function timeAgo(iso: string) {
  const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000)
  if (s < 60) return 'acum'
  const m = Math.round(s / 60)
  if (m < 60) return `acum ${m} min`
  const h = Math.round(m / 60)
  if (h < 24) return `acum ${h} h`
  return new Date(iso).toLocaleDateString('ro-RO', { day: 'numeric', month: 'short' })
}

export const fillColor = (p: number) => (p >= 85 ? 'var(--full)' : p >= 60 ? 'var(--warn)' : 'var(--ok)')

export const TIMISOARA: Coordinates = { lat: 45.7538, lng: 21.2257 }

export type GeoState = {
  position: Coordinates | null
  accuracy: number | null
  error: string | null
  request: () => void
}

/** Pozitia GPS, urmarita cat timp componenta e afisata. */
export function useGeolocation(watch = true): GeoState {
  const [position, setPosition] = useState<Coordinates | null>(null)
  const [accuracy, setAccuracy] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [tick, setTick] = useState(0)

  useEffect(() => {
    if (!watch) return
    if (!navigator.geolocation) {
      setError('Telefonul nu oferă localizare.')
      return
    }
    const id = navigator.geolocation.watchPosition(
      p => {
        setPosition({ lat: p.coords.latitude, lng: p.coords.longitude })
        setAccuracy(Math.round(p.coords.accuracy))
        setError(null)
      },
      e => setError(e.code === e.PERMISSION_DENIED
        ? 'Locația e blocată. Permite accesul la locație din setările browserului.'
        : 'Nu am putut afla locația. Încearcă din nou.'),
      { enableHighAccuracy: true, maximumAge: 10_000, timeout: 20_000 }
    )
    return () => navigator.geolocation.clearWatch(id)
  }, [watch, tick])

  return { position, accuracy, error, request: () => setTick(t => t + 1) }
}

// ---------- rapoartele mele, pastrate pe telefon ----------

export type SavedReport = {
  id: string            // id-ul alertei de pe server
  createdAt: string
  descriere: string
  lat: number
  lng: number
}

const KEY = 'binary.rapoarte'

export function loadReports(): SavedReport[] {
  try {
    return JSON.parse(localStorage.getItem(KEY) || '[]') as SavedReport[]
  } catch {
    return []
  }
}

export function saveReport(r: SavedReport) {
  try {
    const all = loadReports().filter(x => x.id !== r.id)
    localStorage.setItem(KEY, JSON.stringify([r, ...all].slice(0, 50)))
  } catch { /* stocare indisponibila (mod privat): raportul tot a ajuns pe server */ }
}
