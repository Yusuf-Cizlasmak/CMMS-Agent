"""HTTP servisi (FastAPI). Fabrikadaki diğer sistemler / web arayüzü buradan
bağlanır.

    uvicorn cmms_agent.api:app --host 0.0.0.0 --port 8088

    POST /ask         {"question": "..."}   -> tek seferde JSON cevap
    POST /ask/stream  {"question": "..."}   -> Server-Sent Events (token akışı)
    GET  /health

Not: Jetson'da tek bir LLM örneği vardır; aynı anda gelen istekler LLM
sunucusunda sıraya girer. `workers=1` kullanın (her worker ayrı bellek demek).
"""
from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import build_agent
from .agent import AgentResult

app = FastAPI(title="CMMS Agent", version="0.1.0")
_agent = None


def agent():
    global _agent
    if _agent is None:
        _agent = build_agent()
    return _agent


class AskRequest(BaseModel):
    question: str


@app.get("/health")
def health() -> dict:
    a = agent()
    return {"elasticsearch": a.ctx.repo.ping(), "llm": a.llm.health()}


@app.post("/ask")
def ask(req: AskRequest) -> dict:
    return agent().ask(req.question).to_dict()


@app.post("/ask/stream")
def ask_stream(req: AskRequest) -> StreamingResponse:
    def gen():
        for item in agent().stream(req.question):
            if isinstance(item, AgentResult):
                d = item.to_dict()
                d.pop("answer")
                yield f"event: done\ndata: {json.dumps(d, ensure_ascii=False, default=str)}\n\n"
            else:
                yield f"data: {json.dumps({'token': item}, ensure_ascii=False)}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")
