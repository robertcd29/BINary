export type Role = 'citizen' | 'driver'
export type Coordinates = { lat: number; lng: number }

export type ReportStatus = 'În așteptare locală' | 'În verificare' | 'Acceptată' | 'Respinsă'
export type Report = {
  id: string
  description: string
  location: Coordinates
  photoName?: string
  status: ReportStatus
  createdAt: string
}

export type StopType = 'pubela' | 'alerta'

export type RouteStop = {
  id: string
  name: string
  address: string
  location: Coordinates
  fill: number
  done: boolean
  type: StopType
}

export type DriverRoute = {
  truck: number
  stops: string[]
  distance_m: number
  load_l: number
  geometry: Coordinates[]
  stopDetails: RouteStop[]
}

// gol = acelasi server de pe care s-a deschis aplicatia (merge si pe IP, si prin tunel)
const API_BASE = (import.meta.env.VITE_API_BASE_URL || window.location.origin).replace(/\/$/, '')
export const DEMO_MODE = import.meta.env.VITE_DEMO_MODE !== 'false'

export async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init)
  if (!response.ok) {
    let detail = `Serverul a răspuns cu HTTP ${response.status}`
    try {
      const body = await response.json()
      if (body.detail) detail = body.detail
    } catch {}
    throw new Error(detail)
  }
  return response.json() as Promise<T>
}

export async function submitReport(report: {
  description: string
  location: Coordinates
  photo?: File
}) {
  // Până când Robert implementează POST /rapoarte, nu simulăm un răspuns de server.
  if (DEMO_MODE) {
    return {
      id: `LOCAL-${Date.now()}`,
      status: 'În așteptare locală' as ReportStatus
    }
  }

  const form = new FormData()
  form.append('descriere', report.description)
  form.append('lat', String(report.location.lat))
  form.append('lng', String(report.location.lng))
  if (report.photo) form.append('poza', report.photo)

  return apiRequest<{ id: string; status: ReportStatus }>('/rapoarte', {
    method: 'POST',
    body: form
  })
}

type TeoBin = {
  id: string
  lat: number
  lon: number
  fill_level?: number
  capacity_l?: number
}

type TeoAlert = {
  id: string
  lat: number
  lng: number
  status?: string
  truck?: number
  volum_l?: number
}

type TeoRoute = {
  truck: number
  stops: string[]
  distance_m: number
  load_l: number
  geometry: [number, number][]
}

type TeoPlan = {
  routes: TeoRoute[]
  alerts?: TeoAlert[]
}

function makeStopDetails(
  route: TeoRoute,
  bins: TeoBin[],
  alerts: TeoAlert[]
): RouteStop[] {
  const binsById = new Map(bins.map(bin => [bin.id, bin]))
  const alertsById = new Map(alerts.map(alert => [alert.id, alert]))

  return route.stops
    .filter(id => id !== 'depot')
    .flatMap((id): RouteStop[] => {
      const bin = binsById.get(id)
      if (bin) {
        return [{
          id,
          name: `Pubelă ${id}`,
          address: 'Locație din planul de rutare',
          location: { lat: bin.lat, lng: bin.lon },
          fill: Math.round((bin.fill_level ?? 0) * 100),
          done: false,
          type: 'pubela' as const
        }]
      }

      const alert = alertsById.get(id)
      if (alert) {
        return [{
          id,
          name: `Alertă ${id}`,
          address: 'Depozitare ilegală',
          location: { lat: alert.lat, lng: alert.lng },
          fill: 0,
          done: alert.status === 'colectata',
          type: 'alerta' as const
        }]
      }

      // În exemplul Teo, TM-002 și TM-003 provin din tabela trashes a lui Robert.
      // Până avem endpoint-ul comun de detalii, păstrăm coordonatele demonstrative.
      const physicalDemo: Record<string, Coordinates> = {
        'TM-001': { lat: 45.7489, lng: 21.2087 },
        'TM-002': { lat: 45.7537, lng: 21.2257 },
        'TM-003': { lat: 45.7601, lng: 21.2290 }
      }
      const physical = physicalDemo[id]
      if (physical) {
        return [{
          id,
          name: `Pubelă ${id}`,
          address: 'Pubelă fizică – coordonate demonstrative',
          location: physical,
          fill: 0,
          done: false,
          type: 'pubela' as const
        }]
      }
      return []
    })
}

async function getDemoPlan(): Promise<TeoPlan> {
  const [planResponse, binsResponse] = await Promise.all([
    fetch('/routing-demo.json'),
    fetch('/routing-bins.json')
  ])
  if (!planResponse.ok || !binsResponse.ok) {
    throw new Error('Nu găsesc fișierele demo de rutare în public/.')
  }
  const plan = await planResponse.json() as TeoPlan
  return plan
}

export async function getDriverRoute(truckId: string): Promise<DriverRoute> {
  const truck = Number(truckId)
  if (!Number.isInteger(truck) || truck < 0) {
    throw new Error('ID-ul camionului trebuie să fie 0, 1 sau 2.')
  }

  let plan: TeoPlan
  let bins: TeoBin[]

  if (DEMO_MODE) {
    const [demoPlan, binsResponse] = await Promise.all([
      getDemoPlan(),
      fetch('/routing-bins.json')
    ])
    plan = demoPlan
    const binsData = await binsResponse.json() as { bins: TeoBin[] }
    bins = binsData.bins
  } else {
    plan = await apiRequest<TeoPlan>('/api/rute')
    // Endpoint-ul /trashes există deja în server.py al lui Robert.
    // Pubelele simulate sunt încărcate din fișierul static de configurare.
    const [physicalResponse, binsResponse] = await Promise.all([
      apiRequest<Array<{ id: string; lat: number; lng: number; umplere: number }>>('/trashes'),
      fetch('/routing-bins.json')
    ])
    const physical = await physicalResponse
    const binsData = await binsResponse.json() as { bins: TeoBin[] }
    bins = [
      ...binsData.bins,
      ...physical.map(p => ({
        id: p.id,
        lat: p.lat,
        lon: p.lng,
        fill_level: p.umplere / 100
      }))
    ]
  }

  const route = plan.routes.find(item => item.truck === truck)
  if (!route) throw new Error(`Nu există o rută calculată pentru camionul ${truck}.`)

  const geometry = route.geometry.map(([lat, lng]) => ({ lat, lng }))
  const stopDetails = makeStopDetails(route, bins, plan.alerts ?? [])

  return {
    truck: route.truck,
    stops: route.stops,
    distance_m: route.distance_m,
    load_l: route.load_l,
    geometry,
    stopDetails
  }
}

export async function sendDriverPosition(truckId: string, position: Coordinates) {
  if (DEMO_MODE) return { status: 'demo' }

  return apiRequest('/pozitie', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      camion: Number(truckId),
      lat: position.lat,
      lng: position.lng
    })
  })
}

export async function markStopEmptied(stopId: string) {
  if (DEMO_MODE) return { status: 'demo' }
  return apiRequest(`/api/pubele/${encodeURIComponent(stopId)}/golita`, {
    method: 'POST'
  })
}

export async function markAlertCollected(alertId: string) {
  if (DEMO_MODE) return { status: 'demo' }
  return apiRequest(`/api/alerte/${encodeURIComponent(alertId)}/colectata`, {
    method: 'POST'
  })
}

export const demoStops: RouteStop[] = [
  {
    id: 'TM-001',
    name: 'Pubelă TM-001',
    address: 'Timișoara',
    location: { lat: 45.7489, lng: 21.2087 },
    fill: 73,
    done: false,
    type: 'pubela'
  },
  {
    id: 'TM-002',
    name: 'Pubelă TM-002',
    address: 'Timișoara',
    location: { lat: 45.7537, lng: 21.2257 },
    fill: 41,
    done: false,
    type: 'pubela'
  },
  {
    id: 'TM-003',
    name: 'Pubelă TM-003',
    address: 'Timișoara',
    location: { lat: 45.7601, lng: 21.2290 },
    fill: 90,
    done: false,
    type: 'pubela'
  }
]

// ---------- navigatie pas cu pas (GET /api/navigatie/{camion} din serverul de rutare) ----------

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
