import math
from datetime import datetime, timedelta, timezone

from cmms_agent import analytics as A

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def days_ago(*ds):
    return [NOW - timedelta(days=d) for d in ds]


def test_mtbf_from_gaps():
    ts = days_ago(30, 20, 10)
    assert A.mtbf_days(ts, 365) == 10


def test_mtbf_single_failure_uses_window():
    assert A.mtbf_days(days_ago(5), 100) == 100


def test_exponential_prob():
    # h = MTBF iken P = 1 - 1/e ≈ %63.2
    assert math.isclose(A.exponential_failure_prob(10, 10), 1 - math.exp(-1))


def test_weibull_regular_gaps_gives_high_beta():
    fit = A.fit_weibull([10, 10.5, 9.5, 10.2, 9.8])
    assert fit and fit.beta > 5          # çok düzenli = güçlü aşınma karakteri
    assert 9 < fit.eta < 11


def test_weibull_needs_three_gaps():
    assert A.fit_weibull([10, 12]) is None


def test_weibull_conditional_increases_with_age_when_beta_gt_1():
    fit = A.WeibullFit(beta=3, eta=30)
    assert A.weibull_conditional_prob(fit, 25, 7) > A.weibull_conditional_prob(fit, 5, 7)


def test_laplace_detects_deterioration():
    start = NOW - timedelta(days=360)
    # Arızalar dönemin sonuna yığılmış
    ts = [start + timedelta(days=d) for d in (100, 250, 290, 310, 325, 335, 342, 348, 353, 357)]
    u = A.laplace_trend(ts, start, NOW)
    assert u > 1.96 and A.trend_label(u) == "kötüleşiyor"


def test_laplace_uniform_is_stable():
    start = NOW - timedelta(days=360)
    ts = [start + timedelta(days=d) for d in range(18, 360, 36)]
    assert A.trend_label(A.laplace_trend(ts, start, NOW)) == "stabil"


def test_linear_trend_and_forecast():
    a, b = A.linear_trend([1, 2, 3, 4])
    assert math.isclose(b, 1) and math.isclose(a, 1)
    assert A.forecast([1, 2, 3, 4], 2) == [5, 6]


def test_zscore():
    assert A.zscore_anomalies([2, 2, 3, 2, 3, 2, 15]) == [6]


def test_profile_end_to_end():
    start = NOW - timedelta(days=180)
    p = A.reliability_profile("X", days_ago(150, 120, 90, 60, 30), [2, 4], start, NOW, 30)
    d = p.to_dict()
    assert d["failures"] == 5 and d["mtbf_days"] == 30 and d["mttr_hours"] == 3
    assert d["risk_level"] in ("orta", "yüksek")


def test_parse_ts():
    assert A.parse_ts("2026-01-01T00:00:00Z") == NOW
    assert A.parse_ts(1767225600000) == NOW
    assert A.parse_ts(None) is None
