"""Loginul cu Auth0: verifica tokenul trimis de dashboard sau de aplicatie si rolul din el.

Auth0 face loginul (pagina, parolele, Google); noi doar verificam semnatura tokenului
si citim rolurile puse in el de Action-ul "Roluri in token" (admin / sofer / cetatean).

.env:
    AUTH0_DOMAIN=dev-....us.auth0.com
    AUTH0_AUDIENCE=https://binary-api
    AUTH_ACTIV=true          # false = verificarea e oprita (comutator de urgenta pentru demo)
"""
import os
from functools import lru_cache

import jwt
from fastapi import Header, HTTPException

ROLURI = "https://binary-api/roles"      # numele campului pus de Action in token


def _cfg():
    # citit la fiecare cerere, nu la import: .env e incarcat de api.py dupa importuri
    return (os.environ.get("AUTH0_DOMAIN", ""), os.environ.get("AUTH0_AUDIENCE", "https://binary-api"),
            os.environ.get("AUTH_ACTIV", "false").strip().lower() == "true")


@lru_cache(maxsize=4)
def _chei(domeniu):
    """Cheile publice Auth0, cu care verificam semnatura (descarcate o data, apoi tinute minte)."""
    return jwt.PyJWKClient(f"https://{domeniu}/.well-known/jwks.json")


def verifica_token(authorization):
    domeniu, audienta, _ = _cfg()
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Trebuie să fii logat")
    token = authorization.split(" ", 1)[1]
    try:
        cheie = _chei(domeniu).get_signing_key_from_jwt(token).key
        return jwt.decode(token, cheie, algorithms=["RS256"], audience=audienta,
                          issuer=f"https://{domeniu}/")
    except Exception:
        raise HTTPException(401, "Sesiunea a expirat sau nu e valabilă. Loghează-te din nou.")


def cere_rol(*permise):
    """Dependinta FastAPI: lasa cererea sa treaca doar daca utilizatorul are unul din roluri."""
    def verifica(authorization: str | None = Header(None)):
        if not _cfg()[2]:
            return None                  # AUTH_ACTIV=false: totul merge ca inainte
        date = verifica_token(authorization)
        if not set(date.get(ROLURI, [])) & set(permise):
            raise HTTPException(403, "Contul tău nu are voie să facă asta")
        return date
    return verifica
