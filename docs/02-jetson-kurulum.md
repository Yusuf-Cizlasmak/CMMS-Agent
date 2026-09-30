# 2. Jetson Orin Nano: LLM motoru, model ve kütüphane seçimi

## 2.1 Donanımı tanıyalım

| Özellik | Jetson Orin Nano 8 GB |
|---------|-----------------------|
| CPU | 6 çekirdek Arm Cortex-A78AE |
| GPU | 1024 CUDA çekirdekli Ampere, 32 Tensor Core, **compute capability 8.7 (sm_87)** |
| Bellek | 8 GB LPDDR5, **CPU ve GPU ortak kullanır** (unified memory) |
| Bant genişliği | ~68 GB/s (orijinal) → ~102 GB/s (JetPack 6.2 "Super" / MAXN SUPER modu) |
| Yazılım | JetPack 6.x → Ubuntu 22.04, CUDA 12.x, cuDNN, TensorRT |

> **JetPack 6.2+ kullanın.** Aynı donanımda "Super" güç modunu açarak
> bellek bant genişliğini ve GPU saatini artırır; LLM hızı için ücretsiz
> kazançtır. `sudo nvpmodel -q --verbose` ile modları listeleyip en yükseğini
> seçin, ardından `sudo jetson_clocks`.

### LLM hızını belirleyen formül (öğrenmeniz gereken en önemli şey)

Token üretimi (decode) **bellek bant genişliği sınırlıdır**: her yeni token
için modelin tüm ağırlıkları bellekten bir kez okunur.

```
teorik üst sınır (token/s) ≈ bant genişliği (GB/s) / model boyutu (GB)
```

| Model (4-bit Q4_K_M) | Dosya boyutu | Teorik tavan @102 GB/s | Pratikte (≈%50-70) |
|----------------------|--------------|------------------------|--------------------|
| 1.5B | ~1.0 GB | ~100 tok/s | ~50-70 tok/s |
| 3B   | ~1.9 GB | ~53 tok/s  | ~25-40 tok/s |
| 4B   | ~2.5 GB | ~40 tok/s  | ~20-30 tok/s |
| 7-8B | ~4.7 GB | ~22 tok/s  | ~10-16 tok/s |

(Pratik değerler motor, sürüm ve ayarlara göre değişir; **mutlaka**
`scripts/benchmark.py` ile kendi cihazınızda ölçün.) İnsanın okuma hızı
~5-8 token/s olduğu için 20+ tok/s akışlı cevap "anlık" hissettirir.

Çıkarım: **Hız için modeli küçültün ve quantize edin.** Q4_K_M, 3-8B
modellerde kalite/hız dengesi için endüstri standardıdır. Q8 kaliteyi az
artırır ama hızı ~yarıya düşürür.

### Bellek bütçesi (8 GB)

| Kalem | Yaklaşık |
|-------|----------|
| Ubuntu (masaüstü kapalı / headless) | 1.0-1.5 GB (masaüstü açıkken +0.8-1 GB) |
| LLM sunucu (CUDA bağlamı vb.) | 0.3-0.6 GB |
| Model ağırlıkları (3B Q4_K_M) | ~1.9 GB |
| KV-cache (4096 bağlam) | ~0.1-0.3 GB |
| Agent (Python + FastAPI) | ~0.1 GB |
| **Toplam** | **~3.5-4.5 GB** → 7B modele de yer var ama sınırda |

KV-cache hesabı (GQA'lı modellerde küçüktür):
```
KV bayt/token = 2 (K ve V) × katman × kv_head × head_dim × bayt
Qwen2.5-3B:  2 × 36 × 2 × 128 × 2 (fp16) ≈ 36 KB/token → 4096 token ≈ 150 MB
q8_0 KV-cache ile bunun yarısı.
```

## 2.2 LLM çalıştırma motorları (inference engine)

Hepsi **OpenAI uyumlu HTTP API** sunar; agent kodu motor bağımsızdır
(`LLM_BASE_URL` değiştirmek yeterli).

| Motor | Kurulum | Hız | JSON şema | Öneri |
|-------|---------|-----|-----------|-------|
| **Ollama** | `curl -fsSL https://ollama.com/install.sh \| sh` (Jetson'ı tanır) | İyi | ✅ (`format` / `response_format`) | **Başlangıç için 1. tercih** |
| **llama.cpp** (`llama-server`) | Kaynaktan CUDA ile derle (`-DCMAKE_CUDA_ARCHITECTURES=87`) | İyi, ayarlanabilir | ✅ (grammar/json_schema) | **Üretimde kontrol isteyene** |
| **MLC LLM** | `jetson-containers` konteyneri | Jetson'da genelde **en hızlı** decode | Kısmi | Hız kritikse deneyin |
| TensorRT-LLM | NVIDIA'nın Jetson dalı, karmaşık | Çok iyi | Kısmi | İleri seviye |
| vLLM | `jetson-containers` | İyi ama bellek aç | ✅ | 8 GB için önerilmez |

**Öneri:** Ollama ile başlayın (15 dakikada çalışır). Performans ya da
bellek kontrolüne ihtiyaç duyduğunuzda llama.cpp'ye geçin — ikisi de aynı
GGUF model dosyalarını kullanır. MLC'yi en son, gerçekten birkaç token/s
daha gerekiyorsa değerlendirin.

### jetson-containers (alternatif kurulum yolu)

NVIDIA'dan Dustin Franklin'in bakımını yaptığı
[jetson-containers](https://github.com/dusty-nv/jetson-containers) projesi,
JetPack sürümünüze uygun önceden derlenmiş konteynerler sunar:

```bash
git clone https://github.com/dusty-nv/jetson-containers
bash jetson-containers/install.sh
jetson-containers run $(autotag ollama)      # veya llama_cpp, mlc
```
Avantaj: CUDA/cuDNN sürüm uyumsuzluğu derdi yok. Dezavantaj: Docker katmanı
ve disk kullanımı. [Jetson AI Lab](https://www.jetson-ai-lab.com) sitesinde
güncel eğitimler ve benchmark'lar var.

## 2.3 Model seçimi (Türkçe + hız + JSON)

| Model | Boyut (Q4_K_M) | Türkçe | JSON/araç | Not |
|-------|----------------|--------|-----------|-----|
| **Qwen2.5-3B-Instruct** | ~1.9 GB | İyi | Çok iyi | **Varsayılan.** Hız/kalite dengesi |
| Qwen3-4B-Instruct-2507 | ~2.5 GB | İyi | Çok iyi | Daha güçlü; "thinking" olmayan sürümü seçin |
| Gemma 3 4B (it) | ~2.5-3 GB | Çok iyi | İyi | Türkçe anlatım kalitesi yüksek |
| Llama 3.2 3B Instruct | ~2.0 GB | Resmi destek listesinde değil | İyi | Türkçe için ilk tercih değil |
| Qwen2.5-7B-Instruct | ~4.7 GB | Çok iyi | Çok iyi | Kalite yetmezse; ~yarı hız |
| Türkçe ince ayarlı 7-8B modeller (ör. Trendyol LLM) | ~4.5-5 GB | Çok iyi | Değişken | Kendi değerlendirmenizi yapın |

Pratik yol:
1. `qwen2.5:3b-instruct` ile başlayın.
2. `tests/` altındaki gibi 20-30 gerçek soruluk bir değerlendirme listesi
   hazırlayın (bkz. doküman 5) ve 2-3 modeli karşılaştırın.
3. Hız (benchmark.py) ve cevap kalitesine göre karar verin.

> **"Thinking" modelleri:** Qwen3, DeepSeek-R1-Distill gibi modeller cevaptan
> önce yüzlerce token "düşünür" → Jetson'da saniyeler kaybı. Bu iş için
> düşünme modunu kapatın (Qwen3'te `/no_think` ya da "Instruct-2507" sürümü).
> `llm.py` yine de `<think>` bloklarını filtreler.

> **Router için ayrı mini model?** Qwen2.5-0.5B gibi bir modeli sadece
> yönlendirme için kullanmak mümkündür, ancak iki model aynı anda bellekte
> tutulur. Bu projede kural tabanlı router soruların çoğunu LLM'siz
> çözdüğü için tek model yeterlidir.

## 2.4 Hangi Python kütüphaneleri? (Jetson'da)

Agent tarafı **bilinçli olarak hafif** tutuldu; hiçbiri GPU/CUDA istemez,
hepsinin aarch64 (arm64) wheel'i PyPI'da vardır:

| Kütüphane | Ne için | Not |
|-----------|---------|-----|
| `elasticsearch` | Elastic istemcisi | **Ana sürüm sunucuyla aynı olmalı** (ES 8 → `elasticsearch>=8,<9`) |
| `httpx` | LLM sunucusuna HTTP/SSE | `openai` paketi de olur; httpx ile tam kontrol |
| `fastapi` + `uvicorn` | HTTP API | `--workers 1` |
| (yok) numpy/pandas/torch | — | Analitik saf Python; bellek ve kurulum derdi yok |

Ağır iş **LLM sunucusunda** (Ollama/llama.cpp, C++/CUDA). Python sürecinin
torch yüklemesine gerek yok — bu Jetson'da ~1 GB bellek tasarrufu demek.

İleride ihtiyaç duyarsanız:
- **PyTorch / torchvision (Jetson):** PyPI'daki genel `torch` wheel'leri
  Jetson GPU'sunu kullanmaz. NVIDIA'nın JetPack'e özel wheel'lerini ya da
  jetson-containers'ın pip index'ini kullanın (güncel URL için
  jetson-containers README'sine bakın).
- **llama-cpp-python** (LLM'i aynı Python sürecinde çalıştırmak için):
  `CMAKE_ARGS="-DGGML_CUDA=on -DCMAKE_CUDA_ARCHITECTURES=87" pip install llama-cpp-python`
  Derleme 20-40 dk sürer. Ayrı sunucu (Ollama/llama-server) daha izole ve
  yeniden başlatması kolay olduğu için önerilir.
- **scikit-learn, lightgbm, lifelines** (daha gelişmiş tahmin modelleri):
  aarch64 wheel'leri mevcuttur, CPU'da çalışır.
- **sentence-transformers / embedding** (doküman araması, RAG): torch ister;
  alternatif olarak Ollama'nın embedding modelleri (`nomic-embed-text`,
  `bge-m3`) API üzerinden kullanılabilir.

## 2.5 Adım adım kurulum

```bash
# 0) JetPack 6.2+ kurulu, NVMe SSD'den boot önerilir (SD kart yavaş)
git clone <bu-repo> ~/CMMS-Agent && cd ~/CMMS-Agent
cp .env.example .env   # ES bilgilerini girin

# 1) Tek komut (betiği önce okuyun!)
bash scripts/jetson_setup.sh ollama

# 2) Kontrol
source .venv/bin/activate
python -m cmms_agent doctor
python scripts/benchmark.py --runs 3

# 3) Servis olarak çalıştır
sudo cp deploy/cmms-agent.service /etc/systemd/system/   # yolları düzenleyin
sudo systemctl daemon-reload && sudo systemctl enable --now cmms-agent
```

### Ollama için hız ayarları (betik otomatik yapar)
```
OLLAMA_KEEP_ALIVE=-1        # model bellekte kalsın (soğuk başlangıç 5-15 sn!)
OLLAMA_FLASH_ATTENTION=1
OLLAMA_KV_CACHE_TYPE=q8_0   # KV-cache belleği yarıya
OLLAMA_NUM_PARALLEL=1
```
Bağlam penceresi `deploy/Modelfile` içindeki `num_ctx 4096` ile sabitlenir.

### llama.cpp için
```bash
bash scripts/jetson_setup.sh llamacpp
bash deploy/run_llama_server.sh      # bayrakların açıklaması dosyada
./llama.cpp/build/bin/llama-bench -m models/model.gguf -ngl 99   # ham hız
```

## 2.6 İzleme ve sorun giderme

| Belirti | Olası neden / çözüm |
|---------|---------------------|
| İlk soru 10+ sn, sonrakiler hızlı | Model soğuk yükleniyor → `KEEP_ALIVE=-1`, açılışta bir "ısınma" isteği |
| Hep yavaş (≤5 tok/s) | GPU kullanılmıyor. `tegrastats` ile `GR3D_FREQ` kontrol; Ollama loglarında "CUDA" arayın |
| OOM / süreç ölüyor | Masaüstünü kapatın, bağlamı düşürün (`num_ctx 2048`), daha küçük model |
| Performans dalgalı | `jetson_clocks` çalışmıyor; sıcaklık (fan) kontrolü |
| JSON router hataları | `LLM_JSON_MODE=json` veya `none` deneyin (eski sunucu sürümü) |

İzleme için: `tegrastats` (dahili) veya `sudo pip3 install jetson-stats && jtop`.
