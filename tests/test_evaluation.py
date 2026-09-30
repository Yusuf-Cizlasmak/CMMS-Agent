from cmms_agent.evaluation import Case, number_grounding, route_matches

FACTS = {"ranking": [{"asset_id": "KMP-003", "risk_pct": 89.48, "failures": 18,
                      "last": "2026-09-26"}], "pm_compliance_ratio": 0.83}


def test_grounded_numbers_with_rounding():
    ans = "KMP-003 riski %89.5 (18 arıza). PM uyumu %83. Son arıza 2026-09-26."
    g = number_grounding(ans, FACTS)
    assert g["ungrounded"] == [] and g["rate"] == 1.0


def test_detects_hallucinated_number():
    g = number_grounding("KMP-003'te 42 arıza var.", FACTS)
    assert g["ungrounded"] == [42.0]


def test_list_markers_and_question_numbers_ignored():
    ans = "1. Son 30 günde 18 arıza\n2. Öneri"
    assert number_grounding(ans, FACTS, "Son 30 gün?")["rate"] == 1.0


def test_route_matches():
    c = Case("q", "search_workorders", {"text": "yağ kaçağı"})
    assert route_matches("search_workorders", {"text": "yağ kaçağına"}, c)[0]
    ok, errs = route_matches("kpi_summary", {}, c)
    assert not ok and len(errs) == 2
