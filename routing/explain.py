"""Explicatii pe intelesul dispecerului pentru planul de rute, generate cu Gemini.

Faptele (km, pubele, motive) le calculeaza codul nostru. Gemini doar le formuleaza,
ca sa nu poata inventa cifre. Daca API-ul nu e disponibil, se foloseste un text simplu.
"""
import os
import time

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


def _facts(plan, bins):
    """Rezumatul planului ca lista de fapte verificabile."""
    by_id = {b["id"]: b for b in bins}
    m = plan["metrics"]
    facts = [f"Distanta totala: {m['total_km']} km."]
    if "saving_pct" in m:
        facts.append(f"Colectarea tuturor pubelelor ar fi insemnat {m['baseline_km']} km; "
                     f"economie {m['saving_pct']}%.")
    for r in plan["routes"]:
        bins_on_route = [s for s in r["stops"] if s in by_id]
        alerts = [s for s in r["stops"] if s != "depot" and s not in by_id]
        if not bins_on_route and not alerts:
            facts.append(f"Camionul {r['truck']} ramane in depozit: nu e nevoie de el azi.")
            continue
        line = (f"Camionul {r['truck']}: {len(bins_on_route)} pubele, "
                f"{r['distance_m'] / 1000:.1f} km, incarcat {r['load_l']} L")
        if alerts:
            line += f", preia si depozitarile ilegale {', '.join(alerts)}"
        facts.append(line + ".")
    for s in plan["skipped"]:
        hours = plan["predictions"].get(s, {}).get("hours_to_full")
        fill = by_id[s]["umplere"]
        if hours is not None:
            reason = (f"plina {fill}%, se umple in aproximativ {hours:.0f} ore, "
                      f"deci poate astepta urmatoarea colectare")
        else:
            reason = (f"plina {fill}%, sub pragul de 80%, si ocolul pana la ea "
                      f"ar costa mai mult decat merita azi; nu are inca istoric de masuratori")
        facts.append(f"Pubela {s} e sarita azi: {reason}.")
    for a in plan.get("alerts", []):
        if a.get("status") != "assigned":
            facts.append(f"Depozitarea ilegala {a['id']} asteapta: {a.get('reason', 'neasignata')}.")
            continue
        how = ("cu o cursa separata din depozit" if a["dedicated_trip"]
               else f"cu un ocol de {a['extra_m']} m pe ruta existenta")
        facts.append(f"Depozitarea ilegala {a['id']} e preluata de camionul {a['truck']} {how}.")
    for w in plan.get("warnings", []):
        if "obligatorii" in w:
            facts.append(w + ".")
    return facts


def _fallback(facts):
    return "\n".join(facts)


def explain_plan(plan, bins):
    facts = _facts(plan, bins)
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return _fallback(facts)
    prompt = (
        "Esti asistentul unui dispecer de salubritate din Timisoara. Scrie in romana, "
        "pe scurt (maximum 8 propozitii), ce face fiecare camion azi si de ce. "
        "Explica de ce unele pubele sunt sarite. Foloseste DOAR faptele de mai jos, "
        "nu adauga cifre sau nume de strazi care nu apar in ele.\n\nFapte:\n- "
        + "\n- ".join(facts)
    )
    last_error = None
    for attempt in range(3):                   # erorile 5xx de la Google sunt de obicei temporare
        try:
            from google import genai
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
            return response.text.strip()
        except Exception as e:                 # fara internet, cheie gresita, model indisponibil
            last_error = e
            if "ServerError" not in e.__class__.__name__:
                break                          # eroare care nu trece singura: nu mai incercam
            time.sleep(2 * (attempt + 1))
    return _fallback(facts) + f"\n(explicatie fara Gemini: {last_error})"


if __name__ == "__main__":
    import json
    with open("example_output.json") as f:
        plan = json.load(f)
    with open("bins.json") as f:
        data = json.load(f)
    bins = [{"id": b["id"], "umplere": round(b["fill_level"] * 100)} for b in data["bins"]]
    bins += [{"id": "TM-001", "umplere": 73}, {"id": "TM-002", "umplere": 41},
             {"id": "TM-003", "umplere": 90}]
    print(explain_plan(plan, bins))
