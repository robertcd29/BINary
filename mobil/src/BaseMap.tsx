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

// Cati pixeli are voie sa aiba canvas-ul hartii. Pe telefon (mai ales iPhone) un canvas WebGL prea mare,
// redesenat la fiecare cadru, umple memoria paginii si Safari o inchide ("A problem repeatedly occurred").
// Vederea 3D face harta de cateva ori mai mare decat ecranul, deci acolo rezolutia scade automat.
const MOBILE = typeof window !== 'undefined' && window.matchMedia?.('(pointer: coarse)').matches
const MAX_CANVAS_PX = MOBILE ? 3.5e6 : 10e6

type LayerWithGl = L.Layer & { getSize(): L.Point; getMaplibreMap(): { setPixelRatio(r: number): void } | undefined }

function pixelRatioFor(size: L.Point) {
  const css = Math.max(1, size.x * size.y)
  return Math.max(0.6, Math.min(window.devicePixelRatio || 1, 2, Math.sqrt(MAX_CANVAS_PX / css)))
}

/** padding = cat de mare e harta fata de ecran, pe fiecare latura (o harta rotita are nevoie de mai mult). */
export default function BaseMap({ padding = 0.1 }: { padding?: number }) {
  const map = useMap()
  useEffect(() => {
    const size = map.getSize().multiplyBy(1 + padding * 2)
    let ratio = pixelRatioFor(size)
    const layer = new Base({
      style: MAP_STYLE,
      padding,
      updateInterval: 16,
      attribution: ATTRIBUTION,
      attributionControl: { customAttribution: ATTRIBUTION },
      pixelRatio: ratio,
    }) as LayerWithGl
    layer.addTo(map)
    // harta isi schimba marimea (navigatie pe tot ecranul, vederea 3D): rezolutia se potriveste din nou
    const adjust = () => {
      const r = pixelRatioFor(layer.getSize())
      if (Math.abs(r - ratio) < 0.05) return
      ratio = r
      try { layer.getMaplibreMap()?.setPixelRatio(r) } catch { /* harta inca se incarca */ }
    }
    map.on('resize', adjust)
    return () => { map.off('resize', adjust); map.removeLayer(layer) }
  }, [map, padding])
  return null
}
