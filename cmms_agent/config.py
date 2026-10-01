"""Tüm ayarlar tek yerde, ortam değişkenlerinden (.env) okunur.

Neden bu kadar çok ayar? Çünkü her firmanın CMMS'i Elastic'e farklı alan
isimleriyle yazar. Kodu değiştirmeden sadece .env'yi düzenleyerek kendi
index'inize uyarlayabilmeniz için alan adlarını da buraya aldık.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: str = ".env") -> None:
    """python-dotenv bağımlılığı eklememek için minimal .env okuyucu.

    Zaten tanımlı ortam değişkenlerini EZMEZ (systemd/docker önceliklidir).
    """
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if value[:1] in ('"', "'"):
            value = value[1:].split(value[0], 1)[0]     # tırnak içi aynen
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()   # satır sonu yorumu
        os.environ.setdefault(key.strip(), value)


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    return int(os.environ.get(key, default))


def _env_float(key: str, default: float) -> float:
    return float(os.environ.get(key, default))


def _env_bool(key: str, default: bool) -> bool:
    return os.environ.get(key, str(default)).lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class FieldMap:
    """İş emri (work order) index'indeki alan adları.

    Kendi mapping'inizi görmek için:
        GET cmms-workorders/_mapping
    `keyword` tipinde olması gereken alanlar: asset, asset_name, wo_type,
    status, priority, failure_code, location. (terms aggregation için)
    """
    wo_id: str = "wo_id"
    asset: str = "asset_id"
    asset_name: str = "asset_name"
    location: str = "location"
    wo_type: str = "type"                 # corrective / preventive / predictive
    status: str = "status"                # open / in_progress / completed ...
    priority: str = "priority"
    failure_code: str = "failure_code"
    description: str = "description"      # text alanı (tam metin arama)
    created_at: str = "created_at"        # arızanın bildirildiği an
    started_at: str = "started_at"
    completed_at: str = "completed_at"
    downtime_hours: str = "downtime_hours"
    labor_hours: str = "labor_hours"
    cost: str = "cost"

    # Değer eşleştirmeleri: sizin CMMS "Arıza"/"CM" diyorsa burayı değiştirin.
    corrective_values: tuple[str, ...] = ("corrective",)
    preventive_values: tuple[str, ...] = ("preventive",)
    open_status_values: tuple[str, ...] = ("open", "in_progress", "waiting_parts")
    completed_status_values: tuple[str, ...] = ("completed", "closed")


@dataclass(frozen=True)
class ReadingFieldMap:
    """Sensör / durum izleme index'i (opsiyonel). Her belge tek bir ölçüm:
    {asset_id, metric, value, @timestamp}. Geniş format (her metrik ayrı
    alan) kullanıyorsanız bir ingest pipeline veya transform ile uzun formata
    çevirmeniz gerekir (bkz. docs/06-sensor-verisi.md).
    """
    asset: str = "asset_id"
    metric: str = "metric"
    value: str = "value"
    timestamp: str = "@timestamp"


@dataclass(frozen=True)
class Settings:
    # --- Elasticsearch ---
    es_url: str = "http://localhost:9200"
    es_api_key: str = ""
    es_username: str = ""
    es_password: str = ""
    es_ca_certs: str = ""
    es_verify_certs: bool = True
    es_timeout: int = 15
    wo_index: str = "cmms-workorders"
    max_docs: int = 50_000                # tek analizde çekilecek en fazla belge
    readings_index: str = "cmms-readings" # sensör index'i (yoksa araç devre dışı)

    # --- LLM (OpenAI uyumlu uç: Ollama, llama.cpp llama-server, MLC, vLLM) ---
    llm_base_url: str = "http://localhost:11434/v1"
    llm_model: str = "qwen2.5:3b-instruct"
    llm_api_key: str = "local"            # yerel sunucular umursamaz
    llm_timeout: float = 120.0
    llm_temperature: float = 0.2
    llm_max_tokens: int = 400
    # "schema": response_format=json_schema (llama.cpp & Ollama >=0.5 destekler)
    # "json":   response_format=json_object (daha eski sunucular)
    # "none":   serbest metin, JSON'u biz ayıklarız
    llm_json_mode: str = "schema"

    # --- Agent davranışı ---
    router_mode: str = "hybrid"           # rules | llm | hybrid
    default_days: int = 180
    cache_ttl_s: int = 120
    language: str = "tr"

    fields: FieldMap = field(default_factory=FieldMap)
    reading_fields: ReadingFieldMap = field(default_factory=ReadingFieldMap)


def _csv(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(key)
    if not raw:
        return default
    return tuple(v.strip() for v in raw.split(",") if v.strip())


def load_settings(env_file: str = ".env") -> Settings:
    _load_dotenv(env_file)
    d = FieldMap()
    fm = FieldMap(
        wo_id=_env("CMMS_FIELD_WO_ID", d.wo_id),
        asset=_env("CMMS_FIELD_ASSET", d.asset),
        asset_name=_env("CMMS_FIELD_ASSET_NAME", d.asset_name),
        location=_env("CMMS_FIELD_LOCATION", d.location),
        wo_type=_env("CMMS_FIELD_TYPE", d.wo_type),
        status=_env("CMMS_FIELD_STATUS", d.status),
        priority=_env("CMMS_FIELD_PRIORITY", d.priority),
        failure_code=_env("CMMS_FIELD_FAILURE_CODE", d.failure_code),
        description=_env("CMMS_FIELD_DESCRIPTION", d.description),
        created_at=_env("CMMS_FIELD_CREATED_AT", d.created_at),
        started_at=_env("CMMS_FIELD_STARTED_AT", d.started_at),
        completed_at=_env("CMMS_FIELD_COMPLETED_AT", d.completed_at),
        downtime_hours=_env("CMMS_FIELD_DOWNTIME", d.downtime_hours),
        labor_hours=_env("CMMS_FIELD_LABOR", d.labor_hours),
        cost=_env("CMMS_FIELD_COST", d.cost),
        corrective_values=_csv("CMMS_CORRECTIVE_VALUES", d.corrective_values),
        preventive_values=_csv("CMMS_PREVENTIVE_VALUES", d.preventive_values),
        open_status_values=_csv("CMMS_OPEN_STATUS_VALUES", d.open_status_values),
        completed_status_values=_csv("CMMS_COMPLETED_STATUS_VALUES",
                                     d.completed_status_values),
    )
    rd = ReadingFieldMap()
    rfm = ReadingFieldMap(
        asset=_env("CMMS_RFIELD_ASSET", rd.asset),
        metric=_env("CMMS_RFIELD_METRIC", rd.metric),
        value=_env("CMMS_RFIELD_VALUE", rd.value),
        timestamp=_env("CMMS_RFIELD_TIMESTAMP", rd.timestamp),
    )
    s = Settings()
    return Settings(
        es_url=_env("ES_URL", s.es_url),
        es_api_key=_env("ES_API_KEY", s.es_api_key),
        es_username=_env("ES_USERNAME", s.es_username),
        es_password=_env("ES_PASSWORD", s.es_password),
        es_ca_certs=_env("ES_CA_CERTS", s.es_ca_certs),
        es_verify_certs=_env_bool("ES_VERIFY_CERTS", s.es_verify_certs),
        es_timeout=_env_int("ES_TIMEOUT", s.es_timeout),
        wo_index=_env("CMMS_WO_INDEX", s.wo_index),
        max_docs=_env_int("CMMS_MAX_DOCS", s.max_docs),
        readings_index=_env("CMMS_READINGS_INDEX", s.readings_index),
        llm_base_url=_env("LLM_BASE_URL", s.llm_base_url),
        llm_model=_env("LLM_MODEL", s.llm_model),
        llm_api_key=_env("LLM_API_KEY", s.llm_api_key),
        llm_timeout=_env_float("LLM_TIMEOUT", s.llm_timeout),
        llm_temperature=_env_float("LLM_TEMPERATURE", s.llm_temperature),
        llm_max_tokens=_env_int("LLM_MAX_TOKENS", s.llm_max_tokens),
        llm_json_mode=_env("LLM_JSON_MODE", s.llm_json_mode),
        router_mode=_env("ROUTER_MODE", s.router_mode),
        default_days=_env_int("DEFAULT_DAYS", s.default_days),
        cache_ttl_s=_env_int("CACHE_TTL_S", s.cache_ttl_s),
        language=_env("AGENT_LANGUAGE", s.language),
        fields=fm,
        reading_fields=rfm,
    )
