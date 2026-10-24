"""Provider-pluggable LLM client (Groq by default, Anthropic optional).

Callers build provider-neutral content blocks with text_block() / image_block()
and call call_llm(); the provider is chosen by GHOST_LLM_PROVIDER. Every call's
token usage is appended to code/cache/llm_usage.jsonl for evaluation/usage_report.md.
"""

import base64
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from . import config, paths

LEDGER_PATH = paths.REPO_ROOT / "code" / "cache" / "llm_usage.jsonl"
MAX_ATTEMPTS = 6
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


class LLMUnavailable(RuntimeError):
    """No usable provider: missing API key or SDK."""


@dataclass
class Reply:
    text: str
    input_tokens: int
    output_tokens: int


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def image_block(data: bytes, media_type: str = "image/png") -> dict:
    return {"type": "image", "media_type": media_type, "data": base64.standard_b64encode(data).decode("ascii")}


# --- provider adapters ----------------------------------------------------------


def groq_messages(content: list[dict], system: str | None) -> list[dict]:
    """Neutral blocks -> OpenAI-style chat messages (images as base64 data URLs)."""
    parts = []
    for block in content:
        if block["type"] == "text":
            parts.append({"type": "text", "text": block["text"]})
        else:
            parts.append({"type": "image_url", "image_url": {"url": f"data:{block['media_type']};base64,{block['data']}"}})
    messages = [{"role": "system", "content": system}] if system else []
    messages.append({"role": "user", "content": parts})
    return messages


def groq_reasoning_options(model: str) -> dict:
    """Keep reasoning text out of the answer so replies stay parseable JSON."""
    if model.startswith("openai/gpt-oss"):
        return {"include_reasoning": False, "reasoning_effort": "low"}
    if model.startswith("qwen/"):
        return {"reasoning_format": "hidden", "reasoning_effort": "none"}
    return {}


def _call_groq(content: list[dict], model: str, max_tokens: int, json_mode: bool, system: str | None) -> Reply:
    key = config.api_key("groq")
    if not key:
        raise LLMUnavailable("GROQ_API_KEY is not set (add it to .env)")
    try:
        from groq import Groq
    except ImportError as exc:
        raise LLMUnavailable("the 'groq' package is not installed (pip install -r code/requirements.txt)") from exc
    kwargs = {
        "model": model,
        "messages": groq_messages(content, system),
        "max_completion_tokens": max_tokens,
        "temperature": 0,
        **groq_reasoning_options(model),
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    response = Groq(api_key=key, max_retries=0).chat.completions.create(**kwargs)
    usage = response.usage
    return Reply(response.choices[0].message.content or "", usage.prompt_tokens, usage.completion_tokens)


def anthropic_content(content: list[dict]) -> list[dict]:
    out = []
    for block in content:
        if block["type"] == "text":
            out.append({"type": "text", "text": block["text"]})
        else:
            out.append({"type": "image", "source": {"type": "base64", "media_type": block["media_type"], "data": block["data"]}})
    return out


def _call_anthropic(content: list[dict], model: str, max_tokens: int, json_mode: bool, system: str | None) -> Reply:
    key = config.api_key("anthropic")
    if not key:
        raise LLMUnavailable("ANTHROPIC_API_KEY is not set (add it to .env)")
    try:
        import anthropic
    except ImportError as exc:
        raise LLMUnavailable("the 'anthropic' package is not installed (pip install anthropic)") from exc
    kwargs = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": anthropic_content(content)}]}
    if system:
        kwargs["system"] = system
    response = anthropic.Anthropic(api_key=key, max_retries=0).messages.create(**kwargs)
    text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
    return Reply(text, response.usage.input_tokens, response.usage.output_tokens)


ADAPTERS = {"groq": _call_groq, "anthropic": _call_anthropic}


# --- public entry point ---------------------------------------------------------


def retry_delay(exc: Exception, attempt: int) -> float | None:
    """Seconds to wait before retrying, or None if the error is not retryable."""
    status = getattr(exc, "status_code", None)
    connection_error = type(exc).__name__ in ("APIConnectionError", "APITimeoutError")
    if status not in RETRYABLE_STATUS and not connection_error:
        return None
    response = getattr(exc, "response", None)
    header = response.headers.get("retry-after") if response is not None and hasattr(response, "headers") else None
    try:
        return max(float(header), 1.0) if header else min(2.0 ** attempt, 60.0)
    except ValueError:
        return min(2.0 ** attempt, 60.0)


def call_llm(
    content: list[dict],
    *,
    purpose: str,
    task: str,
    max_tokens: int = 2000,
    json_mode: bool = False,
    system: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    sleep=time.sleep,
) -> str:
    """Send one user message and return the reply text. task is 'vision' or 'text'."""
    provider = provider or config.llm_provider()
    model = model or config.model_for(provider, task)
    adapter = ADAPTERS[provider]
    started = time.time()
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            reply = adapter(content, model, max_tokens, json_mode, system)
            break
        except LLMUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - provider SDK errors differ; classify by status
            delay = retry_delay(exc, attempt)
            if delay is None or attempt == MAX_ATTEMPTS:
                raise
            print(f"  {provider} {type(exc).__name__}; retrying in {delay:.0f}s (attempt {attempt}/{MAX_ATTEMPTS})")
            sleep(delay)
    _record(purpose, provider, model, reply, time.time() - started)
    return reply.text


def _record(purpose: str, provider: str, model: str, reply: Reply, seconds: float) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "purpose": purpose,
        "provider": provider,
        "model": model,
        "input_tokens": reply.input_tokens,
        "output_tokens": reply.output_tokens,
        "seconds": round(seconds, 2),
    }
    with open(LEDGER_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def extract_json(text: str):
    """Parse the first JSON object in a model reply (tolerates code fences and stray reasoning)."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in model reply")
    return json.loads(text[start : end + 1])
