import React from 'react'
import ReactDOM from 'react-dom/client'
import './leaflet-global'
import 'leaflet-rotate'             // harta se poate roti pe directia de mers (navigatia soferului)
import App from './App'
import 'leaflet/dist/leaflet.css'   // fara el, dalele hartii se imprastie pe pagina
import './style.css'
import L from 'leaflet'

// leaflet-rotate pune implicit o busola pe fiecare harta; rotirea o controleaza doar navigatia
L.Map.mergeOptions({ rotateControl: false } as L.MapOptions)

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)
