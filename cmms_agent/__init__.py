"""Jetson Orin Nano üzerinde yerel LLM ile çalışan CMMS analiz agent'ı."""
from .agent import AgentResult, CMMSAgent
from .config import Settings, load_settings

__all__ = ["CMMSAgent", "AgentResult", "Settings", "load_settings", "build_agent"]
__version__ = "0.1.0"


def build_agent(settings: Settings | None = None) -> CMMSAgent:
    from .es_client import CMMSRepository, build_client
    from .llm import LLMClient

    s = settings or load_settings()
    repo = CMMSRepository(build_client(s), s)
    return CMMSAgent(s, repo, LLMClient(s))
