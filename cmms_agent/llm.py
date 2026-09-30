"""OpenAI-uyumlu yerel LLM istemcisi.

Jetson'da çalışan tüm popüler sunucular aynı API'yi konuşur:
  - Ollama            -> http://localhost:11434/v1
  - llama.cpp server  -> http://localhost:8080/v1
  - MLC LLM (serve)   -> http://localhost:8000/v1
Böylece motoru değiştirmek sadece LLM_BASE_URL'yi değiştirmektir.

Bilerek `openai` paketi yerine httpx kullanıyoruz: daha az bağımlılık ve
sunucuya özgü alanları (ör. json_schema) doğrudan kontrol ediyoruz.
"""
from __future__ import annotations

import json
import re
from typing import Iterator

import httpx

from .config import Settings

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_think(text: str) -> str:
    """Qwen3/DeepSeek-R1 gibi 'düşünen' modellerin <think> bloklarını at."""
    return _THINK_RE.sub("", text).strip()


def extract_json(text: str) -> dict | None:
    """Model JSON'u ```json ... ``` içine veya metnin ortasına koyarsa kurtar."""
    text = strip_think(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, depth = text.find("{"), 0
    if start < 0:
        return None
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


class LLMClient:
    def __init__(self, s: Settings):
        self.s = s
        self.http = httpx.Client(
            base_url=s.llm_base_url.rstrip("/"),
            timeout=httpx.Timeout(s.llm_timeout, connect=5.0),
            headers={"Authorization": f"Bearer {s.llm_api_key}"},
        )

    def _payload(self, messages: list[dict], **kw) -> dict:
        return {
            "model": self.s.llm_model,
            "messages": messages,
            "temperature": kw.get("temperature", self.s.llm_temperature),
            "max_tokens": kw.get("max_tokens", self.s.llm_max_tokens),
            "stream": kw.get("stream", False),
        }

    def chat(self, messages: list[dict], **kw) -> str:
        r = self.http.post("/chat/completions", json=self._payload(messages, **kw))
        r.raise_for_status()
        return strip_think(r.json()["choices"][0]["message"]["content"] or "")

    def chat_json(self, messages: list[dict], schema: dict, **kw) -> dict | None:
        """Yapılandırılmış çıktı. Sunucu destekliyorsa JSON şeması ile
        *kısıtlı üretim* (grammar-constrained decoding) yapılır: model şemaya
        uymayan token üretemez. Küçük modellerde güvenilirliği çok artırır.
        """
        payload = self._payload(messages, max_tokens=kw.get("max_tokens", 120),
                                temperature=0.0)
        mode = self.s.llm_json_mode
        if mode == "schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "route", "schema": schema},
            }
        elif mode == "json":
            payload["response_format"] = {"type": "json_object"}
        r = self.http.post("/chat/completions", json=payload)
        r.raise_for_status()
        return extract_json(r.json()["choices"][0]["message"]["content"] or "")

    def stream(self, messages: list[dict], **kw) -> Iterator[str]:
        """Token token akış (SSE). İlk kelime ~0.3-1 sn içinde ekranda olur;
        kullanıcı açısından 'hız' algısının en önemli kısmı budur."""
        payload = self._payload(messages, stream=True, **kw)
        in_think = False
        with self.http.stream("POST", "/chat/completions", json=payload) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    delta = json.loads(data)["choices"][0]["delta"].get("content") or ""
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
                # Basit <think> filtresi (akışta)
                if "<think>" in delta:
                    in_think = True
                if in_think:
                    if "</think>" in delta:
                        in_think = False
                        delta = delta.split("</think>", 1)[1]
                    else:
                        continue
                if delta:
                    yield delta

    def health(self) -> bool:
        try:
            return self.http.get("/models", timeout=3).status_code == 200
        except httpx.HTTPError:
            return False
