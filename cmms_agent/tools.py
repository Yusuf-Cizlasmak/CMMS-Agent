"""Agent'ın kullanabileceği araçlar (tools).

Her araç:
  1. Parametreleri doğrular/varsayılanlar,
  2. Elastic'ten veriyi (tercihen aggregation ile) çeker,
  3. analytics.py ile hesaplar,
  4. LLM'e gidecek KÜÇÜK bir "facts" sözlüğü döndürür.

Küçük model + küçük bağlam = hızlı cevap. Bir aracın çıktısı ~1-2 KB JSON'u
geçmemeli; bu, Jetson'da prompt işleme (prefill) süresini doğrudan kısaltır.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import analytics as A
from .es_client import CMMSRepository


@dataclass
class Tool:
    name: str
    description: str          # LLM yönlendiricisinin göreceği kısa açıklama
    params: str               # hangi argümanları kullandığı (dokümantasyon)
    fn: Callable[["ToolContext", dict], dict]


@dataclass
class ToolContext:
    repo: CMMSRepository
    default_days: int
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)


def _r(x: Any, nd: int = 2) -> Any:
    return round(x, nd) if isinstance(x, float) else x


def _days(ctx: ToolContext, args: dict) -> int:
    d = args.get("days") or ctx.default_days
    return max(1, min(int(d), 3650))


def _top_n(args: dict, default: int = 5) -> int:
    return max(1, min(int(args.get("top_n") or default), 20))


def _bucket_map(agg: dict) -> dict[str, int]:
    return {str(b["key"]): b["doc_count"] for b in agg.get("buckets", [])}


def resolve_asset(ctx: ToolContext, asset: str | None) -> str | None:
    """Kullanıcı 'KMP-003' da diyebilir 'kompresör 3' de. İkisini de ID'ye çevir."""
    if not asset:
        return None
    f = ctx.repo.f
    hits = ctx.repo.search(
        {"bool": {"should": [
            {"term": {f.asset: {"value": asset, "boost": 10}}},
            {"term": {f.asset: {"value": asset.upper(), "boost": 10}}},
            # asset_name keyword alan: büyük/küçük harf duyarsız "içerir" araması
            {"wildcard": {f.asset_name: {"value": f"*{asset}*", "case_insensitive": True}}},
        ], "minimum_should_match": 1}},
        size=1, source=[f.asset],
    )
    return hits[0].get(f.asset) if hits else asset


# --------------------------------------------------------------------------- #
# Araçlar
# --------------------------------------------------------------------------- #
def kpi_summary(ctx: ToolContext, args: dict) -> dict:
    repo, f = ctx.repo, ctx.repo.f
    days = _days(ctx, args)
    asset = resolve_asset(ctx, args.get("asset_id"))
    q = repo.bool_query(repo.time_filter(days), repo.asset_filter(asset) if asset else None)
    aggs = {
        "by_type": {"terms": {"field": f.wo_type, "size": 10}},
        "by_status": {"terms": {"field": f.status, "size": 10}},
        "downtime": {"sum": {"field": f.downtime_hours}},
        "cost": {"sum": {"field": f.cost}},
        "labor": {"sum": {"field": f.labor_hours}},
        "corrective": {
            "filter": repo.type_filter(f.corrective_values),
            "aggs": {"mttr": {"avg": {"field": f.downtime_hours}}},
        },
        "pm": {
            "filter": repo.type_filter(f.preventive_values),
            "aggs": {"done": {"filter": {"terms": {f.status: list(f.completed_status_values)}}}},
        },
    }
    r = repo.aggregate(q, aggs)
    corrective = r["corrective"]["doc_count"]
    pm_total, pm_done = r["pm"]["doc_count"], r["pm"]["done"]["doc_count"]
    total = r["_total"]
    return {
        "period_days": days,
        "asset_id": asset,
        "work_orders_total": total,
        "by_type": _bucket_map(r["by_type"]),
        "by_status": _bucket_map(r["by_status"]),
        "corrective_count": corrective,
        "corrective_ratio_pct": _r(100 * corrective / total) if total else None,
        "pm_compliance_pct": _r(100 * pm_done / pm_total) if pm_total else None,
        "mttr_hours": _r(r["corrective"]["mttr"]["value"]),
        "downtime_hours_total": _r(r["downtime"]["value"]),
        "labor_hours_total": _r(r["labor"]["value"]),
        "cost_total": _r(r["cost"]["value"]),
    }


def top_failing_assets(ctx: ToolContext, args: dict) -> dict:
    repo, f = ctx.repo, ctx.repo.f
    days, n = _days(ctx, args), _top_n(args)
    q = repo.bool_query(repo.time_filter(days), repo.type_filter(f.corrective_values))
    order = "downtime" if args.get("sort_by") == "downtime" else "_count"
    aggs = {"assets": {
        "terms": {"field": f.asset, "size": n, "order": {order: "desc"}},
        "aggs": {
            "name": {"terms": {"field": f.asset_name, "size": 1}},
            "downtime": {"sum": {"field": f.downtime_hours}},
            "cost": {"sum": {"field": f.cost}},
            "last": {"max": {"field": f.created_at}},
        },
    }}
    r = repo.aggregate(q, aggs)
    rows = []
    for b in r["assets"]["buckets"]:
        names = b["name"]["buckets"]
        rows.append({
            "asset_id": b["key"],
            "asset_name": names[0]["key"] if names else None,
            "failures": b["doc_count"],
            "downtime_hours": _r(b["downtime"]["value"]),
            "cost": _r(b["cost"]["value"]),
            "last_failure": b["last"].get("value_as_string"),
        })
    return {"period_days": days, "total_corrective": r["_total"], "assets": rows}


def _repair_hours(doc: dict, f) -> float | None:
    if doc.get(f.downtime_hours) is not None:
        return float(doc[f.downtime_hours])
    s, e = A.parse_ts(doc.get(f.started_at)), A.parse_ts(doc.get(f.completed_at))
    return (e - s).total_seconds() / 3600 if s and e else None


def _collect_failures(ctx: ToolContext, days: int, asset: str | None
                      ) -> dict[str, tuple[list[datetime], list[float]]]:
    repo, f = ctx.repo, ctx.repo.f
    q = repo.bool_query(repo.time_filter(days), repo.type_filter(f.corrective_values),
                        repo.asset_filter(asset) if asset else None)
    src = [f.asset, f.created_at, f.downtime_hours, f.started_at, f.completed_at]
    by_asset: dict[str, tuple[list, list]] = defaultdict(lambda: ([], []))
    for doc in repo.iter_docs(q, src):
        ts = A.parse_ts(doc.get(f.created_at))
        if ts is None:
            continue
        times, reps = by_asset[str(doc.get(f.asset))]
        times.append(ts)
        rh = _repair_hours(doc, f)
        if rh is not None:
            reps.append(rh)
    return by_asset


def asset_reliability(ctx: ToolContext, args: dict) -> dict:
    asset = resolve_asset(ctx, args.get("asset_id"))
    if not asset:
        return {"error": "asset_id gerekli. Örn: 'KMP-003 için arıza riski nedir?'"}
    days = _days(ctx, args)
    horizon = int(args.get("horizon_days") or 30)
    now = ctx.now()
    data = _collect_failures(ctx, days, asset)
    times, reps = data.get(asset, ([], []))
    if not times:
        return {"asset_id": asset, "period_days": days,
                "note": "Bu dönemde düzeltici (arıza) iş emri yok."}
    prof = A.reliability_profile(asset, times, reps, now - timedelta(days=days), now, horizon)
    out = prof.to_dict()
    out["last_failures"] = [t.date().isoformat() for t in sorted(times)[-5:]]
    return out


def failure_risk_ranking(ctx: ToolContext, args: dict) -> dict:
    days, n = _days(ctx, args), _top_n(args)
    horizon = int(args.get("horizon_days") or 30)
    now = ctx.now()
    start = now - timedelta(days=days)
    profiles = []
    for asset, (times, reps) in _collect_failures(ctx, days, None).items():
        if len(times) < 2:          # tek arızadan risk tahmini yapmıyoruz
            continue
        profiles.append(A.reliability_profile(asset, times, reps, start, now, horizon))
    profiles.sort(key=lambda p: (p.risk_pct or 0, p.laplace_u or 0), reverse=True)
    keep = ("asset_id", "failures", "mtbf_days", "days_since_last_failure",
            "risk_pct", "risk_level", "trend", "weibull_beta")
    return {
        "period_days": days,
        "horizon_days": horizon,
        "assets_evaluated": len(profiles),
        "ranking": [{k: v for k, v in p.to_dict().items() if k in keep}
                    for p in profiles[:n]],
    }


def workorder_trend(ctx: ToolContext, args: dict) -> dict:
    repo, f = ctx.repo, ctx.repo.f
    days = _days(ctx, args)
    interval = args.get("interval") or ("week" if days <= 120 else "month")
    if interval not in ("day", "week", "month"):
        interval = "month"
    asset = resolve_asset(ctx, args.get("asset_id"))
    q = repo.bool_query(repo.time_filter(days), repo.asset_filter(asset) if asset else None)
    aggs = {"hist": {
        "date_histogram": {
            "field": f.created_at, "calendar_interval": interval,
            "min_doc_count": 0, "format": "yyyy-MM-dd",
            "extended_bounds": {"min": f"now-{days}d/d", "max": "now/d"},
        },
        "aggs": {"corrective": {"filter": repo.type_filter(f.corrective_values)}},
    }}
    r = repo.aggregate(q, aggs)
    buckets = r["hist"]["buckets"]
    labels = [b["key_as_string"] for b in buckets]
    total = [float(b["doc_count"]) for b in buckets]
    corr = [float(b["corrective"]["doc_count"]) for b in buckets]
    # Son kova henüz bitmedi (yarım hafta/ay) -> trend hesabından çıkar,
    # yoksa her zaman sahte bir "düşüş" görürüz.
    fit_corr = corr[:-1] if len(corr) > 2 else corr
    _, slope = A.linear_trend(fit_corr)
    anomalies = A.zscore_anomalies(fit_corr)
    return {
        "period_days": days, "interval": interval, "asset_id": asset,
        "series": [{"period": l, "total": int(t), "corrective": int(c)}
                   for l, t, c in list(zip(labels, total, corr))[-12:]],
        "corrective_slope_per_period": _r(slope, 3),
        "direction": "artıyor" if slope > 0.1 else "azalıyor" if slope < -0.1 else "yatay",
        "forecast_next_3_corrective": [_r(v, 1) for v in A.forecast(fit_corr, 3)],
        "anomaly_periods": [labels[i] for i in anomalies],
        "current_period_incomplete": True,
    }


def failure_modes(ctx: ToolContext, args: dict) -> dict:
    repo, f = ctx.repo, ctx.repo.f
    days, n = _days(ctx, args), _top_n(args, 8)
    asset = resolve_asset(ctx, args.get("asset_id"))
    q = repo.bool_query(repo.time_filter(days), repo.type_filter(f.corrective_values),
                        repo.asset_filter(asset) if asset else None)
    aggs = {"codes": {"terms": {"field": f.failure_code, "size": n},
                      "aggs": {"downtime": {"sum": {"field": f.downtime_hours}},
                               "assets": {"cardinality": {"field": f.asset}}}}}
    r = repo.aggregate(q, aggs)
    return {
        "period_days": days, "asset_id": asset, "total_corrective": r["_total"],
        "failure_modes": [{"code": b["key"], "count": b["doc_count"],
                           "downtime_hours": _r(b["downtime"]["value"]),
                           "affected_assets": b["assets"]["value"]}
                          for b in r["codes"]["buckets"]],
    }


def search_workorders(ctx: ToolContext, args: dict) -> dict:
    repo, f = ctx.repo, ctx.repo.f
    text = (args.get("text") or "").strip()
    if not text:
        return {"error": "Aranacak metin gerekli."}
    days, n = _days(ctx, args), _top_n(args)
    asset = resolve_asset(ctx, args.get("asset_id"))
    q = repo.bool_query(
        repo.time_filter(days), repo.asset_filter(asset) if asset else None,
        must=[{"multi_match": {"query": text, "fields": [f.description, f.failure_code],
                               "fuzziness": "AUTO"}}],
    )
    src = [f.wo_id, f.asset, f.created_at, f.wo_type, f.failure_code, f.description,
           f.downtime_hours]
    hits = repo.search(q, size=n, source=src)
    for h in hits:
        d = h.get(f.description) or ""
        h[f.description] = d[:160] + ("…" if len(d) > 160 else "")
    return {"query": text, "period_days": days, "matches": hits}


def open_backlog(ctx: ToolContext, args: dict) -> dict:
    repo, f = ctx.repo, ctx.repo.f
    asset = resolve_asset(ctx, args.get("asset_id"))
    q = repo.bool_query({"terms": {f.status: list(f.open_status_values)}},
                        repo.asset_filter(asset) if asset else None)
    r = repo.aggregate(q, {
        "by_priority": {"terms": {"field": f.priority, "size": 10}},
        "by_type": {"terms": {"field": f.wo_type, "size": 10}},
        "oldest": {"min": {"field": f.created_at}},
    })
    oldest = repo.search(q, size=5, sort=[{f.created_at: "asc"}],
                         source=[f.wo_id, f.asset, f.priority, f.created_at])
    now = ctx.now()
    for o in oldest:
        ts = A.parse_ts(o.get(f.created_at))
        o["age_days"] = _r((now - ts).total_seconds() / 86400, 1) if ts else None
    return {
        "asset_id": asset, "open_total": r["_total"],
        "by_priority": _bucket_map(r["by_priority"]),
        "by_type": _bucket_map(r["by_type"]),
        "oldest_open": oldest,
    }


TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("kpi_summary", "Genel bakım KPI özeti: iş emri sayıları, MTTR, duruş, maliyet, PM uyumu",
         "days, asset_id?", kpi_summary),
    Tool("top_failing_assets", "En çok arızalanan / en çok duruş yaşatan ekipmanlar",
         "days, top_n, sort_by?(count|downtime)", top_failing_assets),
    Tool("asset_reliability", "Tek bir ekipmanın MTBF, MTTR, trend ve gelecek arıza olasılığı",
         "asset_id (zorunlu), days, horizon_days", asset_reliability),
    Tool("failure_risk_ranking", "Önümüzdeki dönemde arızalanma riski en yüksek ekipmanlar (öngörü)",
         "days, horizon_days, top_n", failure_risk_ranking),
    Tool("workorder_trend", "İş emri/arıza sayısının zaman içindeki trendi, tahmin ve anomali",
         "days, interval?(day|week|month), asset_id?", workorder_trend),
    Tool("failure_modes", "En sık arıza kodları / arıza türleri (Pareto)",
         "days, asset_id?, top_n", failure_modes),
    Tool("search_workorders", "İş emri açıklamalarında metin arama (benzer arızaları bul)",
         "text (zorunlu), days, asset_id?", search_workorders),
    Tool("open_backlog", "Açık/bekleyen iş emirleri, önceliğe göre dağılım, en eski işler",
         "asset_id?", open_backlog),
]}
