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

Opririle pot include și `{ "id": "groapa-0-1", "tip": "groapa", ... }`: camionul se umple, merge să descarce la groapa de gunoi, apoi continuă traseul (ultima descărcare e mereu înainte de depou). `in_camion_l` / `capacitate_l` = cât are camionul acum, de la ultima descărcare.
Ghenele de la 55% în sus se colectează sigur; una care trece de 55% în timpul zilei intră singură pe ruta camionului zonei ei, iar opririle rămase se reordonează de unde e camionul.

### `POST /pozitie`  — NOU
Aplicația o trimite la fiecare ~10 secunde, cât timp șoferul e pe traseu.
```json
{ "camion": 0, "lat": 45.7540, "lng": 21.2500 }
```
Răspuns: `{ "status": "ok" }`

### `POST /pubele/{id}/golita`  — există
Șoferul apasă „golită” la o pubelă. Fără body. Răspuns: `{ "status": "ok" }`

### `POST /alerte/{id}/colectata`  — există
Șoferul apasă „colectată” la o depozitare ilegală. Fără body. Răspuns: `{ "status": "ok" }`.
Dacă alerta era deja închisă (a doua apăsare, sau camera a văzut înainte că locul e curat), răspunsul e tot `ok`, cu `"deja": "colectata" | "disparuta"`.

### `POST /api/opriri/{id}/descarcat`  — NOU
Șoferul apasă „Am descărcat” la o oprire `groapa-…`. Fără body. Camionul e gol din nou, iar opririle rămase se reordonează. Răspuns: `{ "status": "ok" }`.

---

## Camera (Daria)
Adresa: `https://binary-tm.duckdns.org/api/...`. La un push pe `routing` serverul repornește și câteva secunde răspunde **502**: scriptul camerei reîncearcă (de ex. de 5 ori, la 2–3 s), nu raportează „backend indisponibil” de la prima încercare.

### `POST /api/alerte` — camera vede gunoi
```json
{ "lat": 45.7368, "lng": 21.2527, "sursa": "camera", "camera_id": "CAM-01", "marime": "medie",
  "imagine": "<JPEG în base64, opțional>" }
```
Se poate trimite la fiecare detecție: la sub 50 m de o alertă deschisă e `"status": "duplicat"` (aceeași alertă, nu una nouă).
Răspuns: `{ "id": "AL-0009", "status": "assigned" | "pending" | "duplicat", ... }`.

### `POST /api/alerte/curat` — camera nu mai vede gunoiul
Cineva l-a luat sau a fost o detecție falsă. Se închid alertele deschise din raza de 50 m (status `disparuta`), opririle ies din rutele șoferilor, punctele dispar de pe hartă. Nu trebuie știut id-ul alertei.
```
Header: X-Cheie-Camera: <CHEIE_CAMERA din .env-ul serverului>
Body:   { "lat": 45.7368, "lng": 21.2527, "camera_id": "CAM-01" }
```
Aceleași coordonate ca la `POST /api/alerte`. Trimis **abia după câteva cadre la rând fără gunoi** (ca un om sau o mașină care trece prin fața camerei să nu scoată oprirea din rută). Se poate trimite de mai multe ori.
Răspuns: `{ "status": "ok", "inchise": ["AL-0009"] }` (listă goală = nu era nimic deschis acolo). Fără cheie sau cu cheie greșită: 401.

`POST /api/alerte/{id}/disparuta` face același lucru pentru un singur id (același header).

---

## Erori
Cod 4xx/5xx cu `{ "detail": "mesaj" }`. Aplicația afișează mesajul.

## Până sunt gata endpoint-urile NOI
Mihai folosește exact exemplele de mai sus ca date false.
