import React from 'react'
import ReactDOM from 'react-dom/client'
import './leaflet-global'
import 'leaflet-rotate'             // harta se poate roti pe directia de mers (navigatia soferului)
import App from './App'
import 'leaflet/dist/leaflet.css'   // fara el, dalele hartii se imprastie pe pagina
import './style.css'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)
