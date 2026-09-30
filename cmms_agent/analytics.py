"""Bakım güvenilirliği analitiği (saf Python, bağımlılık yok).

Neden LLM değil de klasik istatistik?
- Küçük LLM'ler (1-8B) aritmetikte güvenilmezdir ve yavaştır.
- Burada hesaplanan her sayı deterministik, test edilebilir ve açıklanabilir.
- LLM sadece bu sayıları okuyup yorumlar ("anlatıcı" rolü).

İçerik:
- MTBF / MTTR                  : klasik bakım KPI'ları
- Üstel (exponential) risk     : "sabit arıza oranı" varsayımıyla P(arıza)
- Weibull risk                 : yaşlanma/aşınma etkisini de hesaba katan P(arıza)
- Laplace trend testi          : arızalar sıklaşıyor mu? (istatistiksel anlamlılık)
- Zaman serisi trend + tahmin  : doğrusal regresyon
- Z-skor anomali tespiti
"""
from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

HOURS_PER_DAY = 24.0


def parse_ts(value) -> datetime | None:
    """ES'ten gelen tarih: ISO string veya epoch millis olabilir."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    s = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# MTBF / MTTR
# --------------------------------------------------------------------------- #
def inter_failure_days(failure_times: list[datetime]) -> list[float]:
    ts = sorted(failure_times)
    return [(b - a).total_seconds() / 86400 for a, b in zip(ts, ts[1:])]


def mtbf_days(failure_times: list[datetime], window_days: float) -> float | None:
    """Arızalar arası ortalama süre (gün).

    >=2 arıza varsa ardışık aralıkların ortalaması; 1 arıza varsa
    gözlem penceresi / arıza sayısı (kaba tahmin).
    """
    n = len(failure_times)
    if n == 0:
        return None
    gaps = inter_failure_days(failure_times)
    if gaps:
        return statistics.fmean(gaps)
    return window_days / n


def mttr_hours(repair_hours: list[float]) -> float | None:
    vals = [h for h in repair_hours if h is not None and h >= 0]
    return statistics.fmean(vals) if vals else None


def availability(mtbf_d: float | None, mttr_h: float | None) -> float | None:
    """Kararlı durum kullanılabilirliği A = MTBF / (MTBF + MTTR)."""
    if not mtbf_d or mttr_h is None:
        return None
    mtbf_h = mtbf_d * HOURS_PER_DAY
    return mtbf_h / (mtbf_h + mttr_h)


# --------------------------------------------------------------------------- #
# Arıza olasılığı modelleri
# --------------------------------------------------------------------------- #
def exponential_failure_prob(mtbf_d: float, horizon_days: float) -> float:
    """Sabit arıza oranı (λ = 1/MTBF): P(T <= h) = 1 - e^(-h/MTBF)."""
    if mtbf_d <= 0:
        return 1.0
    return 1.0 - math.exp(-horizon_days / mtbf_d)


@dataclass
class WeibullFit:
    beta: float    # şekil: <1 erken ömür, ~1 rastgele, >1 aşınma/yaşlanma
    eta: float     # ölçek (gün): arızaların ~%63'ünün gerçekleştiği süre


def fit_weibull(gaps_days: list[float]) -> WeibullFit | None:
    """Momentler yöntemiyle hızlı Weibull uydurma (Justus yaklaşımı).

    β ≈ (σ/μ)^-1.086 ,  η = μ / Γ(1 + 1/β)
    En az 3 aralık (4 arıza) ister; daha azıyla β anlamlı değildir.
    Not: Üretimde MLE (ör. `reliability` kütüphanesi) daha doğrudur; bu
    yaklaşım Jetson'da milisaniyede çalışır ve karar desteği için yeterlidir.
    """
    gaps = [g for g in gaps_days if g > 0]
    if len(gaps) < 3:
        return None
    mu = statistics.fmean(gaps)
    sd = statistics.stdev(gaps)
    if mu <= 0 or sd <= 0:
        return None
    beta = (sd / mu) ** -1.086
    beta = max(0.3, min(beta, 10.0))
    eta = mu / math.gamma(1 + 1 / beta)
    return WeibullFit(beta=beta, eta=eta)


def weibull_conditional_prob(fit: WeibullFit, age_days: float, horizon_days: float) -> float:
    """Son arızadan bu yana `age_days` geçmiş ve hâlâ çalışıyor ise,
    önümüzdeki `horizon_days` içinde arızalanma olasılığı:

        P = 1 - exp( (t/η)^β - ((t+h)/η)^β )
    """
    t, h = max(age_days, 0.0), horizon_days
    return 1.0 - math.exp((t / fit.eta) ** fit.beta - ((t + h) / fit.eta) ** fit.beta)


# --------------------------------------------------------------------------- #
# Trend testleri
# --------------------------------------------------------------------------- #
def laplace_trend(failure_times: list[datetime], start: datetime, end: datetime) -> float | None:
    """Laplace testi: arızalar zamanla sıklaşıyor mu?

    U > +1.96  -> %95 güvenle KÖTÜLEŞME (arızalar sıklaşıyor)
    U < -1.96  -> %95 güvenle İYİLEŞME
    aradaki    -> anlamlı trend yok
    """
    n = len(failure_times)
    T = (end - start).total_seconds() / 86400
    if n < 3 or T <= 0:
        return None
    ts = [(t - start).total_seconds() / 86400 for t in failure_times]
    return (statistics.fmean(ts) - T / 2) / (T * math.sqrt(1 / (12 * n)))


def trend_label(u: float | None) -> str:
    if u is None:
        return "yetersiz_veri"
    if u > 1.96:
        return "kötüleşiyor"          # %95 güven
    if u > 1.645:
        return "kötüleşme_eğilimi"    # %90 güven: izlemeye al
    if u < -1.96:
        return "iyileşiyor"
    return "stabil"


def linear_trend(values: list[float]) -> tuple[float, float]:
    """En küçük kareler: y = a + b*x. (a, b) döner. b = periyot başına değişim."""
    n = len(values)
    if n < 2:
        return (values[0] if values else 0.0, 0.0)
    xs = range(n)
    mx, my = (n - 1) / 2, statistics.fmean(values)
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, values))
    b = sxy / sxx if sxx else 0.0
    return my - b * mx, b


def forecast(values: list[float], periods: int) -> list[float]:
    a, b = linear_trend(values)
    n = len(values)
    return [max(0.0, a + b * (n + i)) for i in range(periods)]


def zscore_anomalies(values: list[float], threshold: float = 2.0) -> list[int]:
    """Ortalamadan `threshold` standart sapmadan fazla uzaklaşan indeksler."""
    if len(values) < 4:
        return []
    mu = statistics.fmean(values)
    sd = statistics.pstdev(values)
    if sd == 0:
        return []
    return [i for i, v in enumerate(values) if abs(v - mu) / sd >= threshold]


# --------------------------------------------------------------------------- #
# Tek bir varlık için tüm güvenilirlik profili
# --------------------------------------------------------------------------- #
@dataclass
class ReliabilityProfile:
    asset_id: str
    failures: int
    window_days: float
    mtbf_days: float | None
    mttr_hours: float | None
    availability_pct: float | None
    days_since_last_failure: float | None
    horizon_days: int
    risk_exponential_pct: float | None
    risk_weibull_pct: float | None
    weibull_beta: float | None
    weibull_eta_days: float | None
    laplace_u: float | None
    trend: str
    risk_pct: float | None          # kullanılacak "nihai" risk
    risk_level: str

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in d.items()}


def risk_level(p: float | None) -> str:
    if p is None:
        return "bilinmiyor"
    if p >= 0.6:
        return "yüksek"
    if p >= 0.3:
        return "orta"
    return "düşük"


def reliability_profile(asset_id: str, failure_times: list[datetime],
                        repair_hours: list[float], window_start: datetime,
                        now: datetime, horizon_days: int = 30) -> ReliabilityProfile:
    failure_times = sorted(failure_times)
    window_days = (now - window_start).total_seconds() / 86400
    mtbf = mtbf_days(failure_times, window_days)
    mttr = mttr_hours(repair_hours)
    avail = availability(mtbf, mttr)
    age = ((now - failure_times[-1]).total_seconds() / 86400) if failure_times else None

    p_exp = exponential_failure_prob(mtbf, horizon_days) if mtbf else None
    wb = fit_weibull(inter_failure_days(failure_times))
    p_wb = weibull_conditional_prob(wb, age or 0.0, horizon_days) if wb else None
    u = laplace_trend(failure_times, window_start, now)

    # Weibull varsa onu tercih et (yaşlanmayı da hesaba katar), yoksa üstel.
    p = p_wb if p_wb is not None else p_exp
    # Birkaç düzine olaydan uydurulan bir model %99.99 kesinlik iddia edemez.
    # Nihai riski [%1, %99] aralığına sıkıştırıyoruz (ham değerler ayrıca raporlanır).
    if p is not None:
        p = min(max(p, 0.01), 0.99)
    return ReliabilityProfile(
        asset_id=asset_id,
        failures=len(failure_times),
        window_days=window_days,
        mtbf_days=mtbf,
        mttr_hours=mttr,
        availability_pct=avail * 100 if avail is not None else None,
        days_since_last_failure=age,
        horizon_days=horizon_days,
        risk_exponential_pct=p_exp * 100 if p_exp is not None else None,
        risk_weibull_pct=p_wb * 100 if p_wb is not None else None,
        weibull_beta=wb.beta if wb else None,
        weibull_eta_days=wb.eta if wb else None,
        laplace_u=u,
        trend=trend_label(u),
        risk_pct=p * 100 if p is not None else None,
        risk_level=risk_level(p),
    )


# --------------------------------------------------------------------------- #
# Sensör / durum izleme
# --------------------------------------------------------------------------- #
def sensor_deviation(base_mean: float | None, base_std: float | None,
                     recent_mean: float | None, daily_means: list[float],
                     trend_days: int = 7) -> dict:
    """Bir metriğin "şu anki" durumunu kendi geçmişiyle karşılaştır.

    - z-skoru: son dönem ortalaması, referans (baseline) dönemin dağılımından
      kaç standart sapma uzakta?  |z|>=2 uyarı, |z|>=3 kritik.
    - eğim: son `trend_days` günlük ortalamaların doğrusal eğimi, referans
      ortalamanın yüzdesi olarak (%/gün). Yavaş ama istikrarlı bozulmayı
      (ör. rulman aşınmasıyla artan titreşim) z-skorundan ÖNCE yakalar.

    Neden sabit eşik (ör. "titreşim > 7 mm/s") değil? Her makinenin "normali"
    farklıdır; kendi geçmişine göre sapma, ekipman bazında eşik girmeden çalışır.
    ISO 10816 gibi standart eşikleriniz varsa ikisini birlikte kullanın.
    """
    z = None
    if base_mean is not None and recent_mean is not None and base_std and base_std > 0:
        z = (recent_mean - base_mean) / base_std
    slope_pct = None
    pts = [v for v in daily_means if v is not None][-trend_days:]
    if len(pts) >= 3 and base_mean:
        _, b = linear_trend(pts)
        slope_pct = 100 * b / abs(base_mean)
    az, s = abs(z) if z is not None else 0.0, slope_pct or 0.0
    if az >= 3 or s >= 5:
        status = "kritik"
    elif az >= 2 or s >= 2:
        status = "uyarı"
    elif z is None and slope_pct is None:
        status = "yetersiz_veri"
    else:
        status = "normal"
    return {"z_score": z, "trend_pct_per_day": slope_pct, "status": status}
