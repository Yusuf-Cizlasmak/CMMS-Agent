# 5. Genişletme: agent'ı kendi fabrikanıza göre büyütmek

## 5.1 Yeni bir araç eklemek (3 adım)

Örnek: "Teknisyen bazında iş yükü" aracı.

**1) `tools.py` içine fonksiyonu yazın** — Elastic'ten aggregation, küçük facts:
```python
def technician_workload(ctx: ToolContext, args: dict) -> dict:
    repo = ctx.repo
    days = _days(ctx, args)
    r = repo.aggregate(repo.bool_query(repo.time_filter(days)), {
        "techs": {"terms": {"field": "technician", "size": 10},
                  "aggs": {"labor": {"sum": {"field": repo.f.labor_hours}}}}})
    return {"period_days": days, "technicians": [
        {"name": b["key"], "work_orders": b["doc_count"],
         "labor_hours": _r(b["labor"]["value"])} for b in r["techs"]["buckets"]]}
```

**2) `TOOLS` sözlüğüne kaydedin** — açıklama kısa ve ayırt edici olsun
(LLM router bunu okuyor):
```python
Tool("technician_workload", "Teknisyen bazında iş emri sayısı ve işçilik saati",
     "days", technician_workload),
```

**3) `router.py → route_by_rules` içine anahtar kelime kuralı ekleyin:**
```python
if _has(q, "teknisyen", "iş yükü", "personel"):
    return Route("technician_workload", args)
```
Ve `tests/test_router.py`'ye bir örnek soru ekleyin. Bitti — LLM router'ın
JSON şeması `TOOLS`'tan otomatik türetilir.

## 5.2 Değerlendirme (evaluation) — "iyi çalışıyor mu?" sorusunu ölçmek

LLM projelerinin en çok atlanan ama en önemli adımı. Önerilen minimum:

1. Kullanıcılardan **30-50 gerçek soru** toplayın.
2. Her biri için beklenen aracı ve argümanları yazın (`tests/test_router.py`
   formatında). Bu, router doğruluğunu ölçer — model veya prompt
   değiştirdiğinizde regresyonu yakalar.
3. Anlatım kalitesi için 10 soruluk bir set üzerinde bakım mühendislerine
   cevapları 1-5 arası puanlatın: *doğruluk* (sayılar veriyle aynı mı?),
   *faydalılık* (öneri eyleme dönük mü?), *netlik*.
4. Her model/ayar değişikliğinde bu seti ve `benchmark.py`'yi tekrar çalıştırın.

Otomatik kontrol fikri: cevaptaki tüm sayıları regex ile çıkarıp facts
JSON'unda geçip geçmediğini kontrol edin → "uydurma sayı" oranı.

## 5.3 Sensör / durum izleme verisi ile gerçek kestirimci bakım

İş emri geçmişi "ne zaman arızalandı?"yı söyler; sensör verisi "şu an nasıl?"
sorusunu cevaplar. Elastic'te `cmms-readings` gibi bir index'iniz varsa
(asset_id, metric, value, @timestamp):

- **Araç:** son 24 saat vs. son 30 gün ortalaması, z-skoru, eşik aşımları
  (`date_histogram` + `avg`/`percentiles` aggregation).
- **Elastic ML** (lisansa bağlı): anomali tespit işlerini Elastic'te çalıştırıp
  sonuç index'ini (`.ml-anomalies-*`) agent'a araç olarak açabilirsiniz.
  Böylece ağır ML Jetson'da değil, Elastic kümesinde koşar.
- **Birleşik risk:** Weibull riski × sensör anomali skoru gibi basit bir
  birleşim bile tek başına her ikisinden daha iyi sıralama verir.

## 5.4 Daha gelişmiş tahmin modelleri

Veri arttıkça (yüzlerce ekipman, binlerce arıza):

| Yaklaşım | Kütüphane | Ne kazandırır |
|----------|-----------|---------------|
| Weibull MLE, sansürlü veri | `reliability`, `lifelines` | Daha doğru β/η, güven aralıkları |
| Sağkalım regresyonu (Cox) | `lifelines` | Ekipman tipi, lokasyon, yaş gibi değişkenlerin etkisi |
| Gradient boosting sınıflandırıcı | `lightgbm`, `scikit-learn` | "30 gün içinde arıza" olasılığı, çok değişkenli |

Bunları Jetson'da *eğitmek* yerine sunucuda periyodik (ör. gece) eğitip,
skorları Elastic'e (`cmms-risk-scores` index'i) yazın; agent sadece okusun.
Böylece Jetson hafif ve hızlı kalır.

## 5.5 Doküman bilgisi (RAG): bakım kılavuzları, prosedürler

"KMP-003 rulman değişim prosedürü nedir?" gibi sorular için:
1. PDF kılavuzları parçalara bölün (300-500 token).
2. Embedding üretin — Jetson'da Ollama ile: `ollama pull nomic-embed-text`
   veya çok dilli `bge-m3` (Türkçe için daha iyi).
3. Elastic'te `dense_vector` alanına yazın, `knn` araması yapın.
4. Yeni bir `search_manuals` aracı → en alakalı 3 parça → narrator.

Dikkat: embedding modeli de bellekte yer tutar (~300-600 MB).

## 5.6 İnce ayar (fine-tuning) — gerekli mi?

Çoğu durumda **hayır**. Önce sırasıyla deneyin:
1. Kural router'a kelime eklemek
2. Router prompt'una few-shot örnek eklemek
3. Daha güçlü bir model (3B → 4B → 7B)

Hâlâ yetersizse, 200-1000 (soru → araç JSON) örneğiyle küçük bir modele
**LoRA** ince ayarı (ör. Unsloth veya Hugging Face PEFT ile, bir GPU'lu PC/
bulutta) yapıp GGUF'a çevirerek Jetson'a taşıyabilirsiniz. Eğitim Jetson'da
yapılmaz; sadece çıkarım (inference) yapılır.

## 5.7 Çok kullanıcılı kullanım

- Jetson tek bir LLM akışını en hızlı çalıştırır. Aynı anda birden çok
  kullanıcı varsa istekler sıraya girer.
- `cache.py` aynı soruların Elastic maliyetini sıfırlar; sık sorulan raporlar
  (ör. sabah vardiya özeti) için cevabı da önbelleğe almak düşünülebilir.
- Yüksek eşzamanlılık gerekiyorsa: llama.cpp `--parallel 2` (KV-cache
  bölünür) veya ikinci bir Jetson.
