# BINary – contract API (aplicația lui Mihai ↔ serverul de rutare)

Server rutare (Teo): `http://<ip>:8001/api` — toate căile de mai jos se pun după `/api` (ex. `http://<ip>:8001/api/rapoarte`)
Toate răspunsurile sunt JSON. Coordonatele sunt mereu `lat`, `lng` (grade zecimale).
Login-ul (Auth0) se adaugă la final: fiecare cerere va avea `Authorization: Bearer <token>`.

---

## Cetățean

### `POST /rapoarte`  — NOU
Trimite o poză cu gunoi. Poza trece prin filtrul AI (Gemini) înainte să fie acceptată.

Cerere: `multipart/form-data`
| câmp | tip | exemplu |
|---|---|---|
| `poza` | fișier (jpg/png) | |
| `lat` | număr | 45.7537 |
| `lng` | număr | 21.2257 |

Răspuns acceptat:
```json
{
  "acceptat": true,
  "descriere": "Saci de gunoi și moloz lăsați lângă trotuar.",
  "alerta": { "id": "AL-7", "status": "pe_ruta" }
}
```
`status` poate fi: `pe_ruta` (a intrat deja într-o rută), `deschisa` (intră la următoarea planificare), `duplicat` (punctul a mai fost raportat).

Răspuns respins (tot cod 200):
```json
{
  "acceptat": false,
  "motiv": "Poza nu pare să arate gunoi aruncat ilegal."
}
```

---

## Șofer

Șoferul `N` conduce camionul `N` (0, 1, 2…).

### `GET /ruta/{camion}`  — NOU
Ruta zilei pentru un camion, cu opririle deja în ordine.
```json
{
  "camion": 0,
  "distanta_m": 9577,
  "opriri": [
    { "id": "bin_006", "tip": "pubela", "lat": 45.7547, "lng": 21.2506, "umplere": 87, "facuta": false },
    { "id": "AL-1",    "tip": "depozitare", "lat": 45.7497, "lng": 21.2529, "facuta": false }
  ],
  "traseu": [[45.7547, 21.2506], [45.7538, 21.2510]]
}
```
`traseu` = linia de desenat pe hartă (puncte `[lat, lng]`).

### `POST /pozitie`  — NOU
Aplicația o trimite la fiecare ~10 secunde, cât timp șoferul e pe traseu.
```json
{ "camion": 0, "lat": 45.7540, "lng": 21.2500 }
```
Răspuns: `{ "status": "ok" }`

### `POST /pubele/{id}/golita`  — există
Șoferul apasă „golită” la o pubelă. Fără body. Răspuns: `{ "status": "ok" }`

### `POST /alerte/{id}/colectata`  — există
Șoferul apasă „colectată” la o depozitare ilegală. Fără body. Răspuns: `{ "status": "ok" }`

---

## Erori
Cod 4xx/5xx cu `{ "detail": "mesaj" }`. Aplicația afișează mesajul.

## Până sunt gata endpoint-urile NOI
Mihai folosește exact exemplele de mai sus ca date false.
