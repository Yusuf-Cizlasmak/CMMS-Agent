"""Agent değerlendirmesi.

    python scripts/evaluate.py                      # sadece kural router (LLM'siz, anında)
    python scripts/evaluate.py --mode hybrid        # kural + LLM router
    python scripts/evaluate.py --mode llm           # sadece LLM router (modeli kıyaslamak için)
    python scripts/evaluate.py --full               # + tam cevap, sayı dayanaklılığı, süre

Model, prompt veya kural değiştirdiğinizde çalıştırın; sonuçlar
eval/report.json'a yazılır, önceki raporla karşılaştırabilirsiniz.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cmms_agent.config import load_settings  # noqa: E402
from cmms_agent.evaluation import load_cases, number_grounding, route_matches  # noqa: E402
from cmms_agent.router import route  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=str(ROOT / "eval" / "questions.jsonl"))
    ap.add_argument("--mode", choices=["rules", "hybrid", "llm"], default="rules")
    ap.add_argument("--full", action="store_true", help="tam agent cevabını da üret")
    ap.add_argument("--out", default=str(ROOT / "eval" / "report.json"))
    a = ap.parse_args()

    s = load_settings()
    cases = load_cases(a.cases)
    llm = agent = None
    if a.mode != "rules" or a.full:
        from cmms_agent import build_agent
        agent = build_agent(s)
        llm = agent.llm

    results, ok_count = [], 0
    for c in cases:
        r = route(c.q, llm=llm, mode=a.mode)
        ok, errs = route_matches(r.tool, r.args, c)
        ok_count += ok
        row = {"q": c.q, "expected": c.tool, "got": r.tool, "args": r.args,
               "source": r.source, "ok": ok, "errors": errs}
        if a.full and agent is not None and ok:
            res = agent.ask(c.q)
            row["answer"] = res.answer
            row["grounding"] = number_grounding(res.answer, res.facts, c.q)
            row["timings_ms"] = res.timings_ms
            row["facts"] = res.facts
        results.append(row)
        mark = "✓" if ok else "✗"
        print(f"{mark} [{r.source:8}] {c.q[:60]:<60} → {r.tool}"
              + ("" if ok else f"   ({'; '.join(errs)})"))

    acc = ok_count / len(cases)
    summary = {"mode": a.mode, "cases": len(cases), "route_accuracy": round(acc, 3)}
    full = [r for r in results if "grounding" in r]
    if full:
        summary["grounding_rate_avg"] = round(
            statistics.fmean(r["grounding"]["rate"] for r in full), 3)
        summary["answers_with_ungrounded_numbers"] = sum(
            1 for r in full if r["grounding"]["ungrounded"])
        summary["first_token_ms_p50"] = statistics.median(
            r["timings_ms"]["first_token"] for r in full)
        summary["total_ms_p50"] = statistics.median(r["timings_ms"]["total"] for r in full)

    print("\n" + json.dumps(summary, ensure_ascii=False, indent=1))
    Path(a.out).write_text(json.dumps({"summary": summary, "results": results},
                                      ensure_ascii=False, indent=1, default=str),
                           encoding="utf-8")
    print(f"Rapor: {a.out}")
    return 0 if acc >= 0.9 else 1


if __name__ == "__main__":
    raise SystemExit(main())
