# 1. Mimari: Küçük donanımda hızlı ve doğru bir agent nasıl kurulur?

## 1.1 Problemi doğru tanımlamak

Beklenti üç şeydi:

1. **Elastic'teki CMMS verisini anlamak ve analiz etmek**
2. **Öngörü sağlamak** (hangi ekipman ne zaman arızalanabilir?)
3. **Jetson Orin Nano'da yerel LLM ile hızlı cevap vermek**

Üçüncü madde diğer ikisinin *nasıl* yapılacağını belirliyor. Orin Nano'nun
8 GB belleği CPU ve GPU arasında paylaşılıyor ve bellek bant genişliği
~68-102 GB/s. Bu, pratikte **3-4 milyar parametreli (3-4B), 4-bit
quantize edilmiş** bir modelin rahat, 7-8B modelin ise sınırda çalışacağı
anlamına gelir (hesabı [02-jetson-kurulum.md](02-jetson-kurulum.md) içinde).

Küçük modellerin güçlü ve zayıf yönleri:

| Güçlü | Zayıf |
|-------|-------|
| Kısa metni anlama, sınıflandırma | Çok adımlı plan yapma |
| Verilen gerçekleri özetleme/yorumlama | Aritmetik, istatistik |
| JSON şemasıyla kısıtlanınca güvenilir çıktı | Uzun bağlamda dikkat dağılması |
| Hızlı (3B ≈ 20-40 token/s Orin'de) | Tool-calling formatını bozabilme |

**Tasarım kararı:** Modelin zayıf olduğu her şeyi modelden alıp
deterministik koda veriyoruz.

## 1.2 Üç yaygın agent mimarisi ve neden bunu seçtik

### (a) ReAct / serbest tool-calling döngüsü
```
LLM düşün → araç çağır → LLM düşün → araç çağır → ... → cevap
```
- Esnek, ama her tur bir LLM çağrısı demek. Orin Nano'da 3-5 tur = 15-40 sn.
- Küçük modeller döngüde kaybolur, aynı aracı tekrar çağırır, formatı bozar.
- **Büyük modeller (GPT-4 sınıfı) için iyi; 3B için değil.**

### (b) Text-to-Query (LLM Elasticsearch DSL / ES|QL yazar)
- Kulağa hoş gelir: "model sorguyu yazsın".
- 3B model karmaşık aggregation DSL'ini sıklıkla hatalı yazar; hatalı sorgu
  = yanlış sayı = güven kaybı. Güvenlik açısından da (keyfi sorgu) risklidir.

### (c) **Router → Tool → Narrator** ✅ (bu projede)
```
Soru ─► Router: hangi araç? (kural ya da LLM, JSON)      ≤ 1 LLM çağrısı, çoğu zaman 0
      ─► Tool: önceden yazılmış, test edilmiş sorgu+analiz   0 LLM çağrısı
      ─► Narrator: gerçekleri Türkçe yorumla (akışlı)       1 LLM çağrısı
```
- En fazla **2** LLM çağrısı; sık sorularda **1**.
- Sorgular elle yazılıp test edildiği için sayılar her zaman doğru.
- LLM sadece "ne soruldu?" ve "bu sayılar ne anlama geliyor?" işini yapıyor.

Bu desene literatürde "semantic router", "intent-based tool dispatch" ya da
"deterministic tools + LLM summarizer" da denir.

## 1.3 Gecikme bütçesi (latency budget)

Kullanıcı açısından "hızlı" = **ilk kelimenin ekrana gelme süresi (TTFT)**.
Tipik bir istek için yaklaşık bütçe (3B Q4 model, Orin Nano Super modunda;
kendi ölçümünüz için `scripts/benchmark.py`):

| Adım | Süre (yaklaşık) | Nasıl küçülttük |
|------|-----------------|-----------------|
| Router (kural) | ~0 ms | Regex + anahtar kelime, sık soruların çoğu |
| Router (LLM) | 0.5-1.5 sn | Sadece belirsizse; JSON şema; ~30 token çıktı |
| Elastic sorgusu | 20-300 ms | `size=0` aggregation, sadece gereken alanlar |
| Analitik | < 10 ms | Saf Python, küçük veri |
| Prompt işleme (prefill) | 0.3-1.5 sn | Kompakt JSON, sabit sistem prompt'u (KV-cache yeniden kullanımı) |
| İlk token | **~1-2 sn** | Akış (streaming) |
| Tüm cevap (~250 token) | 8-15 sn | `max_tokens` sınırı, kısa cevap talimatı |

> **Önemli ipucu — prompt önbelleği:** llama.cpp ve Ollama, bir önceki
> istekle *aynı başlayan* prompt kısmının KV-cache'ini yeniden kullanır. Bu
> yüzden sistem prompt'ları **sabit** tutulur ve değişken kısım (soru, veri)
> **sonda** yer alır (`agent.py` → `messages()`). Sistem prompt'una tarih,
> saat gibi değişken bir şey koymak bu kazancı yok eder.

## 1.4 Veri akışı ve "facts" sözleşmesi

Her araç LLM'e ham satır değil, **özet gerçekler** verir:

```json
{"period_days":365,"horizon_days":30,"ranking":[
  {"asset_id":"KMP-003","failures":18,"mtbf_days":18.58,
   "days_since_last_failure":3.78,"risk_pct":89.48,"risk_level":"yüksek",
   "trend":"kötüleşme_eğilimi","weibull_beta":1.73}]}
```

Kurallar:
- ~1-2 KB'ı geçme (her 1 KB ≈ 300-400 token prefill).
- Sayıları yuvarla (`89.48`, `89.4812731` değil) → daha az token.
- Alan adları açıklayıcı olsun; LLM'e ayrıca tablo açıklaması gerekmesin.
- Seviye etiketleri (`risk_level`, `trend`) hazır gelsin; LLM eşik uydurmasın.

## 1.5 Hata dayanıklılığı

- Router LLM'i yanlış/boş JSON dönerse → genel KPI özeti (fallback).
- Araç hata verirse → `{"error": ...}` narrator'a gider, LLM "veriye
  ulaşılamadı" der; agent çökmez.
- Regex'le çıkarılan sayılar (gün, ID) LLM'in çıkardıklarını **ezer**; küçük
  modeller "son 3 ay"ı 3 gün sanabilir.

## 1.6 Güvenlik

- Elastic'e **sadece okuma** yetkili API key ile bağlanın (bkz. doküman 3).
- LLM hiçbir zaman sorgu yazmaz; sadece `ROUTE_SCHEMA`'daki enum'dan araç
  seçer. Prompt injection ile "index'i sil" dedirtmek mümkün değildir.
- API'yi fabrika ağı dışına açmayın; gerekirse önüne reverse proxy + auth koyun.
