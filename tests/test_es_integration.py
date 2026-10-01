"""Gerçek Elasticsearch'e karşı entegrasyon testi.

Çalıştırmak için: docker compose up -d elasticsearch
                  python scripts/seed_sample_data.py --reset
                  pytest tests/test_es_integration.py
ES yoksa testler atlanır.
"""
import pytest

from cmms_agent.config import load_settings
from cmms_agent.es_client import CMMSRepository, build_client
from cmms_agent.tools import TOOLS, ToolContext

s = load_settings()
repo = CMMSRepository(build_client(s), s)
pytestmark = pytest.mark.skipif(
    not repo.ping() or not repo.es.indices.exists(index=s.wo_index),
    reason="Elasticsearch / örnek veri yok",
)
ctx = ToolContext(repo=repo, default_days=365)


@pytest.mark.parametrize("name,args", [
    ("kpi_summary", {}),
    ("top_failing_assets", {"top_n": 3}),
    ("asset_reliability", {"asset_id": "kompresör 3"}),
    ("failure_risk_ranking", {"horizon_days": 30}),
    ("workorder_trend", {"interval": "month"}),
    ("failure_modes", {}),
    ("search_workorders", {"text": "rulman"}),
    ("open_backlog", {}),
    ("sensor_health", {}),
    ("sensor_health", {"asset_id": "KMP-003"}),
])
def test_tool_runs(name, args):
    out = TOOLS[name].fn(ctx, args)
    assert "error" not in out, out


def test_seeded_story_is_found():
    top = TOOLS["failure_risk_ranking"].fn(ctx, {"top_n": 3})
    assert "KMP-003" in [r["asset_id"] for r in top["ranking"]]
    rel = TOOLS["asset_reliability"].fn(ctx, {"asset_id": "KMP-003"})
    assert rel["asset_id"] == "KMP-003" and rel["failures"] >= 5


def test_sensor_story_is_found():
    out = TOOLS["sensor_health"].fn(ctx, {})
    by = {(r["asset_id"], r["metric"]): r["status"] for r in out["readings"]}
    assert by.get(("KMP-003", "vibration_mm_s")) == "kritik"
    assert by.get(("FRN-002", "temperature_c")) in ("uyarı", "kritik")
    top = TOOLS["failure_risk_ranking"].fn(ctx, {"top_n": 1})
    assert top["sensor_data"] and top["ranking"][0]["asset_id"] == "KMP-003"
    assert top["ranking"][0]["sensor_status"] == "kritik"


@pytest.fixture(scope="module")
def turkish_index():
    """Mapping VERMEDEN yüklenmiş (dinamik mapping: text + .keyword) Türkçe index."""
    from elasticsearch import helpers
    idx = "test-tr-isemirleri"
    if repo.es.indices.exists(index=idx):
        repo.es.indices.delete(index=idx)
    docs = [{"IsEmriNo": f"IE-{i}", "Ekipman": "KMP-03" if i % 3 else "PMP-12",
             "IsEmriTipi": "Arıza" if i % 2 else "Periyodik Bakım",
             "Durum": "Açık" if i % 5 == 0 else "Kapalı", "Lokasyon": {"Hat": "Hat-A"},
             "AcilisTarihi": f"2026-0{1 + i % 9}-1{i % 10}T08:00:00Z", "DurusSuresi": 1.5,
             "ArizaAciklamasi": "Rulmandan ses geliyor"} for i in range(60)]
    helpers.bulk(repo.es, ({"_index": idx, "_source": d} for d in docs), refresh="wait_for")
    yield idx
    repo.es.indices.delete(index=idx)


def test_discover_and_run_tools_on_turkish_index(turkish_index):
    """Uçtan uca: keşfet -> öneriyi ayar yap -> araçlar çalışsın."""
    import dataclasses

    from cmms_agent.discovery import discover

    rep = discover(repo.es, turkish_index)
    m = {sg.role: sg.field for sg in rep.suggestions}
    assert m["asset"] == "Ekipman.keyword" and m["created_at"] == "AcilisTarihi"
    assert rep.value_suggestions["CMMS_CORRECTIVE_VALUES"] == ["Arıza"]

    fm = dataclasses.replace(
        s.fields, asset=m["asset"], wo_type=m["wo_type"], status=m["status"],
        created_at=m["created_at"], downtime_hours=m["downtime_hours"],
        description=m["description"], wo_id=m["wo_id"], location=m["location"],
        corrective_values=tuple(rep.value_suggestions["CMMS_CORRECTIVE_VALUES"]),
        open_status_values=tuple(rep.value_suggestions["CMMS_OPEN_STATUS_VALUES"]))
    s2 = dataclasses.replace(s, wo_index=turkish_index, fields=fm, readings_index="yok")
    ctx2 = ToolContext(repo=CMMSRepository(repo.es, s2), default_days=3650)

    rel = TOOLS["asset_reliability"].fn(ctx2, {"asset_id": "kmp-03"})
    assert rel["asset_id"] == "KMP-03" and rel["failures"] > 5
    hits = TOOLS["search_workorders"].fn(ctx2, {"text": "rulman"})["matches"]
    assert hits and all(h["Ekipman"] in ("KMP-03", "PMP-12") for h in hits)  # önek + _source yolu
    backlog = TOOLS["open_backlog"].fn(ctx2, {})
    assert backlog["open_total"] == 12 and backlog["oldest_open"][0]["IsEmriNo"]
