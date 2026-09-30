#!/usr/bin/env bash
# llama.cpp OpenAI-uyumlu sunucu, Jetson Orin Nano için ayarlı.
#  -ngl 99        : tüm katmanlar GPU'da (Orin'de bellek zaten paylaşımlı)
#  -c 4096        : bağlam penceresi (KV-cache boyutunu belirler)
#  --parallel 1   : tek slot -> tüm KV-cache tek konuşmaya; bellek öngörülebilir
#  -ctk/-ctv q8_0 : KV-cache'i 8-bit tut (yarı bellek). Flash attention gerekir.
#  --flash-attn   : yeni sürümlerde "on|off|auto" alır (varsayılan auto);
#                   eski sürümlerde değer almayan "-fa" bayrağıdır.
#  --no-mmap      : dosyayı sayfa önbelleğinde + GPU tamponunda iki kez tutma
#  --jinja        : modelin kendi chat şablonunu kullan
#  Prompt önbelleği (cache_prompt) varsayılan açıktır: sabit sistem prompt'u
#  her istekte yeniden hesaplanmaz.
set -euo pipefail
cd "$(dirname "$0")/.."
exec ./llama.cpp/build/bin/llama-server \
  -m "${MODEL_PATH:-models/model.gguf}" --alias model \
  -ngl 99 -c 4096 --parallel 1 \
  -ctk q8_0 -ctv q8_0 --flash-attn on \
  --no-mmap --jinja \
  --host "${HOST:-127.0.0.1}" --port "${PORT:-8080}"
