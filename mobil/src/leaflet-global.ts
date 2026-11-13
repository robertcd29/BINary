// leaflet-rotate (harta rotita pe directia de mers) cauta Leaflet in window.L,
// deci il punem acolo inainte sa se incarce pluginul (vezi ordinea importurilor din main.tsx)
import L from 'leaflet'

;(window as unknown as { L: typeof L }).L = L
