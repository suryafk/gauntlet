"""Settings read from environment variables (and an optional .env file)."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _agent_ids() -> dict:
    """Ids written by scripts/setup_bolna.py; env vars take precedence."""
    path = ROOT / "agents" / "agent_ids.json"
    return json.loads(path.read_text()) if path.exists() else {}


def pick_llm(settings: "Settings") -> str | None:
    """The LLM provider to use for judging and patching, or None for the offline fallbacks."""
    mode = settings.judge_mode
    if mode in ("gemini", "claude"):
        return mode
    if mode != "auto":
        return None
    key = {"gemini": settings.gemini_api_key, "claude": settings.anthropic_api_key}.get(settings.llm_provider)
    return settings.llm_provider if key else None


@dataclass
class Settings:
    bolna_mode: str = field(default_factory=lambda: _env("BOLNA_MODE", "mock"))
    bolna_api_key: str = field(default_factory=lambda: _env("BOLNA_API_KEY"))
    bolna_api_base: str = field(default_factory=lambda: _env("BOLNA_API_BASE", "https://api.bolna.ai"))
    bolna_caller_agent_id: str = field(
        default_factory=lambda: _env("BOLNA_CALLER_AGENT_ID") or _agent_ids().get("caller", ""))
    bolna_target_agent_id: str = field(
        default_factory=lambda: _env("BOLNA_TARGET_AGENT_ID") or _agent_ids().get("target", ""))
    # persona id -> caller agent id, when setup_bolna.py --per-persona was used
    persona_caller_agent_ids: dict = field(default_factory=lambda: _agent_ids().get("personas", {}))
    # Public HTTPS base URL Bolna can reach (ngrok or the host), used for webhooks and the target's tool.
    public_base_url: str = field(default_factory=lambda: _env("PUBLIC_BASE_URL", "http://localhost:8000"))
    # After a caller call completes, how long to wait for the target side (tool payload, latency).
    link_timeout_s: float = field(default_factory=lambda: float(_env("LINK_TIMEOUT_S", "45")))
    bolna_from_number: str = field(default_factory=lambda: _env("BOLNA_FROM_NUMBER"))
    # Which LLM grades calls and drafts patches: gemini (default) or claude.
    llm_provider: str = field(default_factory=lambda: _env("LLM_PROVIDER", "gemini"))
    gemini_api_key: str = field(default_factory=lambda: _env("GEMINI_API_KEY") or _env("GOOGLE_API_KEY"))
    gemini_model: str = field(default_factory=lambda: _env("GEMINI_MODEL", "gemini-3.8-flash"))
    # Sim mode makes ~10-14 requests per call, so it defaults to the high-quota Flash-Lite model.
    gemini_sim_model: str = field(default_factory=lambda: _env("GEMINI_SIM_MODEL", "gemini-3.5-flash-lite"))
    gemini_patch_model: str = field(default_factory=lambda: _env("GEMINI_PATCH_MODEL", "gemini-4-argon"))
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    # auto = the LLM_PROVIDER model when its key is set, else the offline heuristic judge.
    # heuristic = always offline; gemini / claude = force that provider.
    judge_mode: str = field(default_factory=lambda: _env("JUDGE_MODE", "auto"))
    judge_model: str = field(default_factory=lambda: _env("JUDGE_MODEL", "claude-opus-5-5"))  # Claude model
    judge_concurrency: int = field(default_factory=lambda: int(_env("JUDGE_CONCURRENCY", "4")))
    target_number: str = field(default_factory=lambda: _env("TARGET_NUMBER", "+910000000000"))
    run_call_cap: int = field(default_factory=lambda: int(_env("RUN_CALL_CAP", "50")))
    max_concurrency: int = field(default_factory=lambda: int(_env("MAX_CONCURRENCY", "10")))
    call_timeout_s: float = field(default_factory=lambda: float(_env("CALL_TIMEOUT_S", "300")))
    webhook_secret: str = field(default_factory=lambda: _env("WEBHOOK_SECRET"))
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", str(ROOT / "gauntlet.db")))
    personas_dir: Path = field(default_factory=lambda: Path(_env("PERSONAS_DIR", str(ROOT / "personas"))))
    # Target prompt versions (v1 is hand-written and flawed; patches add v2, v3, ...)
    target_prompts_dir: Path = field(
        default_factory=lambda: Path(_env("TARGET_PROMPTS_DIR", str(ROOT / "agents" / "target_prompts"))))
    # Example run loaded into an empty database at startup (see scripts/seed_run.py).
    seed_path: Path = field(default_factory=lambda: Path(_env("SEED_PATH", str(ROOT / "seed" / "example_run.json"))))
    seed_on_empty: bool = field(default_factory=lambda: _env("SEED_ON_EMPTY", "1") == "1")
    mock_call_min_s: float = field(default_factory=lambda: float(_env("MOCK_CALL_MIN_S", "1.0")))
    mock_call_max_s: float = field(default_factory=lambda: float(_env("MOCK_CALL_MAX_S", "4.0")))
    mock_dial_fail_rate: float = field(default_factory=lambda: float(_env("MOCK_DIAL_FAIL_RATE", "0.05")))
    # Set for repeatable demos: the same persona/prompt/call number always gets the same outcome.
    mock_seed: str = field(default_factory=lambda: _env("MOCK_SEED"))
