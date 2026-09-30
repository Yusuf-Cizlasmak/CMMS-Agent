import pytest

from cmms_agent.router import extract_entities, route, route_by_rules


@pytest.mark.parametrize("q,tool", [
    ("Önümüzdeki ay hangi ekipmanlar arızalanabilir?", "failure_risk_ranking"),
    ("KMP-003 için 2 hafta içinde arıza riski nedir?", "asset_reliability"),
    ("PMP-012 MTBF ve MTTR değerleri", "asset_reliability"),
    ("Son 90 günde en çok arıza yapan 5 makine", "top_failing_assets"),
    ("Açık iş emirleri ne durumda?", "open_backlog"),
    ("Arıza sayısı aylık olarak artıyor mu?", "workorder_trend"),
    ("En sık görülen arıza kodları", "failure_modes"),
    ("'rulman ses' geçen iş emirleri", "search_workorders"),
    ("Bu yılın bakım KPI özeti", "kpi_summary"),
    ("MTBF nedir?", "none"),
    ("Sensörlerde anormal bir durum var mı?", "sensor_health"),
    ("KMP-003 titreşim değerleri nasıl?", "sensor_health"),
    ("Gelecek hafta arıza çıkarması muhtemel ekipmanlar", "failure_risk_ranking"),
])
def test_rules(q, tool):
    r = route_by_rules(q)
    assert r is not None and r.tool == tool


def test_entities():
    e = extract_entities("Son 3 ay için KMP-003 önümüzdeki 2 hafta içinde ilk 5")
    assert e["days"] == 90
    assert e["horizon_days"] == 14
    assert e["asset_id"] == "KMP-003"
    assert e["top_n"] == 5


def test_son_n_gun_icinde_is_not_horizon():
    e = extract_entities("son 30 gün içinde en çok arıza")
    assert e["days"] == 30 and "horizon_days" not in e


def test_turkish_capital_i():
    assert route_by_rules("İLK 3 RİSKLİ EKİPMAN").tool == "failure_risk_ranking"


class FakeLLM:
    def __init__(self, out):
        self.out = out

    def chat_json(self, msgs, schema):
        return self.out


def test_llm_fallback_merges_regex_entities():
    llm = FakeLLM({"tool": "top_failing_assets", "args": {"days": 7}})
    r = route("Geçen çeyrekte son 90 gün hatları kıyasla", llm=llm, mode="hybrid")
    assert r.source == "llm" and r.tool == "top_failing_assets"
    assert r.args["days"] == 90       # regex, LLM'in yanlış sayısını düzeltir


def test_llm_invalid_tool_falls_back():
    r = route("xyz", llm=FakeLLM({"tool": "rm_rf", "args": {}}), mode="hybrid")
    assert r.tool == "kpi_summary" and r.source == "fallback"


def test_search_text_extraction():
    assert route_by_rules("yağ kaçağına benzer kayıtları bul").args["text"] == "yağ kaçağına"
    assert route_by_rules("ara: rulman sesi").args["text"] == "rulman sesi"


def test_eval_set_rules_accuracy():
    """eval/questions.jsonl kural router ile %100 geçmeli (regresyon koruması)."""
    from pathlib import Path

    from cmms_agent.evaluation import load_cases, route_matches
    cases = load_cases(Path(__file__).parent.parent / "eval" / "questions.jsonl")
    failures = []
    for c in cases:
        r = route(c.q, mode="rules")
        ok, errs = route_matches(r.tool, r.args, c)
        if not ok:
            failures.append((c.q, errs))
    assert not failures, failures
