// Loginul cu Auth0. Serverul spune daca loginul e pornit (comutatorul AUTH_ACTIV din .env)
// si cu ce date; Auth0 face pagina de login, iar rolurile (admin / sofer / cetatean) vin in token.
import { createAuth0Client, type Auth0Client } from '@auth0/auth0-spa-js'

const ROLURI = 'https://binary-api/roles'   // campul pus in token de Action-ul "Roluri in token"
let client: Auth0Client | null = null

export type Sesiune = { activ: boolean; logat: boolean; email?: string; roluri: string[]; eroare?: string }

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
  if (q.has('code') && q.has('state')) {
    try { await client.handleRedirectCallback() } catch { /* link de login vechi: cerem login din nou */ }
    history.replaceState({}, '', location.pathname)
  }
  if (!await client.isAuthenticated()) return { activ: true, logat: false, roluri: [] }
  const date = await client.getIdTokenClaims()
  return { activ: true, logat: true, email: (date?.email || date?.name) as string, roluri: (date?.[ROLURI] as string[]) || [] }
}

export const intra = () => client?.loginWithRedirect()
export const iesi = () => client?.logout({ logoutParams: { returnTo: location.origin + '/' } })

/** Antetul cu tokenul, pentru cererile care schimba ceva pe server (fara login pornit: nimic). */
export async function antetAuth(): Promise<Record<string, string>> {
  if (!client) return {}
  try {
    return { Authorization: `Bearer ${await client.getTokenSilently()}` }
  } catch {
    await client.loginWithRedirect()
    return {}
  }
}
