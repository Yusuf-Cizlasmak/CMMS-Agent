# 4. Analitik ve öngörü: sayıların arkasındaki matematik

Tüm kod: [`cmms_agent/analytics.py`](../cmms_agent/analytics.py), testler:
[`tests/test_analytics.py`](../tests/test_analytics.py).

## 4.1 Temel KPI'lar

| KPI | Formül | Yorum |
|-----|--------|-------|
| **MTBF** (Mean Time Between Failures) | ardışık arızalar arası sürelerin ortalaması | Büyük = iyi |
| **MTTR** (Mean Time To Repair) | ortalama onarım/duruş süresi | Küçük = iyi |
| **Kullanılabilirlik** | MTBF / (MTBF + MTTR) | %99 = yılda ~88 saat duruş |
| **Düzeltici oranı** | düzeltici İE / toplam İE | Yüksekse bakım "yangın söndürme" modunda |
| **PM uyumu** | tamamlanan PM / planlanan PM | Hedef genelde ≥ %90 |

> Tek arıza varsa MTBF = gözlem penceresi / 1 olarak kaba tahmin edilir. Bu
> yüzden `failure_risk_ranking` en az 2 arızası olan ekipmanları sıralar.

## 4.2 "Bu makine önümüzdeki 30 günde arızalanır mı?"

### Model 1 — Üstel dağılım (sabit arıza oranı)
Arızaların rastgele ve hafızasız geldiğini varsayar:

```
λ = 1 / MTBF
P(30 gün içinde arıza) = 1 − e^(−30/MTBF)
```
MTBF = 30 gün ise P ≈ %63. Basit ve sağlamdır ama *yaşlanmayı görmez*:
son arızadan 1 gün de geçse 50 gün de geçse aynı olasılığı verir.

### Model 2 — Weibull (yaşlanmayı hesaba katar)
Güvenilirlik mühendisliğinin iş atıdır. İki parametresi vardır:

- **β (şekil):**
  - β < 1 → "bebek ölümü": yeni takılan/tamir edilen parça erken arızalanıyor
    (montaj, kalite sorunu). Öneri: montaj prosedürü, parça kalitesi.
  - β ≈ 1 → rastgele arızalar (üstel dağılıma eşdeğer). Zaman bazlı PM az işe yarar.
  - β > 1 → **aşınma/yaşlanma**: son arızadan beri geçen süre arttıkça risk
    artar. **Zaman bazlı önleyici bakımın en çok işe yaradığı durum.**
- **η (ölçek, gün):** arızaların ~%63'ünün gerçekleştiği süre.

Bu projede β ve η, arızalar arası sürelerin ortalama ve standart sapmasından
(momentler yöntemi, Justus yaklaşımı) hesaplanır:
```
β ≈ (σ/μ)^−1.086        η = μ / Γ(1 + 1/β)
```
ve son arızadan beri `t` gün geçmiş, hâlâ çalışan bir ekipman için
**koşullu** olasılık:
```
P(t, t+h] = 1 − exp( (t/η)^β − ((t+h)/η)^β )
```
En az 3 aralık (4 arıza) gerekir; yoksa üstel modele düşülür.

> **Neden MLE değil?** Maksimum olabilirlik tahmini (MLE) daha doğrudur ve
> `reliability` ya da `lifelines` kütüphaneleriyle yapılabilir. Momentler
> yöntemi bağımlılıksızdır, milisaniyede çalışır ve karar desteği için
> genellikle yeterlidir. Doğruluk kritikse MLE'ye geçin (doküman 5).

### Hangi olasılık raporlanıyor?
`risk_pct` = Weibull varsa Weibull, yoksa üstel. Seviyeler:
`≥%60 yüksek`, `≥%30 orta`, altı `düşük` (`analytics.risk_level`).
Bu eşikleri kendi risk iştahınıza göre değiştirin.

## 4.3 "Arızalar sıklaşıyor mu?" — Laplace trend testi

MTBF bir ortalamadır ve trendi gizler: yılın başında seyrek, sonunda sık
arızalanan bir makine ile düzenli arızalanan bir makinenin MTBF'i aynı
olabilir. Laplace testi bunu ayırt eder:

```
U = ( ortalama(tᵢ) − T/2 ) / ( T · √(1/(12n)) )
```
- tᵢ: pencere başından itibaren arıza zamanları, T: pencere uzunluğu, n: arıza sayısı
- Trend yoksa arızalar pencereye düzgün yayılır, ortalamaları ≈ T/2 → U ≈ 0

| U | Etiket | Anlamı |
|---|--------|--------|
| > 1.96 | `kötüleşiyor` | %95 güvenle arızalar sıklaşıyor |
| 1.645 – 1.96 | `kötüleşme_eğilimi` | %90 güven; izlemeye alın |
| < −1.96 | `iyileşiyor` | Yapılan iyileştirme işe yarıyor |
| arası | `stabil` | Anlamlı trend yok |

**Pratik değer:** Weibull β > 1 *ve* Laplace "kötüleşiyor" ise ekipman hem
yaşlanıyor hem de hızla kötüye gidiyor → revizyon/yenileme adayıdır.

## 4.4 Zaman serisi: trend, tahmin, anomali

`workorder_trend` aracı:
1. Elastic `date_histogram` ile haftalık/aylık iş emri sayılarını alır.
2. **Son (bitmemiş) dönemi** trend hesabından çıkarır — yoksa her zaman
   sahte bir "düşüş" görünür. (Klasik bir hata!)
3. En küçük kareler ile eğim (`corrective_slope_per_period`) ve sonraki 3
   dönem tahmini üretir.
4. z-skoru ≥ 2 olan dönemleri anomali olarak işaretler.

Doğrusal trend basittir; mevsimsellik (ör. yaz aylarında chiller arızaları)
varsa Holt-Winters ya da `statsmodels` ile daha iyi modeller kurulabilir.

## 4.5 Sınırlar — kullanıcıya dürüst olun

- Bunlar **istatistiksel tahminlerdir**, kehanet değildir. Narrator prompt'u
  LLM'e "kesinlik iddia etme" der.
- Az veri (< 4 arıza) = geniş belirsizlik. Araçlar bu durumda Weibull'u
  atlar.
- Sadece iş emri geçmişine dayanan tahmin, **durum izleme** (titreşim,
  sıcaklık, akım) verisi eklendiğinde çok daha isabetli olur. Bu, doğal
  bir sonraki adımdır (doküman 5).

## 4.6 Örnek veride kendiniz görün

`scripts/seed_sample_data.py` içine bilinçli "hikâyeler" gömüldü:

| Ekipman | Gömülü davranış | Agent ne bulmalı? |
|---------|-----------------|-------------------|
| KMP-003 | Arızalar zamanla sıklaşıyor, rulman/titreşim ağırlıklı | En yüksek risk, β > 1, pozitif Laplace U |
| PMP-012 | Sık ama rastgele | Yüksek risk, β ≈ 1 |
| ROB-005 | β = 0.6 (erken dönem arızası) ile üretildi | **Ders:** ~7 arızadan tahmin edilen β gerçek değerden çok sapabilir (ör. 2.0 çıkabilir). Az veride β'ya güvenmeyin |
| KNV-001, GEN-001 | Çok az arıza | Sıralamaya girmez |

```bash
python -m cmms_agent tool asset_reliability --args '{"asset_id":"KMP-003","days":365}'
```
