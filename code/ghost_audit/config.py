"""Runtime configuration from environment variables (and an uncommitted .env file)."""

import os

from . import paths

PROVIDERS = ("groq", "anthropic")
DEFAULT_PROVIDER = "groq"

# Default model per provider and task. Override with GHOST_VISION_MODEL / GHOST_TEXT_MODEL.
DEFAULT_MODELS = {
    "groq": {"vision": "qwen/qwen3.8-27b", "text": "openai/gpt-oss-20b"},
    "anthropic": {"vision": "claude-sonnet-5-5", "text": "claude-haiku-5-5"},
}
API_KEY_VARS = {"groq": "GROQ_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
MODEL_VARS = {"vision": "GHOST_VISION_MODEL", "text": "GHOST_TEXT_MODEL"}


def load_env() -> None:
    """Populate os.environ from .env files without overriding variables already set."""
    for candidate in (paths.REPO_ROOT / ".env", paths.REPO_ROOT / "code" / ".env"):
        if not candidate.exists():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def llm_provider() -> str:
    load_env()
    provider = os.environ.get("GHOST_LLM_PROVIDER", DEFAULT_PROVIDER).strip().lower()
    if provider not in PROVIDERS:
        raise ValueError(f"GHOST_LLM_PROVIDER must be one of {PROVIDERS}, got {provider!r}")
    return provider


def api_key(provider: str) -> str | None:
    load_env()
    return os.environ.get(API_KEY_VARS[provider]) or None


def model_for(provider: str, task: str) -> str:
    """Model id for a task ('vision' or 'text'), honouring the GHOST_*_MODEL overrides."""
    load_env()
    return os.environ.get(MODEL_VARS[task]) or DEFAULT_MODELS[provider][task]
