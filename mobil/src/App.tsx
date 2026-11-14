import { useState } from 'react'
import { Camera, ClipboardList, List, Map as MapIcon, Navigation2 } from 'lucide-react'

import type { Role } from './api'
import NearbyMap from './citizen/NearbyMap'
import ReportFlow from './citizen/ReportFlow'
import MyReports from './citizen/MyReports'
import { DriverNav, DriverStops, TRUCK_COLORS, useDriver } from './driver/DriverView'
import { NavTopBar } from './Navigatie'
import { useGeolocation } from './lib'

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

export default function App() {
  const [role, setRole] = useState<Role>('citizen')
  const [truckId, setTruckId] = useState('2')

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
            <option value="0">Camion 1</option>
            <option value="1">Camion 2</option>
            <option value="2">Camion 3</option>
          </select>
        )}
        <div className="segmented" role="tablist" aria-label="Mod">
          <button role="tab" aria-selected={role === 'citizen'} onClick={() => setRole('citizen')}>Cetățean</button>
          <button role="tab" aria-selected={role === 'driver'} onClick={() => setRole('driver')}>Șofer</button>
        </div>
      </header>

      <main className="content">
        {role === 'citizen' ? <Citizen /> : <Driver key={truckId} truckId={truckId} />}
      </main>
    </div>
  )
}
