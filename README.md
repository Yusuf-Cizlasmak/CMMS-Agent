# CMMS Agent — Jetson Orin Nano üzerinde yerel LLM ile bakım analitiği

Elasticsearch'teki CMMS (bakım yönetim sistemi) verilerini okuyup analiz eden,
**önümüzdeki dönemde hangi ekipmanın arızalanabileceğini öngören** ve bunu
Türkçe, anlaşılır bir dille anlatan bir agent. Tamamen **yerelde** çalışır:
veri fabrikadan çıkmaz, bulut LLM'e gerek yoktur.

Temsili bir etkileşim (sayılar örnek veriden; süreler donanıma göre değişir):

```
Soru: "Önümüzdeki ay hangi ekipmanlar arızalanabilir?"

KMP-003 (Kompresör 3) önümüzdeki 30 günde ~%89 olasılıkla arıza riski taşıyor.
- 365 günde 18 arıza, MTBF 18.6 gün, son arızadan beri 3.8 gün geçti
- Weibull β=1.73 → aşınma karakteri: zaman geçtikçe risk artıyor
- PMP-012'de arızalar istatistiksel olarak sıklaşıyor (Laplace testi)
Öneriler: KMP-003 için rulman/titreşim kontrolünü PM planına ekleyin...
[failure_risk_ranking · rules] ilk token … ms · toplam … ms
```

## Bu repo size ne öğretir?

Bu proje "çalışan bir agent" olmanın yanında bir **ders kitabı** olarak
tasarlandı. Kodun her dosyası *neden* öyle yazıldığını açıklar.

| # | Doküman | Konu |
|---|---------|------|
| 1 | [docs/01-mimari.md](docs/01-mimari.md) | Neden ReAct değil de "Router → Tool → Narrator"? Gecikme bütçesi |
| 2 | [docs/02-jetson-kurulum.md](docs/02-jetson-kurulum.md) | Jetson Orin Nano: hangi LLM motoru, hangi model, hangi kütüphane, bellek hesabı |
| 3 | [docs/03-elastic-veri.md](docs/03-elastic-veri.md) | Elastic'ten veriyi doğru çekmek: yetki, mapping, aggregation, PIT |
| 4 | [docs/04-analitik-ve-ongoru.md](docs/04-analitik-ve-ongoru.md) | MTBF, MTTR, Weibull, Laplace trend testi — öngörünün matematiği |
| 5 | [docs/05-genisletme.md](docs/05-genisletme.md) | Yeni araç ekleme, gelişmiş modeller, RAG, ince ayar (LoRA) |
| 6 | [docs/06-sensor-ve-degerlendirme.md](docs/06-sensor-ve-degerlendirme.md) | Sensör verisiyle durum izleme, agent'ı ölçmek (eval) |
| 7 | [docs/07-eski-agent-dersleri.md](docs/07-eski-agent-dersleri.md) | Gerçek bir vaka: önceki "universal" ReAct agent neden yavaştı? |

## Mimari (özet)

```
            ┌──────────────── Jetson Orin Nano (8 GB) ────────────────┐
 Kullanıcı  │                                                          │
 (CLI/API) ─┼─► Router ──(kurallar ~0 ms | LLM JSON ~1 sn)             │
            │      │                                                   │
            │      ▼                                                   │
            │   Tool  ── Elasticsearch sorgusu (aggregation) ─────────┼──► Elastic
            │      │     + analytics.py (MTBF, Weibull, trend)         │    (fabrika ağı)
            │      ▼  küçük "facts" JSON (~1 KB)                       │
            │   Narrator (yerel LLM, token akışı) ──► Türkçe cevap     │
            │                                                          │
            │   LLM sunucusu: Ollama veya llama.cpp (CUDA, sm_87)      │
            └──────────────────────────────────────────────────────────┘
```

**Temel ilke:** Sayıları LLM değil, Elastic + istatistik hesaplar. LLM
yalnızca *soruyu anlar* ve *sonucu yorumlar*. Bu hem hızı (küçük model yeter)
hem doğruluğu (halüsinasyon yok) sağlar.

## Hızlı başlangıç

### A) Geliştirme bilgisayarında dene (Jetson olmadan)

```bash
git clone <bu-repo> && cd CMMS-Agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

docker compose up -d elasticsearch          # yerel test ES
cp .env.example .env                        # ES_URL=http://localhost:9200 yapın
python scripts/seed_sample_data.py --reset  # içinde "hikâye" olan sentetik veri

# LLM olmadan araçları doğrudan çalıştır (Elastic tarafını doğrular):
python -m cmms_agent tool failure_risk_ranking --args '{"days":365}'

# Yerel LLM (ör. Ollama) ile:
ollama pull qwen2.5:3b-instruct && ollama create cmms-llm -f deploy/Modelfile
python -m cmms_agent doctor
python -m cmms_agent ask "Önümüzdeki ay hangi ekipmanlar arızalanabilir?" --debug
python -m cmms_agent chat
```

### Kendi Elastic index'inize bağlamak

```bash
python -m cmms_agent discover --index <iş-emri-index'iniz> --write .env.discovered
# Önerileri gözden geçirip .env'ye taşıyın, sonra:
python -m cmms_agent doctor
```
`discover` alan adlarını (Türkçe/İngilizce, `.keyword`, iç içe yollar) ve
"Arıza/Periyodik Bakım/Açık/Kapalı" gibi değerleri otomatik eşleştirir.

### B) Jetson Orin Nano'ya kur

```bash
bash scripts/jetson_setup.sh ollama        # veya: llamacpp
python -m cmms_agent doctor
python scripts/benchmark.py                # TTFT ve token/s ölç
uvicorn cmms_agent.api:app --host 0.0.0.0 --port 8088
```

Ayrıntılar: [docs/02-jetson-kurulum.md](docs/02-jetson-kurulum.md)

### Web arayüzü ve HTTP API

API çalışırken tarayıcıdan `http://<jetson-ip>:8088/` adresini açın. Sade bir
sohbet ekranı gelir: cevap token token akar, her cevabın altında kullanılan
araç, süreler ve "Kullanılan veri" (facts) görünür. Harici bağımlılığı
yoktur, internetsiz fabrika ağında da çalışır.

```bash
curl -s localhost:8088/ask -H 'content-type: application/json' \
     -d '{"question":"KMP-003 önümüzdeki 2 haftada arızalanır mı?"}' | jq
curl -N localhost:8088/ask/stream -H 'content-type: application/json' \
     -d '{"question":"Açık iş emirleri ne durumda?"}'      # SSE token akışı
```

## Agent'ın araçları

| Araç | Ne yapar | Örnek soru |
|------|----------|-----------|
| `kpi_summary` | İş emri dağılımı, MTTR, duruş, maliyet, PM uyumu | "Bu yılın bakım özeti" |
| `top_failing_assets` | En çok arıza/duruş yaşatan ekipmanlar | "Son 90 günde en sorunlu 5 makine" |
| `asset_reliability` | Tek ekipman: MTBF, MTTR, Weibull, trend, arıza olasılığı | "KMP-003 için arıza riski?" |
| `failure_risk_ranking` | **Öngörü:** tüm ekipmanlar için risk sıralaması | "Önümüzdeki ay neler arızalanabilir?" |
| `workorder_trend` | Zaman serisi, eğim, 3 dönem tahmin, anomali | "Arızalar aylık artıyor mu?" |
| `failure_modes` | Arıza kodu Pareto'su | "En sık arıza türleri" |
| `search_workorders` | Açıklamada tam metin (Türkçe analyzer) arama | "'rulman ses' geçen iş emirleri" |
| `open_backlog` | Açık işler, öncelik dağılımı, en eski işler | "Bekleyen işler ne durumda?" |
| `sensor_health` | Sensörlerde normalden sapma (z-skoru) ve bozulma eğimi | "Sensörlerde anormal bir durum var mı?" |

Sensör index'i (`cmms-readings`) varsa `failure_risk_ranking` ve
`asset_reliability` geçmiş iş emirlerini **anlık sensör durumuyla**
birleştirir.

## Proje yapısı

```
cmms_agent/
  config.py      # tüm ayarlar + ALAN EŞLEŞTİRME (kendi index'inize uyarlayın)
  es_client.py   # Elastic bağlantısı, aggregation, PIT+search_after
  analytics.py   # MTBF/MTTR/Weibull/Laplace/trend (saf Python, test edilmiş)
  tools.py       # agent araçları: Elastic → hesap → küçük facts JSON
  router.py      # niyet yönlendirme: kurallar + LLM (JSON şemalı)
  llm.py         # OpenAI-uyumlu yerel LLM istemcisi (Ollama/llama.cpp/MLC)
  agent.py       # orkestrasyon, anlatıcı prompt'u, zamanlama ölçümü
  cache.py       # TTL önbellek
  evaluation.py  # router doğruluğu + sayı dayanaklılığı (grounding) ölçümü
  discovery.py   # index şemasından .env alan eşleştirmesi önerme
  cli.py, api.py # arayüzler
  static/        # web sohbet arayüzü (tek HTML dosyası)
eval/            # değerlendirme soru seti
scripts/         # jetson_setup.sh, seed_sample_data.py, benchmark.py, evaluate.py
deploy/          # Modelfile, llama-server betiği, systemd servisleri
tests/           # birim + gerçek ES entegrasyon testleri
```

## Testler ve değerlendirme

```bash
pytest                                   # ES yoksa entegrasyon testleri otomatik atlanır
python scripts/evaluate.py               # router doğruluğu (eval/questions.jsonl)
python scripts/evaluate.py --mode hybrid --full   # + LLM cevapları, uydurma sayı kontrolü, süre
```
