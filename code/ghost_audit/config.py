"""Runtime configuration from environment variables (and an uncommitted .env file)."""

import os

from . import paths


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


def anthropic_api_key() -> str | None:
    load_env()
    return os.environ.get("ANTHROPIC_API_KEY") or None


def vision_model() -> str:
    load_env()
    return os.environ.get("GHOST_VISION_MODEL", "claude-sonnet-5-5")


def intent_model() -> str:
    load_env()
    return os.environ.get("GHOST_INTENT_MODEL", "claude-haiku-5-5")
