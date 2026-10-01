from cmms_agent.config import load_settings
from cmms_agent.discovery import (READING_ROLES, WO_ROLES, classify_values, norm,
                                  parse_field_caps, suggest_fields, unmatched_required)
from cmms_agent.es_client import get_path, project, source_path


def caps(**fields):
    """{'Ekipman': 'text', 'Ekipman.keyword': 'keyword'} -> _field_caps cevabı."""
    return {"fields": {k: {t: {"type": t, "aggregatable": t != "text"}}
                       for k, t in fields.items()}}


TURKISH_DYNAMIC = caps(**{
    "IsEmriNo": "text", "IsEmriNo.keyword": "keyword",
    "Ekipman": "text", "Ekipman.keyword": "keyword",
    "EkipmanAdi": "text", "EkipmanAdi.keyword": "keyword",
    "IsEmriTipi": "text", "IsEmriTipi.keyword": "keyword",
    "Durum": "text", "Durum.keyword": "keyword",
    "Lokasyon.Hat": "text", "Lokasyon.Hat.keyword": "keyword",
    "AcilisTarihi": "date", "KapanisTarihi": "date",
    "DurusSuresi": "float", "Maliyet": "float",
    "ArizaAciklamasi": "text", "_id": "_id",
})


def by_env(sugs):
    return {s.env: s.field for s in sugs}


def test_norm():
    assert norm("IsEmriNo") == "is_emri_no"
    assert norm("Ekipman Adı") == "ekipman_adi"
    assert norm("@timestamp") == "@timestamp"


def test_turkish_dynamic_mapping():
    sugs = suggest_fields(parse_field_caps(TURKISH_DYNAMIC), WO_ROLES)
    m = by_env(sugs)
    assert m["CMMS_FIELD_ASSET"] == "Ekipman.keyword"
    assert m["CMMS_FIELD_ASSET_NAME"] == "EkipmanAdi.keyword"
    assert m["CMMS_FIELD_TYPE"] == "IsEmriTipi.keyword"
    assert m["CMMS_FIELD_STATUS"] == "Durum.keyword"
    assert m["CMMS_FIELD_LOCATION"] == "Lokasyon.Hat.keyword"
    assert m["CMMS_FIELD_CREATED_AT"] == "AcilisTarihi"
    assert m["CMMS_FIELD_COMPLETED_AT"] == "KapanisTarihi"
    assert m["CMMS_FIELD_DOWNTIME"] == "DurusSuresi"
    assert m["CMMS_FIELD_DESCRIPTION"] == "ArizaAciklamasi"
    assert unmatched_required(sugs, WO_ROLES) == []
    assert next(s for s in sugs if s.role == "asset").confidence == "yüksek"


def test_field_never_assigned_twice():
    sugs = suggest_fields(parse_field_caps(caps(date="date")), WO_ROLES)
    assigned = [s.field for s in sugs if s.field]
    assert assigned == ["date"]                       # sadece created_at alır
    assert by_env(sugs)["CMMS_FIELD_CREATED_AT"] == "date"


def test_missing_required_reported():
    sugs = suggest_fields(parse_field_caps(caps(foo="keyword")), WO_ROLES)
    assert "CMMS_FIELD_ASSET" in unmatched_required(sugs, WO_ROLES)


def test_readings_roles():
    sugs = suggest_fields(parse_field_caps(caps(**{
        "tag_name": "keyword", "machine_id": "keyword", "val": "double",
        "@timestamp": "date"})), READING_ROLES)
    assert by_env(sugs) == {"CMMS_RFIELD_ASSET": "machine_id", "CMMS_RFIELD_METRIC": "tag_name",
                            "CMMS_RFIELD_VALUE": "val", "CMMS_RFIELD_TIMESTAMP": "@timestamp"}


def test_classify_values():
    c = classify_values(["Arıza", "Periyodik Bakım", "Kalibrasyon", "CM", "PM",
                         "Açık", "Beklemede", "Kapalı", "İptal", "in_progress"])
    assert c["CMMS_CORRECTIVE_VALUES"] == ["Arıza", "CM"]
    assert c["CMMS_PREVENTIVE_VALUES"] == ["Periyodik Bakım", "PM"]
    assert c["CMMS_OPEN_STATUS_VALUES"] == ["Açık", "Beklemede", "in_progress"]
    assert c["CMMS_COMPLETED_STATUS_VALUES"] == ["Kapalı"]


def test_source_paths():
    doc = {"Ekipman": "KMP-03", "Lokasyon": {"Hat": "Hat-A"}, "a.b": 1}
    assert source_path("Ekipman.keyword") == "Ekipman"
    assert get_path(doc, "Lokasyon.Hat") == "Hat-A"
    assert get_path(doc, "a.b") == 1 and get_path(doc, "yok.x") is None
    assert project(doc, ["Ekipman.keyword", "Lokasyon.Hat.keyword"]) == {
        "Ekipman.keyword": "KMP-03", "Lokasyon.Hat.keyword": "Hat-A"}


def test_env_inline_comments(tmp_path, monkeypatch):
    for k in ("CMMS_FIELD_ASSET", "CMMS_WO_INDEX", "CMMS_CORRECTIVE_VALUES"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text("CMMS_FIELD_ASSET=Ekipman.keyword   # keyword, güven: yüksek\n"
                   'CMMS_WO_INDEX="idx #1"\n'
                   "CMMS_CORRECTIVE_VALUES=Arıza,Acil Arıza\n", encoding="utf-8")
    s = load_settings(str(env))
    assert s.fields.asset == "Ekipman.keyword"
    assert s.wo_index == "idx #1"
    assert s.fields.corrective_values == ("Arıza", "Acil Arıza")
