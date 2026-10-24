"""Offline tests for the provider-pluggable LLM layer (no network, no API keys).

    python -m unittest discover -s code/tests
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ghost_audit import config, llm  # noqa: E402
from ghost_audit.dataset import load_dataset  # noqa: E402
from ghost_audit.evidence import images  # noqa: E402


class FakeResponse:
    def __init__(self, headers):
        self.headers = headers


class FakeStatusError(Exception):
    def __init__(self, status, retry_after=None):
        super().__init__(f"status {status}")
        self.status_code = status
        self.response = FakeResponse({"retry-after": retry_after} if retry_after else {})


class APIConnectionError(Exception):
    """Same class name as the SDKs' connection error."""


class ConversionTests(unittest.TestCase):
    def setUp(self):
        self.content = [llm.image_block(b"\x89PNG", "image/png"), llm.text_block("Return JSON.")]

    def test_groq_messages_use_data_urls(self):
        messages = llm.groq_messages(self.content, "be precise")
        self.assertEqual(messages[0], {"role": "system", "content": "be precise"})
        parts = messages[1]["content"]
        self.assertTrue(parts[0]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(parts[1], {"type": "text", "text": "Return JSON."})

    def test_anthropic_content_uses_base64_source(self):
        blocks = llm.anthropic_content(self.content)
        self.assertEqual(blocks[0]["source"]["type"], "base64")
        self.assertEqual(blocks[0]["source"]["media_type"], "image/png")

    def test_reasoning_kept_out_of_json_answers(self):
        self.assertEqual(llm.groq_reasoning_options("qwen/qwen3.8-27b"), {"reasoning_format": "hidden", "reasoning_effort": "none"})
        self.assertEqual(llm.groq_reasoning_options("openai/gpt-oss-20b")["include_reasoning"], False)
        self.assertEqual(llm.groq_reasoning_options("some/other-model"), {})


class RetryTests(unittest.TestCase):
    def test_retry_delay(self):
        self.assertEqual(llm.retry_delay(FakeStatusError(429, "7"), 1), 7.0)
        self.assertEqual(llm.retry_delay(FakeStatusError(503), 3), 8.0)
        self.assertIsNone(llm.retry_delay(FakeStatusError(400), 1))
        self.assertEqual(llm.retry_delay(APIConnectionError("down"), 2), 4.0)

    def test_call_llm_retries_rate_limits_and_records_usage(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(directory, ignore_errors=True))
        calls, sleeps = [], []

        def flaky(content, model, max_tokens, json_mode, system):
            calls.append((model, json_mode))
            if len(calls) == 1:
                raise FakeStatusError(429, "3")
            return llm.Reply('{"ok": true}', 120, 8)

        ledger = Path(directory) / "usage.jsonl"
        with mock.patch.dict(llm.ADAPTERS, {"groq": flaky}), mock.patch.object(llm, "LEDGER_PATH", ledger):
            text = llm.call_llm([llm.text_block("hi")], purpose="test", task="text", provider="groq", model="m", json_mode=True, sleep=sleeps.append)
        self.assertEqual(text, '{"ok": true}')
        self.assertEqual(sleeps, [3.0])
        self.assertEqual(calls, [("m", True), ("m", True)])
        entry = json.loads(ledger.read_text(encoding="utf-8").strip())
        self.assertEqual((entry["provider"], entry["model"], entry["input_tokens"], entry["output_tokens"]), ("groq", "m", 120, 8))

    def test_non_retryable_errors_propagate(self):
        def broken(*_):
            raise FakeStatusError(400)

        with mock.patch.dict(llm.ADAPTERS, {"groq": broken}):
            with self.assertRaises(FakeStatusError):
                llm.call_llm([llm.text_block("hi")], purpose="test", task="text", provider="groq", model="m", sleep=lambda _: None)

    def test_missing_key_is_reported_not_retried(self):
        with mock.patch.object(config, "api_key", return_value=None):
            for provider in ("groq", "anthropic"):
                with self.assertRaises(llm.LLMUnavailable):
                    llm.call_llm([llm.text_block("hi")], purpose="test", task="text", provider=provider, model="m")


class ConfigTests(unittest.TestCase):
    def test_defaults_and_overrides(self):
        with mock.patch.dict(os.environ, {"GHOST_LLM_PROVIDER": "", "GHOST_VISION_MODEL": "", "GHOST_TEXT_MODEL": ""}):
            os.environ.pop("GHOST_LLM_PROVIDER")
            with mock.patch.object(config, "load_env"):
                self.assertEqual(config.llm_provider(), "groq")
                self.assertEqual(config.model_for("groq", "vision"), "qwen/qwen3.8-27b")
                self.assertEqual(config.model_for("anthropic", "text"), "claude-haiku-5-5")
                os.environ["GHOST_VISION_MODEL"] = "custom/vision"
                self.assertEqual(config.model_for("groq", "vision"), "custom/vision")
                os.environ["GHOST_LLM_PROVIDER"] = "openai"
                with self.assertRaises(ValueError):
                    config.llm_provider()


class ScreenshotMappingTests(unittest.TestCase):
    def test_cached_extraction_becomes_observations(self):
        evidence = load_dataset().evidence_by_id["evidence_04"]
        cache = {
            "evidence_04": {
                "sha256": images._sha256(evidence.path),
                "extraction": {
                    "records": [
                        {"resource_id": "account_5762", "system": "account_service", "record_type": "USER_PROFILE", "presence": "PRESENT",
                         "condition": "IDENTIFIABLE", "emails": ["haruka.s1838@zoho.com"], "user_ids": ["user_11"], "phones": [], "names": ["Haruka Sato"]},
                    ]
                },
            }
        }
        observations, _, problems = images.parse_image(evidence, cache)
        self.assertEqual(problems, [])
        self.assertEqual(len(observations), 1)
        obs = observations[0]
        self.assertEqual((obs.resource_id, obs.presence, obs.condition, obs.category), ("account_5762", "PRESENT", "IDENTIFIABLE", "USER_PROFILE"))
        self.assertIn("email:haruka.s1838@zoho.com", obs.identifiers)
        self.assertIn("user:user_11", obs.identifiers)


if __name__ == "__main__":
    unittest.main()
