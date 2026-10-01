# 7. Gerçek bir vaka: önceki "universal" agent neden yavaştı?

Daha önce yazılmış bir "Universal Elasticsearch Agentic System" (ReAct
deseni, 3 genel araç, Ollama/Gemini) incelendi. "Çok ağır çalışıyordu."
Bu dokümanda neden ağır olduğunu tek tek inceliyoruz. Kötülemek için değil:
bu hataların hepsi agent geliştiren hemen herkesin bir noktada yaptığı,
çok öğretici hatalar.

## 7.1 Bir sorunun yolculuğu (eski sistem)

```
Soru
 └─ İterasyon 1: LLM → "önce get_schema_info çağırayım"      (LLM çağrısı #1)
 └─ İterasyon 2: LLM → "universal_search / aggregation"       (LLM çağrısı #2)
 └─ İterasyon 3: LLM → "done"                                 (LLM çağrısı #3)
 └─ Final: LLM → cevabı yaz (stream yok)                      (LLM çağrısı #4)
```
En iyi durumda 4, en kötü durumda 6 **ardışık** LLM çağrısı. Sistem
prompt'u bunu zorunlu kılıyordu: "MUTLAKA İLK ÖNCE get_schema_info ÇAĞIR".
Böylece her soru, cevabı her seferinde aynı olan bir bilgi için bir tur
harcıyordu.

## 7.2 Yavaşlığın kök nedenleri (etki sırasıyla)

| # | Sorun | Neden pahalı? | Bu projede |
|---|-------|---------------|------------|
| 1 | **Model: `gpt-oss:20b`** (varsayılan) | ~13-14 GB ağırlık; 8 GB'lık Orin Nano'ya hiç sığmaz, PC'de bile token başına çok bellek okur. Ayrıca bir "reasoning" modeli: cevaptan önce düşünme token'ları üretir | 3B Q4 (~1.9 GB), düşünme kapalı |
| 2 | **4-6 ardışık LLM çağrısı** (ReAct + zorunlu şema turu) | Her çağrı = prompt işleme + üretim. Gecikmeler toplanır | 0-1 yönlendirme + 1 anlatım |
| 3 | **Şişkin prompt'lar** | Sistem prompt'u hem `system` rolünde hem kullanıcı mesajında gönderiliyordu (iki kat token). Şema alan listeleri her turda tekrar ediliyordu | Sabit, kısa sistem prompt'u; şema hiç gönderilmez |
| 4 | **Ham belgeler LLM'e** | `universal_search` varsayılan `size=50` ile tam `_source` belgelerini döndürüyordu; 3000-5000 karakterlik JSON her turda prompt'a giriyordu | Araçlar ~1 KB özet "facts" döndürür |
| 5 | **JSON'u ortadan kesmek** | `json.dumps(...)[:3000]` geçerli JSON'u yarıda keser; model bozuk bağlamı yorumlamaya çalışır, daha çok tur gerekir | Facts zaten küçük; kesmeye gerek yok |
| 6 | **Akış (stream) yok** | Kullanıcı tüm turların ve tüm cevabın bitmesini bekler; "ilk kelime" anı = toplam süre | Token akışı; ilk kelime ~1-2 sn |
| 7 | **Serbest JSON** | Şema kısıtlaması yok, açgözlü `\{.*\}` regex'i; parse hatası = döngü erken biter ya da tekrar denenir | `response_format: json_schema` + enum'lu araç adları |
| 8 | **Async içinde bloklayan çağrı** | `async def` endpoint içinde senkron `requests` çağrısı FastAPI'nin olay döngüsünü kilitler: bir soru işlenirken `/health` bile cevap vermez | Senkron endpoint'ler (FastAPI thread pool'da çalıştırır) |
| 9 | **Açılışta tüm index'leri keşfetmek** | `index="*"` ile her index'in mapping'i import anında çekiliyordu; büyük kümelerde yavaş açılış ve devasa şema | Keşif ayrı bir komut (`discover`), bir kez |

## 7.3 Doğruluk ve güvenlik sorunları

- **Global değişken yarışı:** İstek geldiğinde `ELASTICSEARCH_INDEX`
  global olarak değiştirilip sonra geri alınıyordu. Aynı anda iki kullanıcı
  gelirse biri diğerinin index'inde sorgu çalıştırabilirdi.
- **`.keyword` karışıklığı:** Arama alanlarına keyword alanlar için
  `alan.keyword` ekleniyordu. Oysa keyword alanların `.keyword` alt alanı
  yoktur (bu, *text* alanlarda olur). Bu projede de tersi bir `.keyword`
  hatası yakalandı (bkz. doküman 3).
- **Sadece üst seviye alanlar:** Şema keşfi `properties`'in ilk seviyesine
  bakıyordu; iç içe nesneler ve multi-field'lar görünmüyordu.
  `_field_caps` ikisini de düz liste olarak verir.
- **Zaman penceresi:** Varsayılan `now-24h`, log analizi için mantıklı ama
  bakım verisi için değil. MTBF aylarca veri ister.
- **LLM'in yetkisi fazla:** Model hangi index'i (`*` dahil) sorgulayacağına,
  filtrelere ve `query_string` içeriğine karar veriyordu. Prompt injection
  ile istenmeyen index'leri okutmak mümkündü. Ayrıca ES bağlantısında
  kimlik doğrulama/TLS seçeneği yoktu; CORS `*` ve `reload=True` varsayılandı.

## 7.4 İyi olan ve bu projeye taşınan fikirler

| Eski sistemdeki fikir | Bu projede nasıl yaşıyor |
|-----------------------|--------------------------|
| Otomatik şema keşfi (`IndexSchema`) | `cmms_agent/discovery.py` + `discover` komutu: `_field_caps`, Türkçe isim kalıpları, gerçek değer örnekleri; **kurulumda bir kez**, insan onaylı |
| Açılışta "model yüklü mü?" kontrolü | `doctor` artık modelin sunucuda olup olmadığını da söylüyor |
| Sağlayıcıdan bağımsız LLM arayüzü | OpenAI-uyumlu tek istemci (`llm.py`): Ollama, llama.cpp, MLC |
| Her adımı loglamak | Her cevapta `timings_ms` (yönlendirme / araç / ilk token / toplam) + `--debug` ile facts |
| Hata durumunda düz LLM cevabına düşmek | Router "none" aracı ve araç hatasında agent'ın çökmemesi |

## 7.5 Genel ders

> **Agent'ın "zekâsı" ile sistemin hızı ters orantılıdır.** Her kararı
> LLM'e bırakmak (hangi index, hangi alan, hangi sorgu, ne zaman bitti)
> esnek görünür. Ama her karar bir LLM turu, her tur saniyeler ve her
> tahmin bir hata olasılığı demektir. Kararların çoğu (şema, sorgu
> şablonları, istatistik) önceden ve deterministik verilebilir. LLM'i sadece
> gerçekten dil anlama gerektiren yerlerde kullanın.

Genel amaçlı bir "her index'e her soruyu sor" aracı yine de faydalı
olabilir (ör. keşif amaçlı analist kullanımı için). Böyle bir araç
eklenecekse doküman 5.1'deki gibi ayrı bir araç olarak eklenmeli ve şu
sınırlarla tasarlanmalı: izinli index ve alan listesi, `size=0`
aggregation, tek tur.
