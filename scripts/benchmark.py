"""Yerel LLM hız ölçümü: ilk token süresi (TTFT) ve token/saniye.

Jetson'da model/quantization/ayar seçerken tahmin değil ÖLÇÜM kullanın:
    python scripts/benchmark.py                       # .env'deki model
    python scripts/benchmark.py --model qwen2.5:3b-instruct --runs 5
    python scripts/benchmark.py --base-url http://localhost:8080/v1 --model x
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cmms_agent.agent import NARRATOR_SYSTEM  # noqa: E402
from cmms_agent.config import load_settings  # noqa: E402

# Gerçekçi bir anlatım isteği (aracın ürettiği tipte ~600 byte facts)
FACTS = {"period_days": 365, "horizon_days": 30, "assets_evaluated": 8, "ranking": [
    {"asset_id": "KMP-003", "failures": 18, "mtbf_days": 18.6, "days_since_last_failure": 3.8,
     "risk_pct": 89.5, "risk_level": "yüksek", "trend": "kötüleşme_eğilimi", "weibull_beta": 1.73},
    {"asset_id": "PMP-012", "failures": 11, "mtbf_days": 21.5, "days_since_last_failure": 20.2,
     "risk_pct": 75.7, "risk_level": "yüksek", "trend": "kötüleşiyor", "weibull_beta": 1.01},
    {"asset_id": "CNC-007", "failures": 12, "mtbf_days": 30.5, "days_since_last_failure": 9.5,
     "risk_pct": 58.1, "risk_level": "orta", "trend": "stabil", "weibull_beta": 1.4}]}


def one_run(client: httpx.Client, model: str, max_tokens: int) -> dict:
    msgs = [{"role": "system", "content": NARRATOR_SYSTEM},
            {"role": "user", "content": "SORU: Önümüzdeki ay en riskli ekipmanlar?\n"
             f"VERİ: {json.dumps(FACTS, ensure_ascii=False, separators=(',', ':'))}"}]
    t0 = time.perf_counter()
    first, n = None, 0
    with client.stream("POST", "/chat/completions", json={
        "model": model, "messages": msgs, "stream": True,
        "max_tokens": max_tokens, "temperature": 0.2,
    }) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line.startswith("data:") or line.strip() == "data: [DONE]":
                continue
            try:
                delta = json.loads(line[5:])["choices"][0]["delta"].get("content")
            except (KeyError, IndexError, json.JSONDecodeError):
                continue
            if delta:
                first = first or time.perf_counter()
                n += 1          # OpenAI-uyumlu sunucular ~1 token/parça gönderir
    t1 = time.perf_counter()
    gen = (t1 - first) if first else 0
    return {"ttft_ms": ((first or t1) - t0) * 1000, "tokens": n,
            "tok_s": (n - 1) / gen if gen > 0 and n > 1 else 0.0, "total_s": t1 - t0}


def main() -> None:
    s = load_settings()
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=s.llm_base_url)
    ap.add_argument("--model", default=s.llm_model)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--max-tokens", type=int, default=256)
    a = ap.parse_args()

    client = httpx.Client(base_url=a.base_url.rstrip("/"), timeout=300)
    print(f"Model: {a.model} @ {a.base_url}")
    print("Isınma (model belleğe yükleniyor)...")
    one_run(client, a.model, 8)
    res = []
    for i in range(a.runs):
        r = one_run(client, a.model, a.max_tokens)
        res.append(r)
        print(f"  #{i + 1}: TTFT {r['ttft_ms']:.0f} ms | {r['tokens']} token | "
              f"{r['tok_s']:.1f} tok/s | toplam {r['total_s']:.1f} s")
    print(f"ORTALAMA: TTFT {statistics.fmean(x['ttft_ms'] for x in res):.0f} ms, "
          f"{statistics.fmean(x['tok_s'] for x in res):.1f} tok/s")


if __name__ == "__main__":
    main()
