"""Şema keşfi: index'inize bakıp .env alan eşleştirmesini ÖNERİR.

Eski "universal agent"taki `IndexSchema` fikrinden ilham alındı, ama iki
önemli farkla:
1. Keşif **bir kez, kurulumda** yapılır (`python -m cmms_agent discover`),
   her soruda LLM'e şema göndermek yerine. Eski agent her soruda önce
   `get_schema_info` çağırıp tüm alan listesini prompt'a koyuyordu; bu hem
   bir LLM turu hem de yüzlerce token demekti.
2. Sonucu LLM değil, deterministik kurallar üretir; çıktı insan tarafından
   gözden geçirilip .env'ye yazılır. "Hangi alan ekipman ID'si?" kararını
   bir kez doğru vermek, her soruda modele tahmin ettirmekten iyidir.

Kullanılan Elastic API'leri:
- `_field_caps`: tüm alanların tipini ve aggregatable olup olmadığını,
  `.keyword` alt alanları ve iç içe (object) yollar dahil düz liste olarak verir.
  Birden çok index/alias üzerinde de çalışır. (`get_mapping`'i elle
  gezmekten daha sağlam.)
- `terms` aggregation: tip/durum alanlarının GERÇEK değerlerini görmek için.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

KEYWORD = {"keyword", "constant_keyword", "wildcard"}
DATE = {"date", "date_nanos"}
NUMERIC = {"long", "integer", "short", "byte", "double", "float", "half_float",
           "scaled_float", "unsigned_long"}
TEXT = {"text", "match_only_text"}


@dataclass(frozen=True)
class Role:
    env: str
    types: frozenset[str]
    names: tuple[str, ...]      # öncelik sırasıyla aday isimler (normalize edilmiş)
    required: bool = False


# İngilizce + Türkçe yaygın alan adları. Kendi CMMS'inizin adlarını ekleyin.
WO_ROLES: dict[str, Role] = {
    "asset": Role("CMMS_FIELD_ASSET", frozenset(KEYWORD), (
        "asset_id", "assetid", "asset_code", "asset", "equipment_id", "equipment_code",
        "equipment", "ekipman_id", "ekipman_kodu", "ekipman_no", "ekipman", "makine_id",
        "makine_kodu", "makine_no", "makine", "machine_id", "machine", "varlik_id",
        "varlik_kodu", "varlik", "demirbas_no", "tag", "equipment_tag"), True),
    "asset_name": Role("CMMS_FIELD_ASSET_NAME", frozenset(KEYWORD), (
        "asset_name", "equipment_name", "ekipman_adi", "makine_adi", "machine_name",
        "varlik_adi", "asset_description")),
    "wo_type": Role("CMMS_FIELD_TYPE", frozenset(KEYWORD), (
        "type", "wo_type", "work_order_type", "workorder_type", "order_type",
        "maintenance_type", "is_emri_tipi", "is_emri_turu", "bakim_tipi", "bakim_turu",
        "tip", "tur", "category", "kategori"), True),
    "status": Role("CMMS_FIELD_STATUS", frozenset(KEYWORD), (
        "status", "state", "wo_status", "durum", "is_emri_durumu", "statu")),
    "priority": Role("CMMS_FIELD_PRIORITY", frozenset(KEYWORD), (
        "priority", "oncelik", "urgency", "aciliyet")),
    "failure_code": Role("CMMS_FIELD_FAILURE_CODE", frozenset(KEYWORD), (
        "failure_code", "fault_code", "problem_code", "cause_code", "ariza_kodu",
        "ariza_tipi", "ariza_nedeni", "hata_kodu", "failure_mode", "failure_class")),
    "location": Role("CMMS_FIELD_LOCATION", frozenset(KEYWORD), (
        "location", "site", "area", "line", "lokasyon", "konum", "hat", "bolum", "tesis")),
    "wo_id": Role("CMMS_FIELD_WO_ID", frozenset(KEYWORD | NUMERIC), (
        "wo_id", "work_order_id", "workorder_id", "wo_no", "wo_number", "order_no",
        "is_emri_no", "is_emri_id", "ie_no", "ticket_id")),
    "description": Role("CMMS_FIELD_DESCRIPTION", frozenset(TEXT), (
        "description", "desc", "aciklama", "problem_description", "ariza_aciklamasi",
        "notes", "notlar", "summary", "ozet", "comment", "yorum")),
    "created_at": Role("CMMS_FIELD_CREATED_AT", frozenset(DATE), (
        "created_at", "created", "reported_at", "report_date", "failure_date",
        "ariza_tarihi", "bildirim_tarihi", "olusturma_tarihi", "acilis_tarihi",
        "open_date", "opened_at", "@timestamp", "timestamp", "tarih", "date"), True),
    "started_at": Role("CMMS_FIELD_STARTED_AT", frozenset(DATE), (
        "started_at", "start_date", "actual_start", "baslangic_tarihi", "baslama_tarihi",
        "mudahale_tarihi")),
    "completed_at": Role("CMMS_FIELD_COMPLETED_AT", frozenset(DATE), (
        "completed_at", "completion_date", "closed_at", "close_date", "finished_at",
        "end_date", "actual_finish", "kapanis_tarihi", "bitis_tarihi",
        "tamamlanma_tarihi")),
    "downtime_hours": Role("CMMS_FIELD_DOWNTIME", frozenset(NUMERIC), (
        "downtime_hours", "downtime", "durus_suresi", "durus_saat", "durus",
        "outage_hours", "repair_time", "onarim_suresi")),
    "labor_hours": Role("CMMS_FIELD_LABOR", frozenset(NUMERIC), (
        "labor_hours", "labour_hours", "man_hours", "iscilik_saati", "iscilik",
        "adam_saat", "work_hours")),
    "cost": Role("CMMS_FIELD_COST", frozenset(NUMERIC), (
        "cost", "total_cost", "maliyet", "toplam_maliyet", "tutar", "amount")),
}

READING_ROLES: dict[str, Role] = {
    "asset": Role("CMMS_RFIELD_ASSET", frozenset(KEYWORD), WO_ROLES["asset"].names, True),
    "metric": Role("CMMS_RFIELD_METRIC", frozenset(KEYWORD), (
        "metric", "metric_name", "sensor", "sensor_name", "sensor_id", "tag", "tag_name",
        "signal", "parameter", "parametre", "olcum_tipi", "olcum", "measurement"), True),
    "value": Role("CMMS_RFIELD_VALUE", frozenset(NUMERIC), (
        "value", "val", "reading", "deger", "olcum_degeri", "measurement_value"), True),
    "timestamp": Role("CMMS_RFIELD_TIMESTAMP", frozenset(DATE), (
        "@timestamp", "timestamp", "time", "ts", "zaman", "tarih", "olcum_zamani",
        "date"), True),
}

# Değer sınıflandırma kalıpları (normalize edilmiş, büyük/küçük harf duyarsız)
VALUE_PATTERNS = {
    "CMMS_CORRECTIVE_VALUES": r"^(cm|corrective|corr|breakdown|repair|failure|fault|emergency|"
                              r"ariza|arizali|duzeltici|onarim|tamir|acil)",
    "CMMS_PREVENTIVE_VALUES": r"^(pm|preventive|prev|planned|scheduled|periodic|"
                              r"onleyici|periyodik|planli|koruyucu)",
    "CMMS_OPEN_STATUS_VALUES": r"^(open|new|in_?progress|waiting|on_?hold|pending|assigned|"
                               r"acik|yeni|devam|beklemede|bekliyor|atandi|islemde)",
    "CMMS_COMPLETED_STATUS_VALUES": r"^(completed|complete|closed|done|finished|resolved|"
                                    r"kapali|kapandi|tamamlandi|bitti|cozuldu)",
}

_TR = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")


def norm(name: str) -> str:
    """'Ekipman Adı' -> 'ekipman_adi', 'assetId' -> 'asset_id'."""
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name.translate(_TR))
    return re.sub(r"[^a-z0-9@]+", "_", name.lower()).strip("_")


@dataclass
class FieldInfo:
    path: str
    type: str
    aggregatable: bool


@dataclass
class Suggestion:
    role: str
    env: str
    field: str | None
    type: str | None = None
    score: int = 0
    alternatives: list[str] = field(default_factory=list)

    @property
    def confidence(self) -> str:
        return "yüksek" if self.score >= 90 else "orta" if self.score >= 50 else "düşük"


def parse_field_caps(resp: dict) -> list[FieldInfo]:
    out = []
    for path, types in resp.get("fields", {}).items():
        if path.startswith("_"):
            continue
        # Bir alan farklı index'lerde farklı tipte olabilir; en "kullanışlı"sını seç
        for t in sorted(types, key=lambda t: (t not in KEYWORD | DATE | NUMERIC | TEXT, t)):
            if t in ("object", "nested", "unmapped"):
                continue
            out.append(FieldInfo(path, t, bool(types[t].get("aggregatable"))))
            break
    return out


def _score(f: FieldInfo, role: Role) -> int:
    if f.type not in role.types:
        return 0
    base = f.path[:-len(".keyword")] if f.path.endswith(".keyword") else f.path
    full, leaf = norm(base), norm(base.split(".")[-1])
    best = 0
    for i, cand in enumerate(role.names):
        if cand in (full, leaf):
            best = max(best, 100 - min(i, 9))   # tam eşleşme her zaman "yüksek" (≥90)
        elif len(cand) >= 4 and (cand in full):
            best = max(best, 60 - i)
    if best and f.path.endswith(".keyword"):
        best -= 1          # aynı isimde gerçek keyword alan varsa o tercih edilsin
    return max(best, 0)


def suggest_fields(fields: list[FieldInfo], roles: dict[str, Role]) -> list[Suggestion]:
    """Her rol için en iyi alanı seç; bir alan iki role atanmaz (sırayla, açgözlü)."""
    used: set[str] = set()
    out = []
    # Zorunlu roller önce seçilsin (ör. created_at, 'date' alanını kapmadan önce)
    order = sorted(roles, key=lambda r: not roles[r].required)
    for name in order:
        role = roles[name]
        scored = sorted(((s, f) for f in fields
                         if f.path not in used and (s := _score(f, role)) > 0),
                        key=lambda x: -x[0])
        if scored:
            s, f = scored[0]
            used.add(f.path)
            out.append(Suggestion(name, role.env, f.path, f.type, s,
                                  [g.path for _, g in scored[1:4]]))
        else:
            out.append(Suggestion(name, role.env, None))
    return sorted(out, key=lambda s: list(roles).index(s.role))


def classify_values(values: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {k: [] for k in VALUE_PATTERNS}
    for v in values:
        n = norm(str(v))
        for env, pat in VALUE_PATTERNS.items():
            if re.match(pat, n):
                out[env].append(str(v))
    return out


def unmatched_required(sugs: list[Suggestion], roles: dict[str, Role]) -> list[str]:
    return [s.env for s in sugs if s.field is None and roles[s.role].required]


# --------------------------------------------------------------------------- #
# Elastic'e bağlanan kısım
# --------------------------------------------------------------------------- #
def list_indices(es) -> list[dict]:
    rows = es.cat.indices(format="json", h="index,docs.count,store.size")
    return sorted((r for r in rows if not r["index"].startswith(".")),
                  key=lambda r: r["index"])


def top_values(es, index: str, field_path: str, size: int = 20) -> list[tuple[str, int]]:
    r = es.search(index=index, size=0,
                  aggs={"v": {"terms": {"field": field_path, "size": size}}})
    return [(b["key"], b["doc_count"]) for b in r["aggregations"]["v"]["buckets"]]


@dataclass
class DiscoveryReport:
    index: str
    doc_count: int
    suggestions: list[Suggestion]
    value_samples: dict[str, list[tuple[str, int]]]
    value_suggestions: dict[str, list[str]]
    missing: list[str]
    readings_index: str | None = None
    reading_suggestions: list[Suggestion] = field(default_factory=list)
    reading_missing: list[str] = field(default_factory=list)
    metric_samples: list[tuple[str, int]] = field(default_factory=list)

    def env_text(self) -> str:
        lines = [f"# python -m cmms_agent discover çıktısı — GÖZDEN GEÇİRİP .env'ye kopyalayın",
                 f"# index: {self.index} ({self.doc_count} belge)",
                 f"CMMS_WO_INDEX={self.index}"]
        for s in self.suggestions:
            if s.field:
                alt = f"  (alternatifler: {', '.join(s.alternatives)})" if s.alternatives else ""
                lines.append(f"{s.env}={s.field}   # {s.type}, güven: {s.confidence}{alt}")
            else:
                lines.append(f"# {s.env}=   # BULUNAMADI — elle doldurun")
        for env, vals in self.value_suggestions.items():
            if vals:
                lines.append(f"{env}={','.join(vals)}")
            else:
                lines.append(f"# {env}=   # değer eşleşmedi — aşağıdaki örneklere bakın")
        for role, samples in self.value_samples.items():
            if samples:
                vals = ", ".join(f"{k} ({n})" for k, n in samples)
                lines.append(f"#   {role} alanının değerleri: {vals}")
        if self.readings_index:
            lines.append(f"\nCMMS_READINGS_INDEX={self.readings_index}")
            for s in self.reading_suggestions:
                lines.append(f"{s.env}={s.field}   # {s.type}, güven: {s.confidence}"
                             if s.field else f"# {s.env}=   # BULUNAMADI")
            if self.metric_samples:
                lines.append("#   metrikler: " + ", ".join(k for k, _ in self.metric_samples))
        return "\n".join(lines) + "\n"


def discover(es, index: str, readings_index: str | None = None) -> DiscoveryReport:
    fields = parse_field_caps(es.field_caps(index=index, fields="*"))
    sugs = suggest_fields(fields, WO_ROLES)
    by_role = {s.role: s for s in sugs}
    samples: dict[str, list[tuple[str, int]]] = {}
    all_values: list[str] = []
    for role in ("wo_type", "status"):
        s = by_role[role]
        if s.field:
            samples[role] = top_values(es, index, s.field)
            all_values += [k for k, _ in samples[role]]
    # Tip değerlerinden düzeltici/önleyici, durum değerlerinden açık/kapalı
    type_cls = classify_values([k for k, _ in samples.get("wo_type", [])])
    status_cls = classify_values([k for k, _ in samples.get("status", [])])
    value_sugs = {
        "CMMS_CORRECTIVE_VALUES": type_cls["CMMS_CORRECTIVE_VALUES"],
        "CMMS_PREVENTIVE_VALUES": type_cls["CMMS_PREVENTIVE_VALUES"],
        "CMMS_OPEN_STATUS_VALUES": status_cls["CMMS_OPEN_STATUS_VALUES"],
        "CMMS_COMPLETED_STATUS_VALUES": status_cls["CMMS_COMPLETED_STATUS_VALUES"],
    }
    rep = DiscoveryReport(
        index=index, doc_count=es.count(index=index)["count"], suggestions=sugs,
        value_samples=samples, value_suggestions=value_sugs,
        missing=unmatched_required(sugs, WO_ROLES))
    if readings_index and es.indices.exists(index=readings_index):
        rf = parse_field_caps(es.field_caps(index=readings_index, fields="*"))
        rs = suggest_fields(rf, READING_ROLES)
        rep.readings_index = readings_index
        rep.reading_suggestions = rs
        rep.reading_missing = unmatched_required(rs, READING_ROLES)
        metric = next((s for s in rs if s.role == "metric"), None)
        if metric and metric.field:
            rep.metric_samples = top_values(es, readings_index, metric.field)
    return rep
