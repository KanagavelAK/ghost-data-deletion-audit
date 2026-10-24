"""Output contract: statuses, phases, operations and canonical step ordering."""

from dataclasses import dataclass

STATUSES = ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")
REPORTED_STATUSES = ("COMPLETE", "PARTIALLY_COMPLETE", "NOT_STARTED")
PHASES = ("FINDING", "REMEDIATION", "VERIFICATION")

# Operation -> (phase, state), in the order of the problem statement's table.
# This order is also the required order of operations within a phase.
OPERATIONS = {
    "REPORT_REMAINING_RECORD": ("FINDING", "PRESENT"),
    "REPORT_EXPOSED_PII": ("FINDING", "PRESENT"),
    "REPORT_ACTIVE_DATA_FLOW": ("FINDING", "ACTIVE"),
    "REPORT_RESTORE_RISK": ("FINDING", "ACTIVE"),
    "DISABLE_DATA_FLOW": ("REMEDIATION", "DISABLED"),
    "BLOCK_RESTORE": ("REMEDIATION", "BLOCKED"),
    "DELETE_RECORD": ("REMEDIATION", "ABSENT"),
    "REDACT_RECORD": ("REMEDIATION", "REDACTED"),
    "ANONYMIZE_RECORD": ("REMEDIATION", "ANONYMIZED"),
    "VERIFY_RECORD_ABSENT": ("VERIFICATION", "ABSENT"),
    "VERIFY_RECORD_PRESENT": ("VERIFICATION", "PRESENT"),
    "VERIFY_RECORD_REDACTED": ("VERIFICATION", "REDACTED"),
    "VERIFY_RECORD_ANONYMIZED": ("VERIFICATION", "ANONYMIZED"),
    "VERIFY_DATA_FLOW_DISABLED": ("VERIFICATION", "DISABLED"),
    "VERIFY_RESTORE_BLOCKED": ("VERIFICATION", "BLOCKED"),
}

PHASE_ORDER = {phase: i for i, phase in enumerate(PHASES)}
OPERATION_ORDER = {op: i for i, op in enumerate(OPERATIONS)}

# Key order used when serializing a step (matches the problem statement example).
STEP_FIELDS = ("step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id")


@dataclass(frozen=True)
class Step:
    """One step of steps_json. Phase and state are implied by the operation."""

    operation: str
    system_id: str
    resource_id: str
    evidence_id: str = ""

    def __post_init__(self):
        if self.operation not in OPERATIONS:
            raise ValueError(f"unknown operation {self.operation!r}")

    @property
    def phase(self) -> str:
        return OPERATIONS[self.operation][0]

    @property
    def state(self) -> str:
        return OPERATIONS[self.operation][1]

    def to_dict(self, step_no: int) -> dict:
        return {
            "step_no": step_no,
            "phase": self.phase,
            "system_id": self.system_id,
            "resource_id": self.resource_id,
            "operation": self.operation,
            "state": self.state,
            "evidence_id": self.evidence_id,
        }


def step_sort_key(phase: str, operation: str, system_id: str, resource_id: str) -> tuple:
    """Phase, then operation-table order, then system_id and resource_id ascending."""
    return (PHASE_ORDER[phase], OPERATION_ORDER[operation], system_id, resource_id)


def canonical_order(steps: list[Step]) -> list[Step]:
    """Sort steps into the required order and drop exact duplicates."""
    unique = list(dict.fromkeys(steps))
    return sorted(unique, key=lambda s: step_sort_key(s.phase, s.operation, s.system_id, s.resource_id))


def case_sort_key(case_id: str) -> tuple:
    """Order case ids numerically (case_33 before case_100), as in deletion_requests.csv."""
    prefix, _, number = case_id.rpartition("_")
    return (prefix, int(number)) if number.isdigit() else (case_id, -1)
