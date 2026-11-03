"""Teste pe cazuri limita: modulul nu trebuie sa arunce exceptii, orice s-ar intampla in demo."""
import json
import time
import routing_engine as re_

with open("bins.json") as f:
    data = json.load(f)
with open("history.json") as f:
    history = json.load(f)
bins = [{"id": b["id"], "lat": b["lat"], "lng": b["lon"],
         "umplere": round(b["fill_level"] * 100), "capacitate_l": b["capacity_l"]}
        for b in data["bins"]]
passed = 0

def check(name, cond):
    global passed
    assert cond, name
    passed += 1
    print("OK  ", name)

p = re_.plan_routes([])
check("lista goala: camioanele raman in depozit", p["metrics"]["trucks_used"] == 0)

p = re_.plan_routes(bins[:3] + [bins[0]])
check("id duplicat: raportat si ignorat", any("mai multe ori" in w for w in p["warnings"]))

p = re_.plan_routes([{"id": "X", "lat": None, "lng": 21.2, "umplere": 50}] + bins[:3])
check("coordonate lipsa: raportate, restul merge", any("X" in w for w in p["warnings"])
      and p["metrics"]["bins_total"] == 3)

p = re_.plan_routes([{**bins[0], "umplere": 140}])
check("umplere peste 100: limitata", any("limitata" in w for w in p["warnings"]))

full = [{**b, "umplere": 100} for b in bins]          # 60 pubele pline, nu pot incapea toate
p = re_.plan_routes(full)
check("prea mult gunoi obligatoriu: plan partial, nu eroare",
      "routes" in p and p["metrics"]["mandatory_unserved"] > 0)

r = re_.add_alert(p, {"id": "AL-FULL", "lat": 45.7475, "lng": 21.2395, "volum_l": 5000})
check("alerta fara loc in camioane: pending, nu eroare", r["status"] == "pending")

p = re_.plan_routes(bins, history)
r = re_.add_alert(p, {"id": "CIT-1", "lat": 45.7475, "lng": 21.2395, "marime": "mare"})
check("raportare cetatean cu marime: volum 1500 L", r["volum_l"] == 1500 and r["status"] == "assigned")

r = re_.add_alert(p, {"id": "AL-FAR", "lat": 46.5, "lng": 23.0, "marime": "mica"})
check("alerta in afara hartii: respinsa", r["status"] == "rejected")

t = time.time(); re_.plan_routes(bins, history, compare_baseline=True); t1 = time.time() - t
t = time.time(); re_.plan_routes(bins, history, compare_baseline=True); t2 = time.time() - t
print(f"     prima planificare cu baseline: {t1:.1f} s, a doua (baseline din cache): {t2:.1f} s")
check("a doua planificare e mai rapida", t2 < t1)

print(f"\n{passed} teste trecute")
