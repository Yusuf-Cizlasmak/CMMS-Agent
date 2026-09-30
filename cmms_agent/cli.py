"""Komut satırı arayüzü.

    python -m cmms_agent doctor                  # bağlantı + mapping kontrolü
    python -m cmms_agent ask "En riskli 5 ekipman?" --debug
    python -m cmms_agent chat                    # etkileşimli sohbet
    python -m cmms_agent tool failure_risk_ranking --args '{"days":365}'   # LLM'siz
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from .agent import AgentResult
from .config import load_settings


def _print_stream(agent, question: str, debug: bool) -> None:
    for item in agent.stream(question):
        if isinstance(item, AgentResult):
            print()
            t = item.timings_ms
            print(f"\n[{item.route.tool} · {item.route.source}] "
                  f"ilk token {t['first_token']} ms · toplam {t['total']} ms",
                  file=sys.stderr)
            if debug:
                print(json.dumps(item.to_dict()["args"], ensure_ascii=False), file=sys.stderr)
                print(json.dumps(item.facts, ensure_ascii=False, indent=1, default=str),
                      file=sys.stderr)
        else:
            print(item, end="", flush=True)


def cmd_doctor(s) -> int:
    from .es_client import CMMSRepository, build_client
    from .llm import LLMClient

    ok = True
    repo = CMMSRepository(build_client(s), s)
    print(f"Elasticsearch  {s.es_url} ... ", end="")
    if repo.ping():
        info = repo.es.info()
        print(f"OK (sürüm {info['version']['number']})")
        try:
            mapping = repo.es.indices.get_mapping(index=s.wo_index)
            props = next(iter(mapping.values()))["mappings"].get("properties", {})
            print(f"Index          {s.wo_index} ... OK, "
                  f"{repo.es.count(index=s.wo_index)['count']} belge")
            f = s.fields
            need_kw = [f.asset, f.wo_type, f.status, f.priority, f.failure_code]
            need_date = [f.created_at]
            for name in need_kw:
                typ = props.get(name, {}).get("type")
                flag = "OK" if typ == "keyword" else "⚠ keyword olmalı (terms agg için)"
                ok &= typ == "keyword"
                print(f"  alan {name:<16} {str(typ):<10} {flag}")
            for name in need_date:
                typ = props.get(name, {}).get("type")
                ok &= typ == "date"
                print(f"  alan {name:<16} {str(typ):<10} "
                      f"{'OK' if typ == 'date' else '⚠ date olmalı'}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"Index          {s.wo_index} ... HATA: {e}")
    else:
        ok = False
        print("ULAŞILAMIYOR")
    llm = LLMClient(s)
    print(f"LLM            {s.llm_base_url} ({s.llm_model}) ... ", end="")
    if llm.health():
        print("OK")
    else:
        ok = False
        print("ULAŞILAMIYOR (ollama serve / llama-server çalışıyor mu?)")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cmms_agent")
    p.add_argument("--env", default=".env", help=".env dosya yolu")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    a = sub.add_parser("ask")
    a.add_argument("question")
    a.add_argument("--debug", action="store_true")
    c = sub.add_parser("chat")
    c.add_argument("--debug", action="store_true")
    t = sub.add_parser("tool")
    t.add_argument("name")
    t.add_argument("--args", default="{}")
    r = sub.add_parser("route", help="Sadece yönlendirme sonucunu göster")
    r.add_argument("question")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    s = load_settings(args.env)

    if args.cmd == "doctor":
        return cmd_doctor(s)

    from . import build_agent
    agent = build_agent(s)

    if args.cmd == "route":
        r = agent.plan(args.question)
        print(json.dumps({"tool": r.tool, "args": r.args, "source": r.source},
                         ensure_ascii=False))
    elif args.cmd == "tool":
        from .router import Route
        facts = agent.run_tool(Route(args.name, json.loads(args.args), "manual"))
        print(json.dumps(facts, ensure_ascii=False, indent=2, default=str))
    elif args.cmd == "ask":
        _print_stream(agent, args.question, args.debug)
    elif args.cmd == "chat":
        print("CMMS Agent hazır. Çıkmak için 'q'.")
        while True:
            try:
                q = input("\nSoru> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if q.lower() in ("q", "quit", "exit", "çık"):
                break
            if q:
                _print_stream(agent, q, args.debug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
