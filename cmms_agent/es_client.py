"""Elasticsearch erişim katmanı.

Altın kural: **Hesabı Elastic'e yaptır, LLM'e yaptırma.**
- Sayım, toplam, ortalama, zaman serisi -> aggregation (sunucu tarafında, ms'ler)
- Olaylar arası süre gibi ham zaman damgası gereken hesaplar -> sadece gereken
  alanları (_source filtresi) PIT + search_after ile sayfa sayfa çek.
- LLM'e asla binlerce satır verme; ona sadece özet "gerçekleri" (facts) ver.
"""
from __future__ import annotations

import logging
from typing import Any, Iterator

from elasticsearch import Elasticsearch

from .config import Settings

log = logging.getLogger(__name__)


def build_client(s: Settings) -> Elasticsearch:
    kwargs: dict[str, Any] = {
        "request_timeout": s.es_timeout,
        "retry_on_timeout": True,
        "max_retries": 2,
    }
    if s.es_api_key:
        # Önerilen: sadece CMMS index'lerini OKUYABİLEN bir API key (docs/03).
        kwargs["api_key"] = s.es_api_key
    elif s.es_username:
        kwargs["basic_auth"] = (s.es_username, s.es_password)
    if s.es_url.startswith("https"):
        kwargs["verify_certs"] = s.es_verify_certs
        if s.es_ca_certs:
            kwargs["ca_certs"] = s.es_ca_certs
    return Elasticsearch(s.es_url, **kwargs)


def source_path(field: str) -> str:
    """Aggregation/filtre alanı -> _source'taki yol.

    `Ekipman.keyword` bir *multi-field*'dır: sadece index'te yaşar, _source'ta
    yoktur. Belgeyi okurken asıl alan adı (`Ekipman`) kullanılmalıdır.
    """
    return field[: -len(".keyword")] if field.endswith(".keyword") else field


def get_path(doc: dict, path: str) -> Any:
    """`Lokasyon.Hat` gibi noktalı yolu iç içe _source sözlüğünde bul."""
    if path in doc:                      # düz anahtar ("a.b" adıyla yazılmış olabilir)
        return doc[path]
    cur: Any = doc
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def project(doc: dict, fields: list[str]) -> dict:
    """_source'u, istenen (yapılandırılmış) alan adlarıyla düz bir sözlüğe çevir.
    Araç kodu böylece `doc.get(f.asset)` diyebilir; mapping ne olursa olsun."""
    return {f: get_path(doc, source_path(f)) for f in fields}


class CMMSRepository:
    """Agent araçlarının kullandığı tüm Elastic sorguları burada."""

    def __init__(self, es: Elasticsearch, settings: Settings):
        self.es = es
        self.s = settings
        self.f = settings.fields
        self._exists: dict[str, bool] = {}

    # ------------------------------------------------------------------ #
    # Sorgu yapı taşları
    # ------------------------------------------------------------------ #
    def time_filter(self, days: int) -> dict:
        return {"range": {self.f.created_at: {"gte": f"now-{days}d/d", "lte": "now"}}}

    def asset_filter(self, asset_id: str) -> dict:
        return {"term": {self.f.asset: asset_id}}

    def type_filter(self, values: tuple[str, ...]) -> dict:
        return {"terms": {self.f.wo_type: list(values)}}

    def bool_query(self, *filters: dict | None, must: list[dict] | None = None) -> dict:
        q: dict[str, Any] = {"filter": [f for f in filters if f]}
        if must:
            q["must"] = must
        return {"bool": q}

    # ------------------------------------------------------------------ #
    # Çalıştırıcılar
    # ------------------------------------------------------------------ #
    def aggregate(self, query: dict, aggs: dict, index: str | None = None) -> dict:
        """size=0: belge döndürme, sadece aggregation sonucu -> çok hızlı."""
        resp = self.es.search(
            index=index or self.s.wo_index, query=query, aggs=aggs, size=0,
            track_total_hits=True,
        )
        out = dict(resp.get("aggregations", {}))
        out["_total"] = resp["hits"]["total"]["value"]
        return out

    def search(self, query: dict, size: int = 10, source: list[str] | None = None,
               sort: list | None = None) -> list[dict]:
        resp = self.es.search(
            index=self.s.wo_index, query=query, size=size,
            source=[source_path(f) for f in source] if source else None, sort=sort,
        )
        hits = [h.get("_source", {}) for h in resp["hits"]["hits"]]
        return [project(h, source) for h in hits] if source else hits

    def iter_docs(self, query: dict, source: list[str], page_size: int = 2000
                  ) -> Iterator[dict]:
        """Point-in-time + search_after ile tutarlı ve derin sayfalama.

        scroll API'nin modern karşılığıdır. max_docs ile üst sınır koyuyoruz ki
        Jetson'ın 8 GB belleği yanlışlıkla dolmasın.
        """
        pit = self.es.open_point_in_time(index=self.s.wo_index, keep_alive="1m")
        pit_id = pit["id"]
        fetched = 0
        search_after = None
        try:
            while fetched < self.s.max_docs:
                kwargs: dict[str, Any] = {
                    "query": query,
                    "size": min(page_size, self.s.max_docs - fetched),
                    "source": [source_path(f) for f in source],
                    "pit": {"id": pit_id, "keep_alive": "1m"},
                    # _shard_doc: PIT ile gelen en ucuz, benzersiz tie-breaker
                    "sort": [{self.f.created_at: "asc"}, {"_shard_doc": "asc"}],
                }
                if search_after:
                    kwargs["search_after"] = search_after
                # PIT kullanılırken index parametresi VERİLMEZ.
                resp = self.es.search(**kwargs)
                hits = resp["hits"]["hits"]
                if not hits:
                    break
                pit_id = resp.get("pit_id", pit_id)
                for h in hits:
                    yield project(h.get("_source", {}), source)
                fetched += len(hits)
                search_after = hits[-1]["sort"]
            if fetched >= self.s.max_docs:
                log.warning("max_docs (%s) sınırına ulaşıldı, veri kesildi", self.s.max_docs)
        finally:
            try:
                self.es.close_point_in_time(id=pit_id)
            except Exception:  # noqa: BLE001 - kapatma hatası kritik değil
                pass

    def index_exists(self, index: str) -> bool:
        """Sonuç önbelleğe alınır: opsiyonel index'ler için her soruda
        ekstra bir istek atmayalım."""
        if index not in self._exists:
            try:
                self._exists[index] = bool(self.es.indices.exists(index=index))
            except Exception:  # noqa: BLE001 - yetki yoksa yok say
                self._exists[index] = False
        return self._exists[index]

    def ping(self) -> bool:
        try:
            return bool(self.es.ping())
        except Exception:  # noqa: BLE001
            return False
