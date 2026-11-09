"""De la detectiile camerei (sau raportarile cetatenilor) la o alerta pentru rutare.

Inserarea in ruta exista deja (routing_engine.add_alert). Aici e partea dinainte:
- confirmare: alerta pleaca doar dupa CONFIRM_FRAMES cadre consecutive cu detectie,
  ca un om care trece cu o sacosa sa nu trimita un camion;
- volum: YOLO da doar dreptunghiuri, deci marimea (mica/medie/mare) o deducem din cat
  din cadru acopera gunoiul;
- deduplicare: camera trimite o poza la 15 s, iar mai multi cetateni pot raporta aceeasi
  gramada. Tot ce e la sub MERGE_RADIUS_M de o alerta deschisa se lipeste de ea.
"""
import math

CONFIRM_FRAMES = 3          # camera trimite la 15 s, deci ~45 s de detectie continua
MIN_CONF = 0.5              # sub acest scor YOLO, detectia e ignorata
MERGE_RADIUS_M = 50
SIZE_THRESHOLDS = ((0.05, "mica"), (0.20, "medie"))   # fractiune din cadru; peste => "mare"


class FrameConfirmer:
    """Numara cadrele consecutive cu detectie, separat pentru fiecare camera."""

    def __init__(self, frames=CONFIRM_FRAMES):
        self.frames = frames
        self.streak = {}

    def update(self, camera_id, detected):
        """True o singura data: exact la cadrul care confirma depozitarea."""
        if not detected:
            self.streak[camera_id] = 0
            return False
        self.streak[camera_id] = self.streak.get(camera_id, 0) + 1
        return self.streak[camera_id] == self.frames


def relevant(detections, classes=None, min_conf=MIN_CONF):
    """Detectiile care conteaza. classes=None accepta orice clasa (modelul e antrenat doar pe gunoi)."""
    out = []
    for d in detections or []:
        try:
            conf = float(d.get("conf", 0))
            x1, y1, x2, y2 = (float(v) for v in d["bbox"])
        except (KeyError, TypeError, ValueError):
            continue
        if conf < min_conf or (classes is not None and d.get("clasa") not in classes):
            continue
        out.append({**d, "conf": conf, "bbox": [x1, y1, x2, y2]})
    return out


def size_from_boxes(detections, width, height):
    """Marimea depozitarii din aria acoperita de dreptunghiuri (aproximativ, suprapunerile nu se scad)."""
    frame = float(width) * float(height)
    if frame <= 0:
        return "medie"
    covered = sum(max(0.0, d["bbox"][2] - d["bbox"][0]) * max(0.0, d["bbox"][3] - d["bbox"][1])
                  for d in detections)
    fraction = min(1.0, covered / frame)
    for limit, name in SIZE_THRESHOLDS:
        if fraction < limit:
            return name
    return "mare"


def distance_m(lat1, lng1, lat2, lng2):
    """Distanta in linie dreapta (haversine), in metri."""
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def find_duplicate(open_alerts, lat, lng, radius=MERGE_RADIUS_M):
    """Cea mai apropiata alerta deschisa la mai putin de `radius` metri, sau None."""
    best = None
    for a in open_alerts:
        d = distance_m(lat, lng, a["lat"], a["lng"])
        if d <= radius and (best is None or d < best[0]):
            best = (d, a)
    return best[1] if best else None
