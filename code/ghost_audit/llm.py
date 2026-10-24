"""Thin Claude client wrapper that records every call's token usage.

The ledger (code/cache/llm_usage.jsonl) feeds evaluation/usage_report.md.
"""

import json
import time
from datetime import datetime, timezone

from . import config, paths

LEDGER_PATH = paths.REPO_ROOT / "code" / "cache" / "llm_usage.jsonl"


class LLMUnavailable(RuntimeError):
    pass


def call_claude(content: list[dict], *, purpose: str, model: str, max_tokens: int = 2000, system: str | None = None) -> str:
    """Send one user message (list of content blocks) and return the text reply."""
    api_key = config.anthropic_api_key()
    if not api_key:
        raise LLMUnavailable("ANTHROPIC_API_KEY is not set (add it to .env)")
    try:
        import anthropic
    except ImportError as exc:
        raise LLMUnavailable("the 'anthropic' package is not installed (pip install -r code/requirements.txt)") from exc

    client = anthropic.Anthropic(api_key=api_key)
    kwargs = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": content}]}
    if system:
        kwargs["system"] = system
    started = time.time()
    response = client.messages.create(**kwargs)
    text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
    _record(purpose, model, response.usage.input_tokens, response.usage.output_tokens, time.time() - started)
    return text


def _record(purpose: str, model: str, input_tokens: int, output_tokens: int, seconds: float) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "purpose": purpose,
        "provider": "anthropic",
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "seconds": round(seconds, 2),
    }
    with open(LEDGER_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def extract_json(text: str):
    """Parse the first JSON object in a model reply (tolerates code fences)."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in model reply")
    return json.loads(text[start : end + 1])
