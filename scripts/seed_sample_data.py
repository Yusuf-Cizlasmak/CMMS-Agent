"""Sentetik CMMS verisi üretip Elasticsearch'e yükler (geliştirme/deneme için).

Kasıtlı olarak içine "hikâyeler" gömdük ki agent'ın bulup bulamadığını görelim:
  - KMP-003 (Kompresör 3): arızalar son aylarda SIKLAŞIYOR (Weibull beta>1)
  - PMP-012 (Pompa 12): sık ama düzenli arıza (sabit oran)
  - KNV-001 (Konveyör 1): çok az arıza (sağlıklı)
  - "rulman" kelimesi geçen açıklamalar, arıza kodları, açık iş emirleri...

Kullanım:
    python scripts/seed_sample_data.py --reset
"""
from __future__ import annotations

import argparse
import math
import os
import random
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from elasticsearch import helpers  # noqa: E402

from cmms_agent.config import load_settings  # noqa: E402
from cmms_agent.es_client import build_client  # noqa: E402

# Önerilen mapping. Kendi index'inizde de keyword/date tiplerine dikkat edin:
# terms aggregation "text" alanda çalışmaz, "keyword" ister.
MAPPING = {
    "properties": {
        "wo_id": {"type": "keyword"},
        "asset_id": {"type": "keyword"},
        "asset_name": {"type": "keyword"},
        "location": {"type": "keyword"},
        "type": {"type": "keyword"},
        "status": {"type": "keyword"},
        "priority": {"type": "keyword"},
        "failure_code": {"type": "keyword"},
        "description": {"type": "text", "analyzer": "turkish"},
        "created_at": {"type": "date"},
        "started_at": {"type": "date"},
        "completed_at": {"type": "date"},
        "downtime_hours": {"type": "float"},
        "labor_hours": {"type": "float"},
        "cost": {"type": "float"},
        "technician": {"type": "keyword"},
    }
}

ASSETS = [
    # id, ad, lokasyon, profil, temel MTBF (gün)
    ("KMP-003", "Kompresör 3", "Hat-A", "aging", 40),
    ("PMP-012", "Pompa 12", "Hat-A", "random", 18),
    ("KNV-001", "Konveyör 1", "Hat-B", "healthy", 200),
    ("CNC-007", "CNC Tezgah 7", "Hat-B", "random", 35),
    ("CNC-008", "CNC Tezgah 8", "Hat-B", "random", 60),
    ("FRN-002", "Fırın 2", "Hat-C", "aging", 70),
    ("HYD-004", "Hidrolik Pres 4", "Hat-C", "random", 45),
    ("CHL-001", "Chiller 1", "Utility", "healthy", 150),
    ("ROB-005", "Robot Kol 5", "Hat-A", "infant", 50),
    ("GEN-001", "Jeneratör 1", "Utility", "healthy", 300),
]

FAILURES = {
    "BRG": ["Rulmandan anormal ses geliyor", "Rulman aşırı ısınıyor", "Rulman değişimi gerekli"],
    "LEAK": ["Yağ kaçağı tespit edildi", "Hidrolik hortumdan sızıntı", "Conta kaçak yapıyor"],
    "ELEC": ["Motor sürücü arızası", "Sigorta attı, motor durdu", "Sensör kablosu kopuk"],
    "VIB": ["Yüksek titreşim alarmı", "Kaplin hizasızlığı, titreşim", "Balanssızlık tespit edildi"],
    "OVH": ["Aşırı ısınma alarmı", "Soğutma fanı çalışmıyor", "Termal koruma devreye girdi"],
    "PLC": ["PLC haberleşme hatası", "HMI donuyor", "Program hatası, reset gerekti"],
}
TECHS = ["ahmet", "ayse", "mehmet", "zeynep", "can", "elif"]


def failure_times(profile: str, mtbf: float, start: datetime, end: datetime,
                  rng: random.Random) -> list[datetime]:
    """Profile göre arıza zamanları üret (Weibull süreçleri)."""
    beta = {"aging": 2.5, "random": 1.0, "healthy": 1.0, "infant": 0.6}[profile]
    t, out = start, []
    total = (end - start).days
    while True:
        # aging: zamanla ölçek küçülür -> arızalar sıklaşır
        progress = (t - start).days / total
        scale = mtbf * (1.0 - 0.7 * progress) if profile == "aging" else mtbf
        eta = scale / math.gamma(1 + 1 / beta)
        gap = rng.weibullvariate(eta, beta)
        t = t + timedelta(days=max(gap, 0.5))
        if t >= end:
            return out
        out.append(t)


def generate(days: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    docs, n = [], 0

    def new_id() -> str:
        nonlocal n
        n += 1
        return f"WO-{n:06d}"

    for asset_id, name, loc, profile, mtbf in ASSETS:
        # Düzeltici (arıza) iş emirleri
        codes = list(FAILURES)
        weights = [5 if (asset_id == "KMP-003" and c in ("BRG", "VIB")) else 1 for c in codes]
        for ts in failure_times(profile, mtbf, start, end, rng):
            code = rng.choices(codes, weights)[0]
            downtime = round(rng.lognormvariate(1.2, 0.6), 1)
            is_open = (end - ts).days < 5 and rng.random() < 0.6
            started = ts + timedelta(minutes=rng.randint(10, 180))
            docs.append({
                "wo_id": new_id(), "asset_id": asset_id, "asset_name": name,
                "location": loc, "type": "corrective",
                "status": rng.choice(["open", "in_progress", "waiting_parts"]) if is_open
                else "completed",
                "priority": rng.choices(["P1", "P2", "P3"], [3, 5, 2])[0],
                "failure_code": code,
                "description": f"{name}: {rng.choice(FAILURES[code])}",
                "created_at": ts.isoformat(), "started_at": started.isoformat(),
                "completed_at": None if is_open
                else (started + timedelta(hours=downtime)).isoformat(),
                "downtime_hours": None if is_open else downtime,
                "labor_hours": round(downtime * rng.uniform(0.8, 2.0), 1),
                "cost": round(downtime * rng.uniform(800, 2500), 0),
                "technician": rng.choice(TECHS),
            })
        # Önleyici bakım (PM) iş emirleri: ayda bir, ~%85 zamanında tamamlanır
        t = start + timedelta(days=rng.randint(0, 29))
        while t < end:
            done = rng.random() < 0.85
            docs.append({
                "wo_id": new_id(), "asset_id": asset_id, "asset_name": name,
                "location": loc, "type": "preventive",
                "status": "completed" if done else ("open" if (end - t).days < 20 else "cancelled"),
                "priority": "P3", "failure_code": None,
                "description": f"{name}: aylık periyodik bakım, yağlama ve kontrol",
                "created_at": t.isoformat(), "started_at": t.isoformat(),
                "completed_at": (t + timedelta(hours=2)).isoformat() if done else None,
                "downtime_hours": 2.0 if done else None,
                "labor_hours": 2.5 if done else None,
                "cost": 600.0 if done else None,
                "technician": rng.choice(TECHS),
            })
            t += timedelta(days=30)
    return docs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--reset", action="store_true", help="index'i silip yeniden oluştur")
    args = ap.parse_args()

    s = load_settings()
    es = build_client(s)
    if args.reset and es.indices.exists(index=s.wo_index):
        es.indices.delete(index=s.wo_index)
    if not es.indices.exists(index=s.wo_index):
        es.indices.create(index=s.wo_index, mappings=MAPPING,
                          settings={"number_of_shards": 1, "number_of_replicas": 0})
    docs = generate(args.days, args.seed)
    actions = ({"_index": s.wo_index, "_id": d["wo_id"], "_source": d} for d in docs)
    ok, _ = helpers.bulk(es, actions, refresh="wait_for")
    print(f"{ok} iş emri '{s.wo_index}' index'ine yüklendi.")


if __name__ == "__main__":
    main()
