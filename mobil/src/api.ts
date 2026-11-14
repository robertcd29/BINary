// Legatura cu serverul de rutare (routing/api.py). Aplicatia e servita de acelasi server,
// deci implicit vorbeste cu originea de pe care a fost deschisa.

import { antetAuth } from './auth'

export type Role = 'citizen' | 'driver'
export type Coordinates = { lat: number; lng: number }

const API_BASE = (import.meta.env.VITE_API_BASE_URL || window.location.origin).replace(/\/$/, '')

/** Eroare de la server; status 0 = n-a ajuns deloc la server (fara retea, tunel cazut). */
export class ApiError extends Error {
  constructor(message: string, readonly status: number) { super(message) }
}

/** Probleme trecatoare (retea, server repornit, tunel Cloudflare 502/503/504): merita reincercat
 *  si nu trebuie sa stearga ce vede soferul pe ecran. */
export const isTransient = (e: unknown) => e instanceof ApiError && (e.status === 0 || e.status >= 500)

// ~30 s in total: cat dureaza o repornire a serverului dupa un deploy (Caddy da 502 intre timp)
const RETRY_DELAYS_MS = [700, 1500, 3000, 5000, 8000, 12000]

export async function apiRequest<T>(path: string, init?: RequestInit, background = false): Promise<T> {
  // cererile care schimba ceva (POST) merg cu tokenul de login; citirile raman publice.
  // background = trimisa singura (pozitia GPS): daca tokenul nu se poate reinnoi, NU plecam la login
  // (asta scotea soferul din navigatie); cererea pleaca fara token si incercam din nou peste 2 s
  const auth = init?.method && init.method !== 'GET' ? await antetAuth(!background) : {}
  const isRead = !init?.method || init.method === 'GET'
  for (let attempt = 0; ; attempt++) {
    let response: Response
    try {
      response = await fetch(`${API_BASE}${path}`, { ...init, headers: { ...(init?.headers || {}), ...auth } })
    } catch {
      response = new Response(null, { status: 599 })
    }
    // citirile se reincearca singure la o cadere scurta a serverului. Scrierile doar la 502: atunci
    // Caddy nici nu a ajuns la server, deci cererea sigur nu s-a facut si nu se poate dubla
    const retry = isRead ? [502, 503, 504, 599].includes(response.status) : response.status === 502
    if (retry && attempt < RETRY_DELAYS_MS.length) {
      await new Promise(r => setTimeout(r, RETRY_DELAYS_MS[attempt]))
      continue
    }
    if (response.status === 599) throw new ApiError('Nu am legătură cu serverul. Verifică internetul.', 0)
    if (response.status === 502) throw new ApiError('Serverul se repornește. Încearcă din nou în câteva secunde.', 502)
    if (!response.ok) {
      let detail = `Serverul a răspuns cu HTTP ${response.status}`
      try {
        const body = await response.json()
        if (body.detail) detail = body.detail
      } catch { /* raspuns fara JSON */ }
      throw new ApiError(detail, response.status)
    }
    return response.json() as Promise<T>
  }
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
  deseu?: 'menajer' | 'reciclabil'
}

export const getBins = () => apiRequest<Bin[]>('/api/pubele')

// ---------- raportari de la cetateni ----------

export type AlertStatus = 'deschisa' | 'asignata' | 'colectata' | 'disparuta' | 'duplicat'

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
  }, true)
}

export const markStopEmptied = (stopId: string) =>
  apiRequest(`/api/pubele/${encodeURIComponent(stopId)}/golita`, { method: 'POST' })

export const markAlertCollected = (alertId: string) =>
  apiRequest(`/api/alerte/${encodeURIComponent(alertId)}/colectata`, { method: 'POST' })

// ---------- navigatie pas cu pas (GET /api/navigatie/{camion}) ----------

export type NavStep = {
  tip: 'start' | 'inainte' | 'usor-dreapta' | 'dreapta' | 'usor-stanga' | 'stanga' | 'intoarcere' | 'sosire'
    | 'giratoriu' | 'iesire-giratoriu'
  text: string
  strada: string
  la_m: number        // unde incepe manevra, in metri de la inceputul etapei
  distanta_m: number  // cat tine pana la urmatoarea manevra
  lat: number
  lng: number
  // benzile strazii inainte de manevra (de la stanga la dreapta) si pe care sa mearga soferul
  benzi?: { directii: LaneDir[]; buna: boolean }[]
  benzi_estimat?: boolean   // fara marcaje in OpenStreetMap: dupa regula generala din codul rutier
  benzi_text?: string
  iesire?: number     // sens giratoriu: a cata iesire (0 = necunoscut)
  unghi?: number      // sens giratoriu: directia iesirii fata de intrare, in grade (pozitiv = dreapta)
}

export type LaneDir = 'reverse' | 'sharp_left' | 'left' | 'slight_left' | 'through' | 'slight_right' | 'right' | 'sharp_right'

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
