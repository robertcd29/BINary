// Harta de fundal: vectoriala (OpenFreeMap, fara cheie API), desenata cu MapLibre sub straturile Leaflet.
// Fiind vectoriala, ramane clara la orice zoom, inclusiv in navigatia soferului.
import { useEffect } from 'react'
import { useMap } from 'react-leaflet'
import L from 'leaflet'
import '@maplibre/maplibre-gl-leaflet'
import 'maplibre-gl/dist/maplibre-gl.css'

export const MAP_STYLE = 'https://tiles.openfreemap.org/styles/positron'
const ATTRIBUTION = '© <a href="https://openfreemap.org">OpenFreeMap</a> © <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'

type GlLayer = L.Layer & { _map: L.Map; _container: HTMLElement; _zooming: boolean; _glMap: unknown; getSize(): L.Point; _transformGL(gl: unknown): void }

// Pluginul pozitioneaza harta dupa coltul din stanga-sus al ecranului, ceea ce nu mai e corect cand
// harta e rotita (navigatia soferului). O pozitionam dupa centru, care nu se schimba la rotire.
const Base: new (options: object) => L.Layer = (L as unknown as { MaplibreGL: typeof L.Layer }).MaplibreGL.extend({
  _topLeft(this: GlLayer) {
    return this._map.latLngToLayerPoint(this._map.getCenter()).subtract(this.getSize().divideBy(2)).round()
  },
  _initContainer(this: GlLayer & { _topLeft(): L.Point; _resizeContainer(): void }) {
    this._container = L.DomUtil.create('div', 'leaflet-gl-layer')
    this._resizeContainer()
    L.DomUtil.setPosition(this._container, this._topLeft())
  },
  _update(this: GlLayer & { _topLeft(): L.Point }) {
    if (!this._map || this._zooming) return
    L.DomUtil.setPosition(this._container, this._topLeft())
    this._transformGL(this._glMap)
  },
  _animateZoom(this: GlLayer & { _topLeft(): L.Point }, e: L.ZoomAnimEvent) {
    const map = this._map as L.Map & { _latLngToNewLayerPoint(ll: L.LatLng, z: number, c: L.LatLng): L.Point }
    const corner = map.layerPointToLatLng(this._topLeft())
    L.DomUtil.setTransform(this._container, map._latLngToNewLayerPoint(corner, e.zoom, e.center), map.getZoomScale(e.zoom))
  },
})

/** padding = cat de mare e harta fata de ecran, pe fiecare latura (o harta rotita are nevoie de mai mult). */
export default function BaseMap({ padding = 0.1 }: { padding?: number }) {
  const map = useMap()
  useEffect(() => {
    const layer = new Base({
      style: MAP_STYLE,
      padding,
      updateInterval: 16,
      attribution: ATTRIBUTION,
      attributionControl: { customAttribution: ATTRIBUTION },
      pixelRatio: Math.min(window.devicePixelRatio || 1, 2),
    })
    layer.addTo(map)
    return () => { map.removeLayer(layer) }
  }, [map, padding])
  return null
}
