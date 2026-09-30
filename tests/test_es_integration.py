"""Gerçek Elasticsearch'e karşı entegrasyon testi.

Çalıştırmak için: docker compose up -d elasticsearch
                  python scripts/seed_sample_data.py --reset
                  pytest tests/test_es_integration.py
ES yoksa testler atlanır.
"""
import pytest

from cmms_agent.config import load_settings
from cmms_agent.es_client import CMMSRepository, build_client
from cmms_agent.tools import TOOLS, ToolContext

s = load_settings()
repo = CMMSRepository(build_client(s), s)
pytestmark = pytest.mark.skipif(
    not repo.ping() or not repo.es.indices.exists(index=s.wo_index),
    reason="Elasticsearch / örnek veri yok",
)
ctx = ToolContext(repo=repo, default_days=365)


@pytest.mark.parametrize("name,args", [
    ("kpi_summary", {}),
    ("top_failing_assets", {"top_n": 3}),
    ("asset_reliability", {"asset_id": "kompresör 3"}),
    ("failure_risk_ranking", {"horizon_days": 30}),
    ("workorder_trend", {"interval": "month"}),
    ("failure_modes", {}),
    ("search_workorders", {"text": "rulman"}),
    ("open_backlog", {}),
])
def test_tool_runs(name, args):
    out = TOOLS[name].fn(ctx, args)
    assert "error" not in out, out


def test_seeded_story_is_found():
    top = TOOLS["failure_risk_ranking"].fn(ctx, {"top_n": 3})
    assert "KMP-003" in [r["asset_id"] for r in top["ranking"]]
    rel = TOOLS["asset_reliability"].fn(ctx, {"asset_id": "KMP-003"})
    assert rel["asset_id"] == "KMP-003" and rel["failures"] >= 5
