# 6. Sensör verisi ve değerlendirme (eval)

## 6.1 Neden sensör verisi?

İş emri geçmişi **"geçmişte ne oldu?"** sorusunu cevaplar. Weibull ve MTBF
buna dayanarak olasılık üretir, ama makinenin *şu anki* durumunu bilmez.
Sensörler (titreşim, sıcaklık, akım, basınç) **"şu an nasıl?"** sorusunu
cevaplar. İkisi birleşince gerçek **kestirimci bakım** (predictive maintenance) olur:

| Kanıt | Kaynak | Güçlü yanı | Zayıf yanı |
|-------|--------|-----------|------------|
| Geçmiş | `cmms-workorders` | Uzun vadeli risk, yaşlanma (β) | Anlık bozulmayı görmez |
| Şimdi | `cmms-readings` | Bozulmayı günler önce yakalar | Uzun vadeli risk söylemez |

## 6.2 Beklenen veri formatı ("uzun" format)

Her belge tek bir ölçümdür:
```json
{"asset_id": "KMP-003", "metric": "vibration_mm_s", "value": 5.61,
 "unit": "mm/s", "@timestamp": "2026-09-30T08:00:00Z"}
```
Mapping: `asset_id`, `metric` → `keyword`; `value` → `float`;
`@timestamp` → `date`. Alan adları `.env` içinden değiştirilebilir
(`CMMS_READINGS_INDEX`, `CMMS_RFIELD_*`).

Veriniz "geniş" formattaysa (tek belgede `{vibration: .., temperature: ..}`),
bir **ingest pipeline** ile uzun formata çevirebilir ya da aracı her
metrik alanına ayrı `avg` aggregation yapacak şekilde uyarlayabilirsiniz.
SCADA, OPC-UA ya da MQTT kaynaklı veriler genelde Logstash / Elastic Agent /
Telegraf ile Elastic'e aktarılır.

> **Örnekleme sıklığı:** Agent saatlik veya dakikalık ortalamalarla
> çalışmaya yeter. Titreşim spektrumu (FFT) gibi yüksek frekanslı ham veriyi
> agent'a değil, kenar cihazdaki bir ön işleyiciye verin. Oradan çıkan
> özet metrikleri (RMS, tepe değer, kurtosis) Elastic'e yazın.

## 6.3 `sensor_health` aracı nasıl çalışır?

Tek bir aggregation isteği atılır: ekipman → metrik → üç alt ölçüm.

```
|──── referans dönem (baseline) ────|── trend penceresi (7 gün) ──|
now-30d                          now-7d                          now
                                                        |- son 24 saat -|
```

1. **Referans:** `[now-30d, now-7d)` aralığının ortalaması ve standart sapması.
   Referans dönem *bilerek* son 7 günü dışarıda bırakır. Yavaş bir bozulma
   referansın içine sızarsa ortalamayı ve std'yi şişirir, sapma "normal"
   görünür. Bu projeyi geliştirirken tam olarak bu hataya düştük: FRN-002'nin
   ısınması ilk sürümde görünmüyordu.
2. **z-skoru:** son 24 saatin ortalaması referanstan kaç std uzakta?
   `|z| ≥ 2` uyarı, `|z| ≥ 3` kritik.
3. **Eğim:** son 7 günlük ortalamaların doğrusal eğimi, referansın yüzdesi
   olarak (%/gün). `≥ %2/gün` uyarı, `≥ %5/gün` kritik. z-skoru henüz
   yükselmeden yavaş bozulmayı yakalar.

Neden sabit eşik (ör. "titreşim > 7 mm/s") değil? Her makinenin normali
farklıdır. Kendi geçmişine göre sapma, ekipman başına eşik girmeden çalışır.
ISO 10816 / 20816 gibi standart titreşim sınırlarınız varsa bunları ek bir
kural olarak eklemek iyi bir fikirdir.

## 6.4 Risk sıralamasıyla birleştirme

`failure_risk_ranking`, sensör index'i varsa her ekipmanın en kötü sensör
durumunu satıra ekler ve sıralamayı basit, açıklanabilir bir kuralla
ayarlar:

```
skor = Weibull/üstel risk × (kritik: 1.3 | uyarı: 1.15 | normal: 1.0)
```
Katsayılar sezgiseldir. Yeterli geçmiş veriniz olduğunda, sensör
anomalisinden sonra gerçekten arıza çıkma oranına bakarak bunları
kalibre edin. Ya da daha ileri seviye için bir sınıflandırıcı eğitin
(doküman 5.4).

`asset_reliability` de ilgili ekipmanın sensör özetini (`sensors`)
cevabına ekler. Böylece "KMP-003 arızalanır mı?" sorusu hem geçmişe hem
şimdiye bakarak cevaplanır.

## 6.5 Değerlendirme (eval) — ölçmediğiniz şeyi iyileştiremezsiniz

```bash
python scripts/evaluate.py                 # kural router, LLM'siz, anında
python scripts/evaluate.py --mode llm      # sadece LLM router → modelleri kıyaslayın
python scripts/evaluate.py --mode hybrid --full   # + cevaplar, sayı kontrolü, süre
```

Soru seti: [`eval/questions.jsonl`](../eval/questions.jsonl). Her satırda
soru, beklenen araç ve beklenen argümanların bir alt kümesi var:
```json
{"q": "Son 90 günde en çok arıza yapan 5 makine", "tool": "top_failing_assets", "args": {"days": 90, "top_n": 5}}
```

### Ölçülen metrikler

| Metrik | Ne söyler | Hedef |
|--------|-----------|-------|
| `route_accuracy` | Doğru araç ve argüman seçildi mi? | ≥ %95 |
| `grounding_rate_avg` | Cevaptaki sayıların ne kadarı veride var? | ≥ %95 |
| `answers_with_ungrounded_numbers` | Uydurma sayı içeren cevap sayısı | 0'a yakın |
| `first_token_ms_p50` | Kullanıcının hissettiği hız | < 2000 ms |

**Sayı dayanaklılığı (grounding)** nasıl hesaplanır
(`cmms_agent/evaluation.py`): Cevaptaki tüm sayılar çıkarılır. Tarihler,
ekipman ID'leri ve liste numaraları bu sırada atılır. Her sayı, araç
verisinde ya da soruda geçiyor mu diye kontrol edilir; yuvarlamaya
(89.48 → "%89") ve oran/yüzde dönüşümüne (0.83 → "%83") izin verilir.
Eşleşmeyen sayılar büyük olasılıkla LLM'in uydurmasıdır.

### İş akışı önerisi

1. Kullanıcıların gerçek sorularını toplayın (API loglarından) ve
   `questions.jsonl`'a ekleyin.
2. Yanlış yönlenen soru için önce **kural** ekleyin (en hızlısı), sonra
   gerekirse router prompt'una few-shot örnek ekleyin.
3. `tests/test_router.py::test_eval_set_rules_accuracy` eval setini CI'da
   korur: kurallarda yapılan bir değişiklik eski bir soruyu bozarsa test
   kırılır.
4. Model değiştirirken (ör. 3B → 4B) `--mode llm --full` çalıştırıp
   `route_accuracy`, `grounding_rate_avg` ve `first_token_ms_p50`'yi
   yan yana koyun. Karar bu üç sayıyla verilir.

> Bu projede eval seti ilk çalıştırıldığında kural router %92'de kaldı:
> "muhtemel" kelimesi tanınmıyordu ve "yağ kaçağına benzer…" kalıbından
> yanlış arama metni çıkıyordu. İkisi de düzeltildi, doğruluk %100'e çıktı.
> Eval'in değeri tam olarak budur.
