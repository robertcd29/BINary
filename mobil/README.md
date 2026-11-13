# BINary – aplicație mobilă PWA (prototip)

## Tehnologii
- React + TypeScript + Vite
- PWA cu `vite-plugin-pwa`
- Leaflet + OpenStreetMap pentru hartă
- Geolocation API pentru GPS

## Pornire pe Mac
Necesită Node.js 20.19+ sau 22.12+.

```bash
npm install
cp .env.example .env
npm run dev
```

Deschide URL-ul afișat de Vite. Pentru acces de pe telefon în aceeași rețea, pornește cu `npm run dev -- --host 0.0.0.0` și deschide IP-ul Mac-ului pe portul 5173.

## Build și publicare
Serverul servește aplicația direct din `mobil/dist` (vezi `routing/main.py`), iar serverul nu are Node, deci build-ul se face local și se pune în git:

```bash
npm run build                 # folosește .env.production: API pe același server, fără date demo
git add mobil && git commit -m "..." && git push    # pe branch-ul routing
```

În cel mult un minut serverul ia versiunea nouă. Pe telefon, un refresh (de două ori, din cauza service worker-ului).

## Instalare pe telefon
Publică aplicația pe un host HTTPS (de exemplu Vercel/Netlify sau server propriu cu HTTPS). Pe iPhone: deschide URL-ul în Safari → Share → Add to Home Screen. Pe Android: deschide în Chrome → meniul ⋮ → Install app / Add to Home screen.

Geolocația și instalarea PWA necesită HTTPS în producție (localhost este exceptat pentru dezvoltare). Pe iPhone folosește Safari pentru instalarea pe ecranul principal.

## Integrarea cu serverul lui Robert
În `.env` setează `VITE_API_BASE_URL` la URL-ul public/HTTPS al API-ului și `VITE_DEMO_MODE=false`.

Endpoint-urile cunoscute din schema echipei:
- `POST /rapoarte`
- `GET /ruta/{sofer}`
- `POST /pozitie`

**Contractul payload-urilor nu este încă definit în schema primită.** În `src/api.ts` sunt marcate locurile care trebuie aliniate cu Robert. Același lucru este valabil pentru endpoint-ul butonului „Golit”, care nu apare încă în contract.

Autentificarea Auth0 nu este activată încă: pentru asta sunt necesare domeniul tenantului, Client ID și configurarea rolurilor/claims de la echipă. Selectorul Cetățean/Șofer este doar pentru demonstrarea interfeței, nu este securitate reală.

## Mod demo
`VITE_DEMO_MODE=true` folosește date locale demonstrative pentru raportare și ruta șoferului. Nu trimite date reale la server.
