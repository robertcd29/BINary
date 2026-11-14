"""Descarca din OpenStreetMap benzile strazilor din Timisoara (cate benzi, si incotro duce fiecare).

Rulat o singura data; rezultatul (benzi_osm.json) e folosit de navigatie pentru recomandarea benzii.
"""
import json
import urllib.parse
import urllib.request

QUERY = """[out:json][timeout:90];
way["highway"]["highway"!~"footway|path|cycleway|steps|pedestrian|service|track"](45.70,21.185,45.79,21.28)->.w;
(way.w[~"^turn:lanes"~"."];way.w[~"^lanes"~"."];);
out tags;"""
KEYS = ("lanes", "lanes:forward", "lanes:backward", "turn:lanes", "turn:lanes:forward", "turn:lanes:backward", "oneway")

req = urllib.request.Request("https://overpass-api.de/api/interpreter",
                             data=urllib.parse.urlencode({"data": QUERY}).encode(),
                             headers={"User-Agent": "BINary-routing/1.0"})
ways = json.load(urllib.request.urlopen(req, timeout=120))["elements"]
out = {str(w["id"]): {k: w["tags"][k] for k in KEYS if k in w["tags"]} for w in ways}
with open("benzi_osm.json", "w") as f:
    json.dump(out, f, separators=(",", ":"), ensure_ascii=False)
print(len(out), "strazi cu benzi")
