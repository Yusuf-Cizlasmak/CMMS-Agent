"""Agent'ı ölçmek için yardımcılar.

İki ölçüm:
1. Yönlendirme doğruluğu: soru → doğru araç + doğru argümanlar mı?
2. Sayı dayanaklılığı (grounding): cevaptaki her sayı araçtan gelen
   verilerde (facts) veya soruda geçiyor mu? Geçmiyorsa LLM uydurmuş
   olabilir. Küçük modellerde en kritik kalite göstergesi budur.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

_NUM_RE = re.compile(r"(?<![\w.])[-+]?\d+(?:[.,]\d+)?")
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:T[\d:.]+Z?)?\b")
_ID_RE = re.compile(r"\b[A-Za-zÇĞİÖŞÜçğıöşü]{2,6}-\d{1,6}\b")
_LIST_MARKER_RE = re.compile(r"^\s*\d+[.)]\s", re.MULTILINE)


@dataclass
class Case:
    q: str
    tool: str
    args: dict = field(default_factory=dict)


def load_cases(path: str | Path) -> list[Case]:
    cases = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            d = json.loads(line)
            cases.append(Case(d["q"], d["tool"], d.get("args", {})))
    return cases


def route_matches(tool: str, args: dict, case: Case) -> tuple[bool, list[str]]:
    """Araç aynı mı ve beklenen her argüman eşleşiyor mu? (fazlası serbest)"""
    errors = []
    if tool != case.tool:
        errors.append(f"araç {tool} != {case.tool}")
    for k, v in case.args.items():
        got = args.get(k)
        if isinstance(v, str) and isinstance(got, str):
            ok = v.lower() in got.lower()
        else:
            ok = got == v
        if not ok:
            errors.append(f"{k}={got!r} (beklenen {v!r})")
    return not errors, errors


def _numbers(text: str) -> list[float]:
    text = _DATE_RE.sub(" ", text)
    text = _ID_RE.sub(" ", text)
    text = _LIST_MARKER_RE.sub(" ", text)
    out = []
    for m in _NUM_RE.findall(text):
        try:
            out.append(float(m.replace(",", ".")))
        except ValueError:
            pass
    return out


def _flatten_numbers(obj) -> list[float]:
    if isinstance(obj, bool):
        return []
    if isinstance(obj, (int, float)):
        return [float(obj)]
    if isinstance(obj, dict):
        return [x for v in obj.values() for x in _flatten_numbers(v)]
    if isinstance(obj, (list, tuple)):
        return [x for v in obj for x in _flatten_numbers(v)]
    if isinstance(obj, str):
        return _numbers(obj)
    return []


def _grounded(x: float, sources: list[float]) -> bool:
    for s in sources:
        # Yuvarlamaya izin ver: 89.48 -> "89", "89.5", "%89" hepsi geçerli.
        if abs(x - s) <= 0.51 or (abs(s) > 20 and abs(x - s) / abs(s) <= 0.01):
            return True
        # Oran <-> yüzde dönüşümü (0.83 -> %83)
        if abs(x - s * 100) <= 0.51:
            return True
    return False


def number_grounding(answer: str, facts: dict, question: str = "") -> dict:
    sources = _flatten_numbers(facts) + _numbers(question)
    nums = _numbers(answer)
    bad = [x for x in nums if not _grounded(x, sources)]
    return {
        "numbers": len(nums),
        "ungrounded": bad,
        "rate": 1.0 if not nums else round(1 - len(bad) / len(nums), 3),
    }
