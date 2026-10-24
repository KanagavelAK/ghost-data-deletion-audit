"""Regression tests for the evidence normalizer.

    python -m unittest discover -s code/tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ghost_audit import paths  # noqa: E402
from ghost_audit.dataset import load_dataset, load_requests  # noqa: E402
from ghost_audit.evidence import load_evidence  # noqa: E402
from ghost_audit.evidence.logs import parse_line  # noqa: E402
from ghost_audit.evidence.model import classify_token  # noqa: E402
from ghost_audit.output import read_output  # noqa: E402


class LineSyntaxTests(unittest.TestCase):
    """One example of each log syntax found in dataset/evidence/logs."""

    CASES = {
        "kv": '2026-09-15T11:56:57Z level=INFO service=account_service trace_id=tr-1 resource_id=account_9797 event=profile_lookup_completed rows=0 http_status=404',
        "json": '2026-09-10T08:56:20Z {"service":"account_service","trace_id":"tr-2","resource_id":"account_6633","event":"profile_lookup_completed","rows":"1"}',
        "syslog": '2026-08-22T10:51:17Z <15>1 node-1 account_service tr-3 - - level=DEBUG resource_id="account_1785" event="profile_lookup_completed" rows="1"',
        "bracket": '2026-07-28T05:51:21Z [INFO] [account_service] [tr-4] profile_lookup_completed: resource_id="account_7947" rows="0" http_status="404"',
        "pipe": '2026-07-03T07:51:32Z | DEBUG | security_service | tr-5 | audit_record_read_completed | resource_id="audit_record_4413" rows="1"',
        "cloud": '2026-06-08T06:52:09Z requestId=tr-6 level=INFO function=search_service message="resource_id=\\"search_021\\" event=\\"index_query_completed\\" result=\\"hit\\""',
    }

    def test_every_syntax_yields_service_trace_event_and_resource(self):
        for name, line in self.CASES.items():
            with self.subTest(syntax=name):
                parsed = parse_line(1, line)
                self.assertIsNotNone(parsed)
                self.assertTrue(parsed.service.endswith(("_service", "_platform")))
                self.assertTrue(parsed.trace_id.startswith("tr-"))
                self.assertTrue(parsed.event)
                self.assertRegex(parsed.fields["resource_id"], r"^[a-z_]+_\d+$")


class TokenTests(unittest.TestCase):
    def test_identifier_classification(self):
        self.assertEqual(classify_token("SIPHO.MOKOENA@GMAIL.COM"), ("email", "sipho.mokoena@gmail.com"))
        self.assertEqual(classify_token("user_05"), ("user", "user_05"))
        self.assertEqual(classify_token("+971 50 482 7316"), ("phone", "971504827316"))
        self.assertEqual(classify_token("44a65154bfd8e408852578fbd76813d094d1944f255ac1769ee81ba90492f10e")[0], "hash")
        self.assertEqual(classify_token("analytics_257"), ("resource", "analytics_257"))
        self.assertIsNone(classify_token("null"))


class DatasetEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = load_dataset()
        cls.store = load_evidence(cls.dataset)

    def test_all_non_image_files_parse_cleanly(self):
        problems = [p for s in self.store.summaries if s.kind != "png" for p in s.problems]
        self.assertEqual(problems, [])

    def test_failover_replica_ghost_is_visible(self):
        ghost = [o for o in self.store.by_resource["account_005"] if o.copy == "failover_replica"]
        self.assertEqual(len(ghost), 1)
        self.assertEqual(ghost[0].presence, "PRESENT")
        self.assertIn("email:leila.haddad@yahoo.com", ghost[0].identifiers)

    def test_tombstone_redaction_state(self):
        self.assertEqual(self.store.by_resource["tombstone_440"][-1].condition, "REDACTED")
        self.assertEqual(self.store.by_resource["tombstone_9178"][-1].condition, "IDENTIFIABLE")

    def test_sample_citations_match_normalized_state(self):
        """Every cited step in sample_output.csv is backed by the cited file with the expected state."""
        expected = {
            "REPORT_REMAINING_RECORD": ("PRESENT", None), "REPORT_EXPOSED_PII": ("PRESENT", "IDENTIFIABLE"),
            "REPORT_ACTIVE_DATA_FLOW": ("PRESENT", "ACTIVE"), "REPORT_RESTORE_RISK": ("PRESENT", "RESTORABLE"),
            "VERIFY_RECORD_ABSENT": ("ABSENT", None), "VERIFY_RECORD_PRESENT": ("PRESENT", None),
            "VERIFY_RECORD_REDACTED": ("PRESENT", "REDACTED"), "VERIFY_RECORD_ANONYMIZED": ("PRESENT", "ANONYMIZED"),
            "VERIFY_DATA_FLOW_DISABLED": ("PRESENT", "DISABLED"), "VERIFY_RESTORE_BLOCKED": ("PRESENT", "BLOCKED"),
        }
        requests = {r.case_id: r for r in load_requests(paths.SAMPLE_REQUESTS)}
        _, rows = read_output(paths.SAMPLE_GOLD)
        for row in rows:
            for step in row.steps:
                if not step["evidence_id"]:
                    continue
                with self.subTest(case=row.case_id, resource=step["resource_id"]):
                    presence, condition = expected[step["operation"]]
                    in_file = [o for o in self.store.by_resource.get(step["resource_id"], []) if o.file_id == step["evidence_id"]]
                    if in_file:
                        self.assertEqual(in_file[-1].presence, presence)
                        if condition:
                            self.assertEqual(in_file[-1].condition, condition)
                    else:
                        self.assertEqual(presence, "ABSENT")
                        covered = [c for c in self.store.coverage if c.file_id == step["evidence_id"] and c.system_id == step["system_id"]]
                        self.assertTrue(covered)
                        self.assertLessEqual(covered[0].observed_at, requests[row.case_id].audit_at)


if __name__ == "__main__":
    unittest.main()
