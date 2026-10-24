"""Apply deletion_rules.csv to a reconstructed resource state.

Each resource gets an Assessment: its data category, the required treatment,
whether the current state complies, and the finding / remediation /
verification operations that follow from that.
"""

from dataclasses import dataclass

from .dataset import Activity, Rule, parse_ts
from .evidence.model import ABSENT, ACTIVE, ANONYMIZED, BLOCKED, DISABLED, IDENTIFIABLE, PRESENT, REDACTED, RESTORABLE, default_category
from .state import ResourceState

# Workflow operation attempted on a record -> the treatment it implies (used when the category is unknown).
ATTEMPT_TO_ACTION = {"DELETE_RECORD": "DELETE", "REDACT_RECORD": "REDACT", "ANONYMIZE_RECORD": "ANONYMIZE"}
ACTION_TO_CATEGORY = {"DELETE": "SUPPORT_ATTACHMENT", "REDACT": "SUPPORT_MESSAGE", "ANONYMIZE": "ANALYTICS_PROFILE"}


@dataclass
class Assessment:
    state: ResourceState
    category: str | None
    action: str  # DELETE | REDACT | ANONYMIZE | RETAIN | BLOCK_RESTORE | DISABLE_FLOW | UNKNOWN
    compliant: bool
    finding: str | None  # operation names from the output contract
    remediation: str | None
    verification: str | None
    reason: str

    @property
    def resource_id(self) -> str:
        return self.state.resource_id

    @property
    def system_id(self) -> str:
        return self.state.system_id


class Policy:
    def __init__(self, rules: list[Rule]):
        self.action_by_category = {r.data_category: r.required_action for r in rules}

    def category_for(self, state: ResourceState, activities: list[Activity]) -> str | None:
        category = state.category or next(iter(sorted(state.observed_categories)), None) or default_category(state.resource_id)
        # A record that identifies the subject only through an email hash is a secondary identifier.
        if category == "ANALYTICS_PROFILE" and state.condition == IDENTIFIABLE and state.pii_fields == {"email_hash"}:
            return "SECONDARY_IDENTIFIER"
        # Support records whose type the evidence does not show: the workflow's attempted treatment tells,
        # otherwise assume the common case, a conversation message.
        if category is None and state.resource_id.startswith("support_"):
            attempted = self._attempted_action(activities)
            return ACTION_TO_CATEGORY[attempted] if attempted in ("DELETE", "REDACT") else "SUPPORT_MESSAGE"
        return category

    @staticmethod
    def _retention_end(state: ResourceState):
        for obs in reversed(state.supporting):
            for name in ("retention_until", "retained_until"):
                if obs.attributes.get(name):
                    try:
                        return parse_ts(str(obs.attributes[name]))
                    except ValueError:
                        return None
        return None

    @staticmethod
    def _attempted_action(activities: list[Activity]) -> str | None:
        attempts = [ATTEMPT_TO_ACTION[a.event_type] for a in activities if a.event_type in ATTEMPT_TO_ACTION]
        return attempts[-1] if attempts else None

    def assess(self, state: ResourceState, activities: list[Activity]) -> Assessment:
        category = self.category_for(state, activities)
        presence, condition = state.presence, state.condition

        if category == "DATA_FLOW":
            if presence == PRESENT and condition == ACTIVE:
                return Assessment(state, category, "DISABLE_FLOW", False, "REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED", "active flow can recreate data")
            return Assessment(state, category, "DISABLE_FLOW", condition == DISABLED or presence == ABSENT, None, None, "VERIFY_DATA_FLOW_DISABLED", f"flow {condition or presence}")

        if category == "BACKUP_ARCHIVE":
            if presence == PRESENT and condition == RESTORABLE:
                return Assessment(state, category, "BLOCK_RESTORE", False, "REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED", "restore still allowed")
            return Assessment(state, category, "BLOCK_RESTORE", condition == BLOCKED, None, None, "VERIFY_RESTORE_BLOCKED", f"backup {condition or presence}")

        action = self.action_by_category.get(category or "") or self._attempted_action(activities) or "UNKNOWN"

        if action == "DELETE":
            if presence == PRESENT:
                return Assessment(state, category, action, False, "REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT", "record must be deleted but is present")
            return Assessment(state, category, action, True, None, None, "VERIFY_RECORD_ABSENT", f"record {presence.lower()}")
        if action == "REDACT":
            if presence == PRESENT and condition == IDENTIFIABLE:
                return Assessment(state, category, action, False, "REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED", "direct identifiers still exposed")
            return Assessment(state, category, action, presence == PRESENT and condition == REDACTED, None, None, "VERIFY_RECORD_REDACTED", f"record {condition or presence}")
        if action == "ANONYMIZE":
            if presence == PRESENT and condition == IDENTIFIABLE:
                return Assessment(state, category, action, False, "REPORT_EXPOSED_PII", "ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED", "still attributable to the user")
            return Assessment(state, category, action, presence == PRESENT and condition == ANONYMIZED, None, None, "VERIFY_RECORD_ANONYMIZED", f"record {condition or presence}")
        if action == "RETAIN":
            expiry = self._retention_end(state)
            if expiry is not None and expiry < (state.as_of or expiry):
                # Rules 12/13 only retain within the retention period / while the hold is active.
                if presence == PRESENT:
                    return Assessment(state, category, "DELETE", False, "REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT", "retention period has ended")
                return Assessment(state, category, "DELETE", True, None, None, "VERIFY_RECORD_ABSENT", "retention ended; record absent")
            return Assessment(state, category, action, presence == PRESENT, None, None, "VERIFY_RECORD_PRESENT", "retained by policy")
        return Assessment(state, category, "UNKNOWN", True, None, None, None, "no applicable rule")
