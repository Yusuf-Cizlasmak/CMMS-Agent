"""Agent uçtan uca: sahte OpenAI-uyumlu sunucu (httpx MockTransport) + sahte araç."""
import json

import httpx

from cmms_agent.agent import AgentResult, CMMSAgent
from cmms_agent.config import Settings
from cmms_agent.llm import LLMClient, extract_json, strip_think
from cmms_agent.tools import TOOLS


def fake_server(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    if body.get("stream"):
        chunks = ["<think>hmm</think>", "KMP-003 ", "yüksek ", "riskte."]
        sse = "".join(
            f"data: {json.dumps({'choices': [{'delta': {'content': c}}]})}\n\n" for c in chunks
        ) + "data: [DONE]\n\n"
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
    assert body["response_format"]["type"] == "json_schema"
    content = '```json\n{"tool":"kpi_summary","args":{}}\n```'
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def make_llm():
    llm = LLMClient(Settings())
    llm.http = httpx.Client(base_url="http://x/v1", transport=httpx.MockTransport(fake_server))
    return llm


def test_extract_json_variants():
    assert extract_json('{"a":1}') == {"a": 1}
    assert extract_json('Tabii: ```json\n{"a":{"b":2}}\n``` bitti') == {"a": {"b": 2}}
    assert extract_json("<think>{x}</think>{\"a\":3}") == {"a": 3}
    assert extract_json("yok") is None
    assert strip_think("<think>\nx\n</think> merhaba") == "merhaba"


def test_llm_json_and_stream():
    llm = make_llm()
    assert llm.chat_json([{"role": "user", "content": "?"}], {})["tool"] == "kpi_summary"
    assert "".join(llm.stream([])) == "KMP-003 yüksek riskte."


def test_agent_stream_uses_tool_and_cache(monkeypatch):
    calls = []

    def fake_tool(ctx, args):
        calls.append(args)
        return {"ranking": [{"asset_id": "KMP-003", "risk_pct": 89.5}]}

    monkeypatch.setattr(TOOLS["failure_risk_ranking"], "fn", fake_tool)
    agent = CMMSAgent(Settings(), repo=None, llm=make_llm())

    items = list(agent.stream("Önümüzdeki ay en riskli ekipmanlar?"))
    res = items[-1]
    assert isinstance(res, AgentResult)
    assert res.route.tool == "failure_risk_ranking" and res.route.args["horizon_days"] == 30
    assert res.answer == "KMP-003 yüksek riskte."
    assert set(res.timings_ms) >= {"route", "tool", "first_token", "total"}

    # Narrator prompt'u hesaplanmış veriyi içermeli
    msgs = agent.messages("q", res.route, res.facts)
    assert "89.5" in msgs[1]["content"]

    agent.ask("Önümüzdeki ay en riskli ekipmanlar?")
    assert len(calls) == 1            # ikinci sefer önbellekten


def test_tool_error_does_not_crash(monkeypatch):
    def boom(ctx, args):
        raise RuntimeError("es down")

    monkeypatch.setattr(TOOLS["open_backlog"], "fn", boom)
    agent = CMMSAgent(Settings(), repo=None, llm=make_llm())
    res = agent.ask("Açık iş emirleri?")
    assert "es down" in res.facts["error"]
