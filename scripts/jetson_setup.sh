#!/usr/bin/env bash
# Jetson Orin Nano (8 GB) - CMMS Agent kurulum betiği
# Test hedefi: JetPack 6.x (L4T R36.x, Ubuntu 22.04, CUDA 12.x)
#
# Kullanım:
#   bash scripts/jetson_setup.sh ollama      # (önerilen, en kolay)
#   bash scripts/jetson_setup.sh llamacpp    # (en fazla kontrol / hız ayarı)
#
# Her adımın NEDEN yapıldığı yorumlarda açıklanmıştır; körü körüne çalıştırmayın,
# okuyun. Bazı adımlar sudo ister.
set -euo pipefail

ENGINE="${1:-ollama}"
MODEL_OLLAMA="${MODEL_OLLAMA:-qwen2.5:3b-instruct}"
GGUF_URL="${GGUF_URL:-https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf}"

echo "== 0) Sistem bilgisi"
cat /etc/nv_tegra_release 2>/dev/null || echo "Uyarı: Jetson değil gibi görünüyor"
nvcc --version 2>/dev/null | tail -1 || echo "nvcc PATH'te değil: export PATH=/usr/local/cuda/bin:\$PATH"

echo "== 1) Güç modu ve saatler"
# Orin Nano'da en yüksek performans modu. JetPack 6.2+ 'MAXN SUPER' modunu
# getirir (Orin Nano Super). Mod numaraları sürüme göre değişir -> listeleyin:
#   sudo nvpmodel -q --verbose
# Genellikle en yüksek mod 0 veya 2'dir; aşağıdaki satırı kendi çıktınıza göre ayarlayın.
sudo nvpmodel -m 0 || true
# CPU/GPU/EMC saatlerini maksimuma sabitle (DVFS gecikmesini yok eder).
# Kalıcı değildir; systemd servisi aşağıda her açılışta tekrar çalıştırır.
sudo jetson_clocks || true

echo "== 2) Bellek: 8 GB CPU+GPU arasında PAYLAŞIMLIDIR"
# Masaüstü (GNOME) ~800 MB-1 GB RAM yer. Headless çalıştırmak modele yer açar.
# Geri almak için: sudo systemctl set-default graphical.target
if [[ "${HEADLESS:-1}" == "1" ]]; then
  sudo systemctl set-default multi-user.target
fi
# NVMe üzerinde swap: model yüklenirken ani tepe kullanımlarda OOM'u önler.
# (Model katmanları swap'ta ÇALIŞMAZ, çok yavaş olur; bu sadece emniyet payı.)
if ! swapon --show | grep -q /swapfile; then
  sudo fallocate -l 8G /swapfile && sudo chmod 600 /swapfile
  sudo mkswap /swapfile && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
fi
# zram (sıkıştırılmış RAM swap) çift swap yaratır; NVMe swap varsa kapatın.
sudo systemctl disable --now nvzramconfig 2>/dev/null || true

echo "== 3) Python ortamı"
sudo apt-get update && sudo apt-get install -y python3-venv python3-pip git cmake build-essential
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt

if [[ "$ENGINE" == "ollama" ]]; then
  echo "== 4a) Ollama"
  # Resmi betik Jetson'ı (JetPack 5/6) tanır ve CUDA'lı arm64 sürümünü kurar.
  curl -fsSL https://ollama.com/install.sh | sh
  # Hız ayarları (systemd override):
  #  KEEP_ALIVE=-1   : model bellekte kalsın; her soruda yeniden yükleme (10+ sn) olmasın
  #  FLASH_ATTENTION : bellek ve hız kazancı
  #  KV_CACHE_TYPE   : q8_0 KV-cache -> yarı bellek, kalite kaybı ihmal edilebilir
  #  NUM_PARALLEL=1  : tek kullanıcı/tek akış; bellek sabit kalır
  sudo mkdir -p /etc/systemd/system/ollama.service.d
  sudo tee /etc/systemd/system/ollama.service.d/override.conf >/dev/null <<'EOF'
[Service]
Environment="OLLAMA_KEEP_ALIVE=-1"
Environment="OLLAMA_FLASH_ATTENTION=1"
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
EOF
  sudo systemctl daemon-reload && sudo systemctl restart ollama
  ollama pull "$MODEL_OLLAMA"
  # Bağlam penceresini sabitleyen özel model (OpenAI /v1 ucu num_ctx almaz)
  sed "s|^FROM .*|FROM ${MODEL_OLLAMA}|" deploy/Modelfile > /tmp/Modelfile.cmms
  ollama create cmms-llm -f /tmp/Modelfile.cmms
  echo "LLM_BASE_URL=http://localhost:11434/v1" >> .env
  echo "LLM_MODEL=cmms-llm" >> .env

elif [[ "$ENGINE" == "llamacpp" ]]; then
  echo "== 4b) llama.cpp (CUDA, sm_87 = Orin)"
  export PATH=/usr/local/cuda/bin:$PATH
  [[ -d llama.cpp ]] || git clone --depth 1 https://github.com/ggml-org/llama.cpp
  # CMAKE_CUDA_ARCHITECTURES=87: Orin GPU mimarisi. Sadece bunu derlemek
  # derleme süresini dakikalarca kısaltır.
  cmake -S llama.cpp -B llama.cpp/build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=87 \
        -DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=OFF
  cmake --build llama.cpp/build --config Release -j"$(nproc)" --target llama-server llama-bench
  mkdir -p models
  [[ -f models/model.gguf ]] || curl -L -o models/model.gguf "$GGUF_URL"
  echo "LLM_BASE_URL=http://localhost:8080/v1" >> .env
  echo "LLM_MODEL=model" >> .env
  echo "Sunucuyu başlatmak için: bash deploy/run_llama_server.sh"
fi

echo "== 5) systemd servisleri"
sudo cp deploy/jetson-clocks.service /etc/systemd/system/
sudo systemctl enable jetson-clocks.service
echo "Agent API servisi için: deploy/cmms-agent.service dosyasındaki yolları düzenleyip kopyalayın."

echo "== Bitti. Kontrol: python -m cmms_agent doctor && python scripts/benchmark.py"
