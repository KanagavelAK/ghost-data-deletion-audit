"""Tests for identity resolution, case context linking and intent classification.

    python -m unittest discover -s code/tests
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ghost_audit import intent as intent_module  # noqa: E402
from ghost_audit.context import ContextBuilder  # noqa: E402
from ghost_audit.dataset import load_all_requests, load_dataset  # noqa: E402
from ghost_audit.evidence import load_evidence  # noqa: E402
from ghost_audit.identity import IdentityResolver, email_hash  # noqa: E402
from ghost_audit.intent import IntentClassifier, classify_by_rules, classify_intent  # noqa: E402


class IdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.resolver = IdentityResolver(load_dataset())

    def test_historical_email_resolves_to_same_user_with_all_identifiers(self):
        identity = self.resolver.resolve("nadia.ionescu@yahoo.com")
        self.assertEqual(identity.user_id, "user_10")
        self.assertEqual(identity.matched_via, "historical_email")
        self.assertEqual(identity.emails, {"nadia.ionescu@yahoo.com", "nadia.petrescu@yahoo.com", "n.petrescu@yahoo.com"})

    def test_old_email_hash_is_an_identifier(self):
        identity = self.resolver.resolve("minjun.kim@gmail.com")
        self.assertIn("hash:" + email_hash("minjun.kim90@gmail.com"), identity.tokens())

    def test_uppercase_request_email(self):
        self.assertIsNotNone(self.resolver.resolve("SIPHO.MOKOENA@GMAIL.COM").user_id)

    def test_unknown_email_keeps_email_and_hash(self):
        identity = self.resolver.resolve("nobody@example.com")
        self.assertIsNone(identity.user_id)
        self.assertEqual(identity.matched_via, "unknown")
        self.assertIn("hash:" + email_hash("nobody@example.com"), identity.tokens())

    def test_identifier_validity_in_time(self):
        identity = self.resolver.resolve("minjun.kim@gmail.com")
        before_change = min(s.valid_to for s in identity.spans if s.valid_to)
        tokens_then = identity.tokens(until=before_change.replace(year=2025))
        self.assertIn("email:minjun.kim90@gmail.com", tokens_then)
        self.assertNotIn("email:minjun.kim@gmail.com", tokens_then)


class ContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dataset = load_dataset()
        cls.requests = {r.case_id: r for r in load_all_requests()}
        cls.builder = ContextBuilder(dataset, load_evidence(dataset), list(cls.requests.values()))

    def build(self, case_id):
        request = self.requests[case_id]
        return self.builder.build(request, classify_intent(request.request_text))

    def test_earlier_request_of_same_person_does_not_leak(self):
        ctx = self.build("case_17")  # user_17 also filed case_46 in March with account_6523
        self.assertIn("account_4092", ctx.links)
        self.assertNotIn("account_6523", ctx.links)
        self.assertIn("account_6523", ctx.earlier_only)

    def test_record_keyed_by_old_email_hash_is_linked(self):
        self.assertIn("analytics_9914", self.build("case_12").links)

    def test_lookalike_decoy_is_flagged_not_linked(self):
        ctx = self.build("case_28")
        self.assertIn("smartinez6461.jr@gmail.com", ctx.lookalikes)
        decoy_records = {o.resource_id for o in self.builder.store.by_identifier["email:smartinez6461.jr@gmail.com"]}
        self.assertFalse(decoy_records & set(ctx.links))

    def test_search_document_links_its_source_account(self):
        ctx = self.build("case_06")
        self.assertIn("search_001", ctx.links)
        self.assertIn("account_006", ctx.links)
        self.assertIn("ref:search_001", ctx.links["account_006"].via)

    def test_only_evidence_up_to_audit_at_is_used(self):
        for case_id in ("case_03", "case_11", "case_27"):
            ctx = self.build(case_id)
            for history in ctx.history.values():
                self.assertTrue(all(o.observed_at <= ctx.request.audit_at for o in history))


class IntentTests(unittest.TestCase):
    def test_non_deletion_requests_are_detected(self):
        for text in (
            "Please stop the promotional emails. I still need the account and my order history, so I am not asking for deletion.",
            "Could you send me a copy of the information held on my account? I still use it, so please don't delete or change anything.",
            "Can you tell me what personal information you hold about me? This is an information request, not a request to erase it.",
        ):
            self.assertFalse(classify_by_rules(text).authorizes_deletion, text)

    def test_deletion_requests_in_several_languages(self):
        for text in (
            "I'm deleting my account. You can keep anonymous statistics, but nothing in analytics should still point back to me.",
            "Je demande la suppression définitive du compte associé à cette adresse e-mail.",
            "Solicito a exclusão permanente da conta associada a este e-mail.",
            "このメールアドレスに関連するアカウントの完全な削除を依頼します。",
            "이 이메일 주소에 연결된 계정의 영구 삭제를 요청합니다.",
            "Please check the latest state and confirm that the profile is gone and that remaining records cannot identify me.",
        ):
            self.assertTrue(classify_by_rules(text).authorizes_deletion, text)

    def test_every_dataset_request_is_decided_by_rules(self):
        undecided = [r.case_id for r in load_all_requests() if classify_by_rules(r.request_text) is None]
        self.assertEqual(undecided, [])

    def test_llm_fallback_only_for_undecided_texts_and_cached(self):
        calls = []

        def fake_llm(batch):
            calls.append(list(batch))
            return {t: {"authorizes_deletion": True, "reason": "asks to wipe data"} for t in batch}

        with mock.patch.object(intent_module, "CACHE_PATH", Path(self._tmp()) / "intent.json"):
            classifier = IntentClassifier(llm_call=fake_llm)
            texts = ["Bitte alles von mir entfernen, danke.", "Please delete my account."]
            results = classifier.classify_all(texts)
            self.assertEqual(calls, [["Bitte alles von mir entfernen, danke."]])
            self.assertEqual(results[texts[0]].method, "llm")
            self.assertEqual(results[texts[1]].method, "rule")
            IntentClassifier(llm_call=fake_llm).classify_all(texts)  # second run is served from the cache
            self.assertEqual(len(calls), 1)

    def _tmp(self):
        import tempfile

        directory = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        return directory


if __name__ == "__main__":
    unittest.main()
