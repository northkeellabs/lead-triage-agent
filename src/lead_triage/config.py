"""Settings from environment variables. Secrets are read here and nowhere else."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
USER_AGENT = "northkeel-lead-triage-agent/0.1 (+public-page research for inbound leads; max 3 pages)"


def _user_env(name: str) -> str:
    """Environment variable, falling back to the Windows user environment (registry)."""
    val = os.environ.get(name, "")
    if val or os.name != "nt":
        return val.strip()
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return str(winreg.QueryValueEx(k, name)[0]).strip()
    except OSError:
        return ""


def lacrm_api_key() -> str:
    return _user_env("LACRM_API_KEY")


def webhook_secret() -> str:
    return _user_env("TRIAGE_WEBHOOK_SECRET")


def known_secrets() -> list[str]:
    return [s for s in (lacrm_api_key(), webhook_secret(), os.environ.get("ANTHROPIC_API_KEY", ""),
                        os.environ.get("OPENAI_API_KEY", ""), os.environ.get("GOOGLE_API_KEY", "")) if s]


@dataclass
class Settings:
    model_provider: str = field(default_factory=lambda: os.environ.get("MODEL_PROVIDER", "ollama").lower())
    model_name: str = field(default_factory=lambda: os.environ.get("MODEL_NAME", "qwen3:8b"))
    ollama_base_url: str = field(default_factory=lambda: os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"))
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("TRIAGE_DATA_DIR", ROOT / "data")))
    icp_path: Path = field(default_factory=lambda: Path(os.environ.get("TRIAGE_ICP", ROOT / "icp.yaml")))
    lacrm_mcp_dir: str = field(default_factory=lambda: _user_env("LACRM_MCP_DIR"))
    lacrm_mcp_env: str = field(default_factory=lambda: _user_env("LACRM_MCP_ENV"))

    def ensure_dirs(self) -> None:
        (self.data_dir / "audit").mkdir(parents=True, exist_ok=True)

    @property
    def checkpoint_db(self) -> Path:
        return self.data_dir / "checkpoints.sqlite"


def load_icp(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)
