import { useEffect, useState } from 'react'
import { Camera, ClipboardList, List, LogOut, Map as MapIcon, Navigation2 } from 'lucide-react'

import type { Role } from './api'
import NearbyMap from './citizen/NearbyMap'
import ReportFlow from './citizen/ReportFlow'
import MyReports from './citizen/MyReports'
import { DriverNav, DriverStops, TRUCK_COLORS, useDriver } from './driver/DriverView'
import { NavTopBar } from './Navigatie'
import { useGeolocation } from './lib'
import { iesi, intra, pornesteAuth, type Sesiune } from './auth'

type CitizenTab = 'harta' | 'raport' | 'rapoarte'
type DriverTab = 'navigatie' | 'opriri'

function BrandMark() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M3 6h18" /><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6" /><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2" />
    </svg>
  )
}

function Citizen() {
  const [tab, setTab] = useState<CitizenTab>('harta')
  const geo = useGeolocation(true)

  return (
    <>
      {tab === 'harta' && <NearbyMap geo={geo} />}
      {tab === 'raport' && <ReportFlow geo={geo} onDone={() => setTab('rapoarte')} />}
      {tab === 'rapoarte' && <MyReports onNew={() => setTab('raport')} />}

      <nav className="tabbar" aria-label="Secțiuni">
        <button aria-current={tab === 'harta'} onClick={() => setTab('harta')}><MapIcon size={22} /><span>Ghene</span></button>
        <button className="tabbar-main" aria-current={tab === 'raport'} onClick={() => setTab('raport')}>
          <span className="tabbar-main-icon"><Camera size={24} /></span><span>Raportează</span>
        </button>
        <button aria-current={tab === 'rapoarte'} onClick={() => setTab('rapoarte')}><ClipboardList size={22} /><span>Rapoarte</span></button>
      </nav>
    </>
  )
}

function Driver({ truckId }: { truckId: string }) {
  const [tab, setTab] = useState<DriverTab>('navigatie')
  const d = useDriver(truckId)

  return (
    <>
      <NavTopBar n={d.navigation} />
      {tab === 'navigatie' ? <DriverNav d={d} truckId={truckId} /> : <DriverStops d={d} />}
      {!d.navigation.started && (
        <nav className="tabbar" aria-label="Secțiuni">
          <button aria-current={tab === 'navigatie'} onClick={() => setTab('navigatie')}><Navigation2 size={22} /><span>Navigație</span></button>
          <button aria-current={tab === 'opriri'} onClick={() => setTab('opriri')}><List size={22} /><span>Opriri</span></button>
        </nav>
      )}
    </>
  )
}

function EcranLogin({ s }: { s: Sesiune }) {
  return (
    <div className="login">
      <span className="brand-mark login-mark"><BrandMark /></span>
      <h1>BIN<b>ary</b></h1>
      <p>Raportează depozitările de gunoi din Timișoara. Intră în cont ca să trimiți poze; șoferii intră cu contul primit de la dispecerat.</p>
      {s.eroare ? <p className="login-err">{s.eroare}</p>
        : <button className="btn-primary login-btn" onClick={() => intra()}>Intră în cont</button>}
    </div>
  )
}

function FaraAcces({ email }: { email?: string }) {
  return (
    <div className="login">
      <h1>Doar pentru șoferi</h1>
      <p>Contul {email} nu are rol de șofer. Intră cu contul de șofer primit de la dispecerat.</p>
      <button className="btn-primary login-btn" onClick={() => iesi()}>Intră cu alt cont</button>
    </div>
  )
}

export default function App() {
  const [role, setRole] = useState<Role>('citizen')
  const [truckId, setTruckId] = useState('0')
  const [sesiune, setSesiune] = useState<Sesiune | null>(null)

  useEffect(() => {
    pornesteAuth().then(setSesiune).catch(e => setSesiune({ activ: true, logat: false, roluri: [], eroare: `Loginul nu a mers (${e.message}).` }))
  }, [])

  if (!sesiune) return <div className="app"><div className="login"><p>Se încarcă…</p></div></div>
  if (sesiune.activ && !sesiune.logat) return <div className="app"><EcranLogin s={sesiune} /></div>
  const poateConduce = sesiune.roluri.includes('sofer') || sesiune.roluri.includes('admin')

  return (
    <div className={`app role-${role}`}>
      <header className="appbar">
        <div className="brand">
          <span className="brand-mark"><BrandMark /></span>
          <span className="brand-name">BIN<b>ary</b></span>
        </div>
        {role === 'driver' && (
          <select className="truck-select" value={truckId} onChange={e => setTruckId(e.target.value)} aria-label="Camionul tău"
            style={{ borderColor: TRUCK_COLORS[Number(truckId)] }}>
            {TRUCK_COLORS.map((_, i) => <option key={i} value={i}>Camion {i + 1}</option>)}
          </select>
        )}
        <div className="segmented" role="tablist" aria-label="Mod">
          <button role="tab" aria-selected={role === 'citizen'} onClick={() => setRole('citizen')}>Cetățean</button>
          <button role="tab" aria-selected={role === 'driver'} onClick={() => setRole('driver')}>Șofer</button>
        </div>
        {sesiune.activ && (
          <button className="logout" onClick={() => iesi()} aria-label={`Ieși din cont (${sesiune.email})`} title={sesiune.email}>
            <LogOut size={18} />
          </button>
        )}
      </header>

      <main className="content">
        {role === 'citizen' ? <Citizen /> : poateConduce ? <Driver key={truckId} truckId={truckId} /> : <FaraAcces email={sesiune.email} />}
      </main>
    </div>
  )
}
