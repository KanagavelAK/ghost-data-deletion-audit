"""Tests for activity linking, state reconstruction, policy and the end-to-end sample score.

    python -m unittest discover -s code/tests
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evaluation"))

from ghost_audit import paths  # noqa: E402
from ghost_audit.dataset import load_dataset, load_requests  # noqa: E402
from ghost_audit.evidence import load_evidence  # noqa: E402
from ghost_audit.linker import in_phase  # noqa: E402
from ghost_audit.output import read_output  # noqa: E402
from ghost_audit.schema import Step  # noqa: E402
from ghost_audit.solver import solve_all  # noqa: E402
from scorer import score  # noqa: E402


class PhaseTests(unittest.TestCase):
    def test_phase_windows(self):
        anchor = datetime(2026, 9, 5, 11, 1, 53, tzinfo=timezone.utc)
        reported = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(in_phase(anchor, anchor + timedelta(seconds=1781), reported))  # anonymize at -28:26 (case_21)
        self.assertFalse(in_phase(anchor, anchor + timedelta(seconds=1782 + 3), reported))
        self.assertTrue(in_phase(anchor, anchor + timedelta(seconds=6352), reported))  # post-report check, +22 s phase
        self.assertFalse(in_phase(anchor, anchor + timedelta(seconds=6352 - 12), reported))


class SampleBehaviourTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dataset = load_dataset()
        store = load_evidence(dataset)
        cls.requests = load_requests(paths.SAMPLE_REQUESTS)
        cls.results = {r.case_id: r for r in solve_all(cls.requests, dataset, store, use_llm=False)}
        _, rows = read_output(paths.SAMPLE_GOLD)
        cls.gold = {r.case_id: (r.status, r.steps) for r in rows}

    def steps(self, case_id):
        return set(self.results[case_id].steps)

    def test_ghost_in_failover_replica_makes_case_incomplete(self):
        self.assertEqual(self.results["case_05"].status, "INCOMPLETE")
        self.assertIn(Step("REPORT_REMAINING_RECORD", "account_service", "account_005", "evidence_54"), self.steps("case_05"))

    def test_absence_proven_by_full_table_listing(self):
        self.assertIn(Step("VERIFY_RECORD_ABSENT", "account_service", "account_001", "evidence_54"), self.steps("case_01"))

    def test_failed_activity_does_not_override_evidence(self):
        # case_09: the account deletion reported FAILED, but evidence shows it absent.
        self.assertIn(Step("VERIFY_RECORD_ABSENT", "account_service", "account_3960"), self.steps("case_09"))

    def test_later_captured_file_is_not_cited(self):
        # The newest sighting of backup_4978 is in evidence_06 (captured after audit_at); evidence_93 is cited.
        self.assertIn(Step("REPORT_RESTORE_RISK", "backup_service", "backup_4978", "evidence_93"), self.steps("case_09"))

    def test_record_keyed_by_old_email_hash_is_deleted(self):
        self.assertIn(Step("DELETE_RECORD", "analytics_platform", "analytics_9914"), self.steps("case_12"))

    def test_tombstone_keeps_reference_but_loses_identifiers(self):
        self.assertEqual(self.results["case_31"].status, "COMPLETE")
        self.assertIn(Step("VERIFY_RECORD_REDACTED", "identity_service", "tombstone_440", "evidence_51"), self.steps("case_31"))
        self.assertIn(Step("REDACT_RECORD", "identity_service", "tombstone_9178"), self.steps("case_32"))

    def test_activity_only_records_are_linked(self):
        # account_6440 and analytics_5335 appear in no evidence with case_21's identifiers.
        self.assertIn(Step("VERIFY_RECORD_ANONYMIZED", "analytics_platform", "analytics_5335", "evidence_92"), self.steps("case_21"))
        self.assertIn(Step("VERIFY_RECORD_ABSENT", "account_service", "account_6440", "evidence_92"), self.steps("case_21"))

    def test_sample_score_regression_guard(self):
        pred = {cid: (r.status, [s.to_dict(i) for i, s in enumerate(r.steps, 1)]) for cid, r in self.results.items()}
        summary = score(self.gold, pred)
        self.assertEqual(summary.status_accuracy, 1.0)
        # Three gold records exist in no participant-visible data (account_004, audit_record_6645, audit_record_5569).
        self.assertGreaterEqual(sum(c.exact for c in summary.cases), 29)
        self.assertEqual(summary.steps.fp, 0)


if __name__ == "__main__":
    unittest.main()
