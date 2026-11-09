"""Teste pentru filtrarea senzorilor si pipeline-ul de depozitari (fara harta, ruleaza instant).

Rulare: python -m pytest test_filters.py -q   (din folderul routing)
"""
from datetime import datetime, timedelta, timezone

import dumping
import sensor_filter as sf

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)


# ---------- senzori ----------

def test_valid_respinge_erorile_de_senzor():
    assert sf.valid(None) is None
    assert sf.valid(-1) is None
    assert sf.valid("abc") is None
    assert sf.valid(float("nan")) is None
    assert sf.valid(130) == 100.0
    assert sf.valid(0) == 0.0          # 0 valid ramane 0: pubela chiar goala


def test_mediana_ignora_un_varf_izolat():
    assert sf.smooth([40, 41, 95, 42, 41]) == 41
    assert sf.smooth([None, -1, 50]) == 50
    assert sf.smooth([None, -1]) is None


def test_histerezis_nu_oscileaza():
    states, urgent = [], False
    for level in [78, 81, 79, 82, 75, 71, 69, 79, 81]:
        urgent = sf.next_urgent(urgent, level)
        states.append(urgent)
    # devine urgenta la 81, ramane asa pana scade sub 70, apoi iar la 81
    assert states == [False, True, True, True, True, True, False, False, True]
    assert sf.next_urgent(True, None) is True


def test_senzor_proaspat():
    r = sf.routing_fill(55, NOW - timedelta(minutes=2), NOW - timedelta(minutes=2), NOW)
    assert r == {"umplere": 55, "sursa": "senzor", "motiv": None}


def test_senzor_defect_estimeaza_nu_raporteaza_gol():
    # citiri recente, dar toate invalide: ultima buna acum 2 ore, la 60%
    r = sf.routing_fill(60, NOW - timedelta(hours=2), NOW - timedelta(seconds=10), NOW, rate_pct_h=5)
    assert r["sursa"] == "estimat" and r["umplere"] == 70
    assert "defect" in r["motiv"]


def test_senzor_mut():
    r = sf.routing_fill(60, NOW - timedelta(hours=2), NOW - timedelta(hours=2), NOW)
    assert r["sursa"] == "estimat" and "nu mai transmite" in r["motiv"]


def test_fara_nicio_citire_buna_e_vizitata():
    r = sf.routing_fill(None, None, NOW, NOW)
    assert r["sursa"] == "necunoscut" and r["umplere"] == sf.UNKNOWN_FILL


# ---------- depozitari ilegale ----------

def test_confirmare_doar_dupa_cadre_consecutive():
    c = dumping.FrameConfirmer(frames=3)
    seq = [True, True, False, True, True, True, True, True]
    assert [c.update("CAM-01", d) for d in seq] == [False, False, False, False, False, True, False, False]
    # alta camera are numaratoarea ei
    assert c.update("CAM-02", True) is False


def test_detectii_slabe_sau_stricate_sunt_ignorate():
    dets = [{"conf": 0.9, "bbox": [0, 0, 10, 10]},
            {"conf": 0.2, "bbox": [0, 0, 10, 10]},
            {"conf": 0.9},
            {"conf": 0.8, "bbox": [0, 0, 5, 5], "clasa": "persoana"}]
    assert len(dumping.relevant(dets)) == 2
    assert len(dumping.relevant(dets, classes={"gunoi"})) == 0


def test_marime_din_aria_acoperita():
    box = lambda w, h: {"bbox": [0, 0, w, h]}
    assert dumping.size_from_boxes([box(20, 20)], 160, 120) == "mica"     # 2%
    assert dumping.size_from_boxes([box(60, 40)], 160, 120) == "medie"    # 12.5%
    assert dumping.size_from_boxes([box(120, 90)], 160, 120) == "mare"    # 56%
    assert dumping.size_from_boxes([box(10, 10)], 0, 0) == "medie"


def test_deduplicare_pe_raza():
    open_alerts = [{"id": "AL-1", "lat": 45.7475, "lng": 21.2395},
                   {"id": "AL-2", "lat": 45.7600, "lng": 21.2300}]
    # ~30 m mai la nord de AL-1
    assert dumping.find_duplicate(open_alerts, 45.74777, 21.2395)["id"] == "AL-1"
    # ~200 m: alerta noua
    assert dumping.find_duplicate(open_alerts, 45.7493, 21.2395) is None
