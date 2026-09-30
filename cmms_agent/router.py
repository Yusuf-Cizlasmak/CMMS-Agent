"""Niyet yönlendirici (intent router): soruyu hangi araç cevaplamalı?

Hız stratejisi ("hybrid" mod):
  1. Önce KURALLAR (regex + anahtar kelime). ~0 ms. Sık sorulan soruların
     çoğu buradan çıkar ve LLM çağrısı hiç yapılmaz.
  2. Kurallar emin değilse LLM'e sor; ama serbest metin değil, JSON şemasıyla
     kısıtlanmış kısa bir çıktı (~20-40 token) iste.
  3. Sayılar/ID'ler gibi varlıkları (entity) her zaman regex ile de çıkar ve
     LLM'in kaçırdıklarını tamamla. Küçük modeller sayıları karıştırabilir.

Klasik "ReAct" döngüsü (düşün → araç → düşün → araç ...) Jetson'da her tur
için saniyeler demek. Tek atış yönlendirme + tek atış anlatım = 2 LLM çağrısı.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .tools import TOOLS

NONE_TOOL = "none"   # veri gerektirmeyen genel soru ("MTBF nedir?")


@dataclass
class Route:
    tool: str
    args: dict = field(default_factory=dict)
    source: str = "rules"   # rules | llm | fallback


def tr_lower(text: str) -> str:
    """Türkçe'ye uygun küçük harf: 'İ'->'i', 'I'->'ı'."""
    return text.replace("İ", "i").replace("I", "ı").lower()


_UNIT_DAYS = {"gün": 1, "gun": 1, "hafta": 7, "ay": 30, "yıl": 365, "yil": 365, "sene": 365}
_ASSET_RE = re.compile(r"\b([A-Za-zÇĞİÖŞÜçğıöşü]{2,6}-\d{1,6}|[A-Z]{2,6}\d{2,6})\b")


def extract_entities(question: str, now: datetime | None = None) -> dict:
    q = tr_lower(question)
    args: dict = {}

    m = re.search(r"son\s+(\d+)\s*(gün|gun|hafta|ay|yıl|yil|sene)", q)
    if m:
        args["days"] = int(m.group(1)) * _UNIT_DAYS[m.group(2)]
    elif re.search(r"\bbu\s+yıl", q):
        now = now or datetime.now(timezone.utc)
        args["days"] = max(1, now.timetuple().tm_yday)
    else:
        for pat, d in ((r"\bson\s+(bir\s+)?hafta", 7), (r"\b(son|geçen)\s+(bir\s+)?ay\b", 30),
                       (r"\bson\s+(bir\s+)?yıl|\bgeçen\s+yıl", 365), (r"\bbugün", 1)):
            if re.search(pat, q):
                args["days"] = d
                break

    m = re.search(r"(önümüzdeki|gelecek|sonraki|içinde)\s+(\d+)\s*(gün|gun|hafta|ay)", q) \
        or re.search(r"(?<!son )(?<!\d)(\d+)\s*(gün|gun|hafta|ay)\s+içinde", q)
    if m:
        n, unit = (m.group(2), m.group(3)) if len(m.groups()) == 3 else (m.group(1), m.group(2))
        args["horizon_days"] = int(n) * _UNIT_DAYS[unit]
    elif re.search(r"(önümüzdeki|gelecek)\s+hafta", q):
        args["horizon_days"] = 7
    elif re.search(r"(önümüzdeki|gelecek)\s+ay", q):
        args["horizon_days"] = 30

    m = re.search(r"(?:ilk|en\s+(?:çok|fazla|riskli|kötü)|top)\s*(\d{1,2})\b", q) \
        or re.search(r"\b(\d{1,2})\s*(?:ekipman|makine|varlık|cihaz)", q)
    if m:
        args["top_n"] = int(m.group(1))

    m = _ASSET_RE.search(question)
    if m and not re.fullmatch(r"(?i)top\d+", m.group(1)):
        args["asset_id"] = m.group(1).upper()

    m = re.search(r"[\"'“”‘’](.+?)[\"'“”‘’]", question)
    if m:
        args["text"] = m.group(1)

    if re.search(r"\bhaftalık", q):
        args["interval"] = "week"
    elif re.search(r"\baylık", q):
        args["interval"] = "month"
    elif re.search(r"\bgünlük", q):
        args["interval"] = "day"
    if re.search(r"duruş|downtime", q):
        args["sort_by"] = "downtime"
    return args


def _has(q: str, *words: str) -> bool:
    return any(w in q for w in words)


def route_by_rules(question: str) -> Route | None:
    q = tr_lower(question)
    args = extract_entities(question)
    asset = "asset_id" in args

    if _has(q, "nedir", "ne demek", "nasıl hesaplan") and not asset and not _has(q, "bizim", "durum"):
        return Route(NONE_TOOL, args)
    if _has(q, "risk", "öngör", "tahmin", "arızalanabilir", "arızalanır", "olasılı",
            "ihtimal", "bozulabilir", "bozulur mu", "predict"):
        if _has(q, "trend", "iş emri sayı", "kaç iş emri"):
            return Route("workorder_trend", args)
        return Route("asset_reliability" if asset else "failure_risk_ranking", args)
    if _has(q, "mtbf", "mttr", "güvenilirlik", "reliability") and asset:
        return Route("asset_reliability", args)
    if _has(q, "açık iş", "bekleyen", "backlog", "birikmiş", "kapanmamış", "açık olan"):
        return Route("open_backlog", args)
    if _has(q, "benzer", "içeren", "geçen iş emir", "ara:", "bul:") or "text" in args:
        if "text" not in args:
            args["text"] = re.split(r"benzer|içeren|ara:|bul:", q, maxsplit=1)[-1].strip(" ?.")
        return Route("search_workorders", args)
    if _has(q, "trend", "eğilim", "artıyor", "azalıyor", "artış", "aylık", "haftalık",
            "zaman içinde", "anomali"):
        return Route("workorder_trend", args)
    if _has(q, "arıza kod", "arıza tür", "arıza tip", "arıza neden", "arıza modu",
            "pareto", "kök neden", "hangi arızalar"):
        return Route("failure_modes", args)
    if _has(q, "en çok arıza", "en fazla arıza", "en sorunlu", "en çok duruş",
            "en fazla duruş", "en kötü", "sık arıza", "hangi ekipman", "hangi makine"):
        return Route("top_failing_assets", args)
    if _has(q, "mtbf", "mttr"):
        return Route("kpi_summary", args)
    if _has(q, "özet", "kpi", "genel durum", "performans", "maliyet", "duruş süre",
            "pm uyum", "kaç iş emri", "durum nedir", "rapor"):
        return Route("kpi_summary", args)
    return None


# --------------------------------------------------------------------------- #
# LLM yönlendirici
# --------------------------------------------------------------------------- #
ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "tool": {"type": "string", "enum": [*TOOLS.keys(), NONE_TOOL]},
        "args": {
            "type": "object",
            "properties": {
                "asset_id": {"type": "string"},
                "days": {"type": "integer"},
                "horizon_days": {"type": "integer"},
                "top_n": {"type": "integer"},
                "text": {"type": "string"},
                "interval": {"type": "string", "enum": ["day", "week", "month"]},
            },
        },
    },
    "required": ["tool", "args"],
}


def router_system_prompt() -> str:
    # Statik prompt: her istekte AYNI olduğu için llama.cpp/Ollama KV-cache'te
    # bu önek yeniden kullanılır, prefill neredeyse bedava olur.
    lines = [f"- {t.name}: {t.description}. Parametreler: {t.params}" for t in TOOLS.values()]
    lines.append(f"- {NONE_TOOL}: Veri gerektirmeyen genel bakım/terim sorusu")
    return (
        "Sen bir CMMS (bakım yönetim sistemi) sorgu yönlendiricisisin. Kullanıcının "
        "sorusunu cevaplamak için EN UYGUN tek aracı seç ve argümanlarını çıkar. "
        "Sadece JSON döndür.\n\nAraçlar:\n" + "\n".join(lines) +
        "\n\nKurallar: days=geçmiş analiz penceresi (gün), horizon_days=gelecek tahmin "
        "penceresi (gün). Bilinmeyen argümanı hiç yazma.\n\nÖrnekler:\n"
        'S: "Pompa P-12 önümüzdeki 2 haftada arızalanır mı?"\n'
        '{"tool":"asset_reliability","args":{"asset_id":"P-12","horizon_days":14}}\n'
        'S: "Geçen çeyrekte en çok duruşa sebep olan makineler"\n'
        '{"tool":"top_failing_assets","args":{"days":90}}\n'
        'S: "rulman sesi şikayeti olan iş emirleri"\n'
        '{"tool":"search_workorders","args":{"text":"rulman ses"}}'
    )


def route_by_llm(llm, question: str) -> Route | None:
    msgs = [{"role": "system", "content": router_system_prompt()},
            {"role": "user", "content": question}]
    try:
        out = llm.chat_json(msgs, ROUTE_SCHEMA)
    except Exception:  # noqa: BLE001 - yönlendirici hatası agent'ı düşürmemeli
        return None
    if not out or out.get("tool") not in (*TOOLS.keys(), NONE_TOOL):
        return None
    args = out.get("args") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
    # Regex ile bulunan varlıklar LLM'inkini tamamlar (sayılarda regex daha güvenilir)
    merged = {k: v for k, v in args.items() if v not in (None, "", 0)}
    merged.update(extract_entities(question))
    return Route(out["tool"], merged, source="llm")


def route(question: str, llm=None, mode: str = "hybrid") -> Route:
    if mode in ("rules", "hybrid"):
        r = route_by_rules(question)
        if r:
            return r
    if mode in ("llm", "hybrid") and llm is not None:
        r = route_by_llm(llm, question)
        if r:
            return r
    # Son çare: genel KPI özeti her zaman işe yarar bir bağlam verir.
    return Route("kpi_summary", extract_entities(question), source="fallback")
