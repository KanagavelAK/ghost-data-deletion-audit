"""Contract validator for output.csv.

Errors are violations of the submission contract that the grader is likely to
penalise or reject (bad JSON, wrong enums, broken numbering, ordering, citation
rules). Warnings flag outputs that are legal but semantically suspicious.
"""

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .dataset import Dataset, Request
from .output import OUTPUT_HEADER, OutputRow, read_output
from .schema import OPERATIONS, PHASE_ORDER, PHASES, STATUSES, STEP_FIELDS, case_sort_key, step_sort_key


@dataclass
class ValidationReport:
    errors: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[tuple[str, str]] = field(default_factory=list)

    def error(self, case_id: str, message: str) -> None:
        self.errors.append((case_id, message))

    def warn(self, case_id: str, message: str) -> None:
        self.warnings.append((case_id, message))

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self, limit: int = 20) -> str:
        lines = [f"validation: {len(self.errors)} error(s), {len(self.warnings)} warning(s)"]
        for label, items in (("ERROR", self.errors), ("WARN", self.warnings)):
            for case_id, message in items[:limit]:
                lines.append(f"  {label} {case_id}: {message}")
            if len(items) > limit:
                lines.append(f"  ... {len(items) - limit} more {label.lower()}(s)")
        return "\n".join(lines)


def validate_file(path: Path, requests: list[Request], dataset: Dataset) -> ValidationReport:
    header, rows = read_output(path)
    return validate_rows(header, rows, requests, dataset)


def validate_rows(header: list[str], rows: list[OutputRow], requests: list[Request], dataset: Dataset) -> ValidationReport:
    report = ValidationReport()
    if tuple(header) != OUTPUT_HEADER:
        report.error("-", f"header must be {','.join(OUTPUT_HEADER)!r}, got {','.join(header)!r}")

    expected = {r.case_id: r for r in requests}
    seen = Counter(row.case_id for row in rows)
    if len(rows) != len(requests):
        report.error("-", f"expected {len(requests)} data rows, got {len(rows)}")
    for case_id, count in seen.items():
        if count > 1:
            report.error(case_id, f"duplicate case_id ({count} rows)")
        if case_id not in expected:
            report.error(case_id, "case_id not present in the requests file")
    for case_id in expected:
        if case_id not in seen:
            report.error(case_id, "missing row")
    order = [row.case_id for row in rows]
    if order != sorted(order, key=case_sort_key):
        report.error("-", "rows are not sorted by case_id")

    for row in rows:
        _validate_row(row, expected.get(row.case_id), dataset, report)
    return report


def _validate_row(row: OutputRow, request: Request | None, dataset: Dataset, report: ValidationReport) -> None:
    cid = row.case_id
    if row.status not in STATUSES:
        report.error(cid, f"invalid deletion_status {row.status!r}")
    if row.steps is None:
        report.error(cid, f"steps_json is not valid JSON: {row.parse_error}")
        return
    if not isinstance(row.steps, list):
        report.error(cid, "steps_json must be a JSON array")
        return

    steps = []
    for i, step in enumerate(row.steps, start=1):
        if not isinstance(step, dict):
            report.error(cid, f"step {i} is not an object")
            return
        keys = set(step)
        if keys != set(STEP_FIELDS):
            report.error(cid, f"step {i} keys differ: missing={sorted(set(STEP_FIELDS) - keys)} extra={sorted(keys - set(STEP_FIELDS))}")
            return
        if step["step_no"] != i or isinstance(step["step_no"], bool):
            report.error(cid, f"step {i} has step_no {step['step_no']!r}; numbering must be 1..n")
        for name in STEP_FIELDS[1:]:
            if not isinstance(step[name], str):
                report.error(cid, f"step {i} field {name} must be a string")
        op = step["operation"]
        if op not in OPERATIONS:
            report.error(cid, f"step {i} unknown operation {op!r}")
            return
        phase, state = OPERATIONS[op]
        if step["phase"] != phase:
            report.error(cid, f"step {i} {op} must be in phase {phase}, got {step['phase']!r}")
        if step["state"] != state:
            report.error(cid, f"step {i} {op} must have state {state}, got {step['state']!r}")
        if not step["system_id"] or not step["resource_id"]:
            report.error(cid, f"step {i} has empty system_id or resource_id")
        steps.append(step)

    _check_order(cid, steps, report)
    if request is not None and row.status in STATUSES:
        _check_status_rules(cid, row.status, steps, request, dataset, report)


def _check_order(cid: str, steps: list[dict], report: ValidationReport) -> None:
    phases = [PHASE_ORDER.get(s["phase"], -1) for s in steps]
    if phases != sorted(phases):
        report.error(cid, "phases are not ordered FINDING -> REMEDIATION -> VERIFICATION")
        return
    keys = [step_sort_key(s["phase"], s["operation"], s["system_id"], s["resource_id"]) for s in steps]
    if keys != sorted(keys):
        report.error(cid, "steps within a phase are not ordered by operation, system_id, resource_id")
    dupes = [k for k, n in Counter(keys).items() if n > 1]
    if dupes:
        report.warn(cid, f"{len(dupes)} duplicated step(s)")


def _check_status_rules(cid: str, status: str, steps: list[dict], request: Request, dataset: Dataset, report: ValidationReport) -> None:
    by_phase = {p: [s for s in steps if s["phase"] == p] for p in PHASES}

    if status == "NOT_APPLICABLE":
        if steps:
            report.error(cid, "NOT_APPLICABLE must use an empty steps array")
        return

    if status == "COMPLETE":
        if by_phase["FINDING"] or by_phase["REMEDIATION"]:
            report.error(cid, "COMPLETE may only contain VERIFICATION steps")
        if not steps:
            report.warn(cid, "COMPLETE with no verification steps")
        for s in steps:
            _check_citation(cid, s, request, dataset, report)
        return

    # INCOMPLETE
    if not by_phase["FINDING"]:
        report.error(cid, "INCOMPLETE must report at least one finding")
    if not by_phase["REMEDIATION"]:
        report.warn(cid, "INCOMPLETE without remediation steps")
    if not by_phase["VERIFICATION"]:
        report.warn(cid, "INCOMPLETE without verification steps")
    for s in by_phase["FINDING"]:
        _check_citation(cid, s, request, dataset, report)
    for s in by_phase["REMEDIATION"] + by_phase["VERIFICATION"]:
        if s["evidence_id"] != "":
            report.error(cid, f"{s['operation']} {s['resource_id']} is future work and must use an empty evidence_id")

    found = {(s["system_id"], s["resource_id"]) for s in by_phase["FINDING"]}
    fixed = {(s["system_id"], s["resource_id"]) for s in by_phase["REMEDIATION"]}
    verified = {(s["system_id"], s["resource_id"]) for s in by_phase["VERIFICATION"]}
    for system_id, resource_id in sorted(found - fixed):
        report.warn(cid, f"finding for {resource_id} has no remediation")
    for system_id, resource_id in sorted(fixed - verified):
        report.warn(cid, f"remediation for {resource_id} has no verification")


def _check_citation(cid: str, step: dict, request: Request, dataset: Dataset, report: ValidationReport) -> None:
    evidence_id = step["evidence_id"]
    if not evidence_id:
        report.error(cid, f"{step['operation']} {step['resource_id']} must cite a file_id, activity_id or audit_id")
        return
    when = dataset.evidence_time(evidence_id)
    if when is None:
        report.error(cid, f"{step['operation']} {step['resource_id']} cites unknown evidence {evidence_id!r}")
    elif when > request.audit_at:
        report.error(cid, f"{step['operation']} {step['resource_id']} cites {evidence_id} captured after audit_at")
