"""CMMS Agent: yönlendir → veriyi çek/hesapla → anlat.

    Soru ──► Router (kural / LLM-JSON) ──► Tool (Elastic + analytics)
                                               │  küçük "facts" JSON
                                               ▼
                                         Narrator (LLM, stream) ──► Cevap

LLM'in iki görevi var ve ikisi de kısa:
  - Yönlendirme: sadece belirsiz sorularda, ~30 token JSON.
  - Anlatım: hesaplanmış gerçekleri Türkçe, eyleme dönük bir yoruma çevirme.
Hiçbir sayı LLM tarafından üretilmez; hepsi araçlardan gelir (halüsinasyon
riskini azaltmanın en etkili yolu).
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Iterator

from .cache import TTLCache
from .config import Settings
from .router import NONE_TOOL, Route, route
from .tools import TOOLS, ToolContext

log = logging.getLogger(__name__)

NARRATOR_SYSTEM = (
    "Sen deneyimli bir bakım güvenilirlik mühendisisin ve bir CMMS asistanısın. "
    "Görevin: sana verilen VERİ bölümündeki hesaplanmış gerçekleri kullanarak "
    "kullanıcının sorusunu Türkçe, kısa ve net cevaplamak.\n"
    "Kurallar:\n"
    "1. Sadece VERİ'deki sayıları kullan. Asla sayı uydurma, yuvarlarken anlamı değiştirme.\n"
    "2. Veri yetersizse bunu açıkça söyle.\n"
    "3. Önce 1-2 cümlelik doğrudan cevap, sonra en fazla 4 madde bulgu, "
    "sonra 1-3 somut bakım önerisi (ör. PM sıklığını artır, yedek parça hazırla, "
    "kök neden analizi yap) ver.\n"
    "4. Risk yüzdeleri istatistiksel tahmindir; kesinlik iddia etme.\n"
    "Terimler: MTBF=arızalar arası ortalama süre, MTTR=ortalama onarım süresi, "
    "Weibull beta>1 aşınma/yaşlanma, beta<1 erken dönem arızası, "
    "trend 'kötüleşiyor'=arızalar istatistiksel olarak sıklaşıyor, "
    "sensör z_score=son 24 saatin normalden kaç standart sapma uzak olduğu, "
    "trend_pct_per_day=sensör değerinin günlük yüzde değişimi. "
    "Sensör 'kritik' ise bunu cevabın başında vurgula."
)

GENERAL_SYSTEM = (
    "Sen deneyimli bir bakım güvenilirlik mühendisisin. Bakım yönetimi, CMMS, "
    "MTBF/MTTR, önleyici/kestirimci bakım konularındaki genel soruları Türkçe, "
    "kısa ve öğretici biçimde cevapla. Şirket verisi hakkında tahmin yürütme."
)


@dataclass
class AgentResult:
    question: str
    route: Route
    facts: dict
    answer: str = ""
    timings_ms: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "tool": self.route.tool,
            "args": self.route.args,
            "route_source": self.route.source,
            "facts": self.facts,
            "answer": self.answer,
            "timings_ms": self.timings_ms,
        }


class CMMSAgent:
    def __init__(self, settings: Settings, repo, llm):
        self.s = settings
        self.llm = llm
        self.ctx = ToolContext(repo=repo, default_days=settings.default_days)
        self.cache = TTLCache(ttl_s=settings.cache_ttl_s)

    # ------------------------------------------------------------------ #
    def plan(self, question: str) -> Route:
        return route(question, llm=self.llm, mode=self.s.router_mode)

    def run_tool(self, r: Route) -> dict:
        if r.tool == NONE_TOOL:
            return {}
        key = (r.tool, json.dumps(r.args, sort_keys=True, default=str))
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        try:
            facts = TOOLS[r.tool].fn(self.ctx, dict(r.args))
        except Exception as e:  # noqa: BLE001
            log.exception("Araç hatası: %s", r.tool)
            facts = {"error": f"{type(e).__name__}: {e}"}
        else:
            self.cache.set(key, facts)
        return facts

    def messages(self, question: str, r: Route, facts: dict) -> list[dict]:
        if r.tool == NONE_TOOL:
            return [{"role": "system", "content": GENERAL_SYSTEM},
                    {"role": "user", "content": question}]
        # Kompakt JSON (boşluksuz) = daha az token = daha hızlı prefill.
        data = json.dumps(facts, ensure_ascii=False, separators=(",", ":"), default=str)
        return [
            {"role": "system", "content": NARRATOR_SYSTEM},
            {"role": "user", "content":
                f"SORU: {question}\nARAÇ: {r.tool}\nVERİ: {data}"},
        ]

    # ------------------------------------------------------------------ #
    def stream(self, question: str) -> Iterator[str | AgentResult]:
        """Önce metin parçalarını (str) yield eder, en sonda AgentResult."""
        t0 = time.perf_counter()
        r = self.plan(question)
        t1 = time.perf_counter()
        facts = self.run_tool(r)
        t2 = time.perf_counter()
        parts: list[str] = []
        first_token_at = None
        for chunk in self.llm.stream(self.messages(question, r, facts)):
            if first_token_at is None:
                first_token_at = time.perf_counter()
            parts.append(chunk)
            yield chunk
        t3 = time.perf_counter()
        yield AgentResult(
            question=question, route=r, facts=facts, answer="".join(parts).strip(),
            timings_ms={
                "route": round((t1 - t0) * 1000),
                "tool": round((t2 - t1) * 1000),
                "first_token": round(((first_token_at or t3) - t2) * 1000),
                "generate": round((t3 - t2) * 1000),
                "total": round((t3 - t0) * 1000),
            },
        )

    def ask(self, question: str) -> AgentResult:
        result = None
        for item in self.stream(question):
            if isinstance(item, AgentResult):
                result = item
        assert result is not None
        return result
