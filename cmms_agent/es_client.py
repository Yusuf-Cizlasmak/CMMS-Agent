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


class CMMSRepository:
    """Agent araçlarının kullandığı tüm Elastic sorguları burada."""

    def __init__(self, es: Elasticsearch, settings: Settings):
        self.es = es
        self.s = settings
        self.f = settings.fields

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
    def aggregate(self, query: dict, aggs: dict) -> dict:
        """size=0: belge döndürme, sadece aggregation sonucu -> çok hızlı."""
        resp = self.es.search(
            index=self.s.wo_index, query=query, aggs=aggs, size=0,
            track_total_hits=True,
        )
        out = dict(resp.get("aggregations", {}))
        out["_total"] = resp["hits"]["total"]["value"]
        return out

    def search(self, query: dict, size: int = 10, source: list[str] | None = None,
               sort: list | None = None) -> list[dict]:
        resp = self.es.search(
            index=self.s.wo_index, query=query, size=size,
            source=source, sort=sort,
        )
        return [h["_source"] for h in resp["hits"]["hits"]]

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
                    "source": source,
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
                    yield h["_source"]
                fetched += len(hits)
                search_after = hits[-1]["sort"]
            if fetched >= self.s.max_docs:
                log.warning("max_docs (%s) sınırına ulaşıldı, veri kesildi", self.s.max_docs)
        finally:
            try:
                self.es.close_point_in_time(id=pit_id)
            except Exception:  # noqa: BLE001 - kapatma hatası kritik değil
                pass

    def ping(self) -> bool:
        try:
            return bool(self.es.ping())
        except Exception:  # noqa: BLE001
            return False
