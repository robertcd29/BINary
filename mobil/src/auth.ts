// Loginul cu Auth0. Serverul spune daca loginul e pornit (comutatorul AUTH_ACTIV din .env)
// si cu ce date; Auth0 face pagina de login, iar rolurile (admin / sofer / cetatean) vin in token.
import { createAuth0Client, type Auth0Client } from '@auth0/auth0-spa-js'

const ROLURI = 'https://binary-api/roles'   // campul pus in token de Action-ul "Roluri in token"
const CAMION = 'https://binary-api/camion'  // camionul soferului, din app_metadata
let client: Auth0Client | null = null

export type Sesiune = { activ: boolean; logat: boolean; vizitator?: boolean; email?: string; roluri: string[]; camion?: number; eroare?: string }

// vizitator = cetatean fara cont: poate raporta, dar nu are acces la partea de sofer
const CHEIE_VIZITATOR = 'binary-vizitator'
const esteVizitator = () => { try { return localStorage.getItem(CHEIE_VIZITATOR) === '1' } catch { return false } }
export function intraCaVizitator() { try { localStorage.setItem(CHEIE_VIZITATOR, '1') } catch { /* fara storage */ } location.reload() }
function uitaVizitator() { try { localStorage.removeItem(CHEIE_VIZITATOR) } catch { /* fara storage */ } }

export async function pornesteAuth(): Promise<Sesiune> {
  const cfg = await fetch('/api/auth-config').then(r => r.json()).catch(() => ({ activ: false }))
  if (!cfg.activ) return { activ: false, logat: true, roluri: ['cetatean', 'sofer', 'admin'] }
  if (!window.isSecureContext) {
    return { activ: true, logat: false, roluri: [], eroare: 'Loginul merge doar pe linkul https (trycloudflare).' }
  }
  client = await createAuth0Client({
    domain: cfg.domain, clientId: cfg.client_id, cacheLocation: 'localstorage', useRefreshTokens: true,
    authorizationParams: { redirect_uri: location.origin + '/', audience: cfg.audience },
  })
  const q = new URLSearchParams(location.search)
  if (q.has('state')) {
    try { await client.handleRedirectCallback() } catch { /* link de login vechi: cerem login din nou */ }
    history.replaceState({}, '', location.pathname)
  }
  if (!await client.isAuthenticated()) {
    if (esteVizitator()) return { activ: true, logat: true, vizitator: true, roluri: ['cetatean'] }
    return { activ: true, logat: false, roluri: [] }
  }
  const date = await client.getIdTokenClaims()
  const camion = date?.[CAMION]
  return { activ: true, logat: true, email: (date?.email || date?.name) as string, roluri: (date?.[ROLURI] as string[]) || [],
    camion: camion === undefined || camion === null ? undefined : Number(camion) }
}

export const intra = () => { uitaVizitator(); client?.loginWithRedirect() }
export function iesi() {
  if (esteVizitator()) { uitaVizitator(); location.reload(); return }
  client?.logout({ logoutParams: { returnTo: location.origin + '/' } })
}

/** Antetul cu tokenul, pentru cererile care schimba ceva pe server (fara login pornit: nimic). */
export async function antetAuth(interactiv = true): Promise<Record<string, string>> {
  // vizitatorul (fara cont) trimite cererile fara token: serverul accepta raportarile si asa
  if (!client || esteVizitator() || !await client.isAuthenticated()) return {}
  try {
    return { Authorization: `Bearer ${await client.getTokenSilently()}` }
  } catch (e) {
    // login din nou doar cand sesiunea chiar a expirat si doar la o actiune a utilizatorului;
    // o cadere de retea sau o cerere trimisa singura (pozitia GPS) nu il scoate din aplicatie
    const cod = (e as { error?: string })?.error
    const expirata = cod === 'login_required' || cod === 'invalid_grant' || cod === 'missing_refresh_token' || cod === 'consent_required'
    if (interactiv && expirata) await client.loginWithRedirect()
    return {}
  }
}
