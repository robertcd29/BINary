// Legatura cu serverul de rutare (routing/api.py). Aplicatia e servita de acelasi server,
// deci implicit vorbeste cu originea de pe care a fost deschisa.

export type Role = 'citizen' | 'driver'
export type Coordinates = { lat: number; lng: number }

const API_BASE = (import.meta.env.VITE_API_BASE_URL || window.location.origin).replace(/\/$/, '')

export async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init)
  if (!response.ok) {
    let detail = `Serverul a răspuns cu HTTP ${response.status}`
    try {
      const body = await response.json()
      if (body.detail) detail = body.detail
    } catch { /* raspuns fara JSON */ }
    throw new Error(detail)
  }
  return response.json() as Promise<T>
}

export const photoUrl = (alertId: string) => `${API_BASE}/captures/${encodeURIComponent(alertId)}.jpg`

// ---------- ghene (harta cetateanului) ----------

export type Bin = {
  id: string
  lat: number
  lng: number
  umplere: number
  nume: string | null
  adresa: string | null
  pe_ruta_posibil?: boolean
}

export const getBins = () => apiRequest<Bin[]>('/api/pubele')

// ---------- raportari de la cetateni ----------

export type AlertStatus = 'deschisa' | 'asignata' | 'colectata' | 'duplicat'

export type Alert = {
  id: string
  sursa: string
  lat: number
  lng: number
  status: AlertStatus
  camion: number | null
  descriere: string | null
  creata: string
}

export const getAlerts = () => apiRequest<Alert[]>('/api/alerte')

export type ReportResult =
  | { acceptat: true; descriere: string; alerta: { id: string; status: string } }
  | { acceptat: false; motiv: string }

/** Poza e verificata pe server (Gemini); daca e o depozitare reala, intra direct pe o ruta. */
export async function submitReport(photo: File, location: Coordinates): Promise<ReportResult> {
  const form = new FormData()
  form.append('poza', photo)
  form.append('lat', String(location.lat))
  form.append('lng', String(location.lng))
  return apiRequest<ReportResult>('/api/rapoarte', { method: 'POST', body: form })
}

// ---------- soferi ----------

export type RouteStop = {
  id: string
  tip: 'pubela' | 'depozitare'
  nume: string
  adresa: string | null
  lat: number
  lng: number
  umplere?: number
  facuta: boolean
}

export type DriverRoute = {
  camion: number
  distanta_m: number
  incarcare_l: number
  opriri: RouteStop[]
  traseu: [number, number][]
}

export const getDriverRoute = (truckId: string) =>
  apiRequest<DriverRoute>(`/api/ruta/${encodeURIComponent(truckId)}`)

export async function sendDriverPosition(truckId: string, position: Coordinates) {
  return apiRequest('/api/pozitie', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ camion: Number(truckId), lat: position.lat, lng: position.lng })
  })
}

export const markStopEmptied = (stopId: string) =>
  apiRequest(`/api/pubele/${encodeURIComponent(stopId)}/golita`, { method: 'POST' })

export const markAlertCollected = (alertId: string) =>
  apiRequest(`/api/alerte/${encodeURIComponent(alertId)}/colectata`, { method: 'POST' })

// ---------- navigatie pas cu pas (GET /api/navigatie/{camion}) ----------

export type NavStep = {
  tip: 'start' | 'inainte' | 'usor-dreapta' | 'dreapta' | 'usor-stanga' | 'stanga' | 'intoarcere' | 'sosire'
  text: string
  strada: string
  la_m: number        // unde incepe manevra, in metri de la inceputul etapei
  distanta_m: number  // cat tine pana la urmatoarea manevra
  lat: number
  lng: number
}

export type NavLeg = {
  geometrie: [number, number][]
  distanta_m: number
  pasi: NavStep[]
}

export type NavStop = {
  id: string
  tip: 'pubela' | 'depozitare' | 'depou'
  nume: string
  adresa: string | null
  lat: number
  lng: number
  umplere?: number
}

export type Navigation = {
  camion: number
  opriri: NavStop[]   // opriri[i] e capatul etapei etape[i]; ultima e depoul
  etape: NavLeg[]
}

export async function getNavigation(truckId: string, from?: Coordinates | null): Promise<Navigation> {
  const q = from ? `?lat=${from.lat}&lng=${from.lng}` : ''
  return apiRequest<Navigation>(`/api/navigatie/${encodeURIComponent(truckId)}${q}`)
}
