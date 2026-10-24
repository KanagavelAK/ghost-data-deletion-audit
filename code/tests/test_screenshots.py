"""Tests for screenshot-related evidence rules, independent of any model output.

    python -m unittest discover -s code/tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ghost_audit.context import ContextBuilder  # noqa: E402
from ghost_audit.dataset import load_all_requests, load_dataset  # noqa: E402
from ghost_audit.evidence import EvidenceStore  # noqa: E402
from ghost_audit.evidence.catalog_links import catalog_aliases  # noqa: E402
from ghost_audit.evidence.images import resolve_resource  # noqa: E402
from ghost_audit.evidence.model import PRESENT, UNKNOWN, Observation  # noqa: E402
from ghost_audit.intent import classify_intent  # noqa: E402
from ghost_audit.policy import Policy  # noqa: E402
from ghost_audit.state import ResourceState  # noqa: E402


class AliasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = load_dataset()
        cls.aliases = catalog_aliases(cls.dataset)

    def test_catalog_display_references(self):
        self.assertEqual(self.aliases["delivery_confirmation_19.pdf"], "object_19")
        self.assertEqual(self.aliases["user_17"], "tombstone_17")
        self.assertEqual(self.aliases["nst-2026-0718-1842"], "export_1842")

    def test_screen_records_resolve_by_id_or_reference(self):
        self.assertEqual(resolve_resource({"resource_id": "search_428"}, self.aliases), "search_428")
        self.assertEqual(resolve_resource({"resource_id": None, "reference": "user_17"}, self.aliases), "tombstone_17")
        self.assertEqual(resolve_resource({"resource_id": "delivery_confirmation_19.pdf"}, self.aliases), "object_19")
        self.assertIsNone(resolve_resource({"resource_id": "ORD-20265762"}, self.aliases))


class CatalogConflictTests(unittest.TestCase):
    def test_record_showing_someone_else_is_not_linked(self):
        dataset = load_dataset()
        requests = {r.case_id: r for r in load_all_requests()}
        request = requests["case_63"]  # kwame-mensah@proton.me
        link_time = request.requested_at.replace(hour=7)
        catalog = Observation(
            "activity_4008", link_time, "marketing_platform", "marketing_001", UNKNOWN,
            identifiers={"email:kwame-mensah@proton.me", "user:user_09"}, source="activity:LINK_RESOURCE_IDENTITY", captured_at=link_time,
        )
        screen = Observation(
            "evidence_60", link_time, "marketing_platform", "marketing_001", PRESENT,
            identifiers={"email:sj.park@naver.com", "phone:821058379451"}, source="image:records[0]", captured_at=link_time,
        )
        store = EvidenceStore([catalog, screen], [], {}, [])
        ctx = ContextBuilder(dataset, store, list(requests.values())).build(request, classify_intent(request.request_text))
        self.assertNotIn("marketing_001", ctx.links)
        self.assertIn("marketing_001", ctx.conflicting)

        # Without the contradicting screen the catalog link stands.
        ctx = ContextBuilder(dataset, EvidenceStore([catalog], [], {}, []), list(requests.values())).build(request, classify_intent(request.request_text))
        self.assertIn("marketing_001", ctx.links)


class UnprovenSafetyStateTests(unittest.TestCase):
    def setUp(self):
        self.policy = Policy(load_dataset().rules)

    def test_backup_without_visible_restore_state_is_a_restore_risk(self):
        state = ResourceState("backup_9287", "backup_service", presence=PRESENT, category="BACKUP_ARCHIVE")
        self.assertEqual(self.policy.assess(state, []).finding, "REPORT_RESTORE_RISK")

    def test_flow_without_visible_state_is_an_active_flow(self):
        state = ResourceState("data_flow_1121", "integration_service", presence=PRESENT, category="DATA_FLOW")
        self.assertEqual(self.policy.assess(state, []).finding, "REPORT_ACTIVE_DATA_FLOW")


if __name__ == "__main__":
    unittest.main()
