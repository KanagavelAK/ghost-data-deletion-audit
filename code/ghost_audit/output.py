"""Reading and writing output.csv with deterministic serialization."""

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

from .schema import Step, case_sort_key

OUTPUT_HEADER = ("case_id", "deletion_status", "steps_json")


@dataclass
class CaseResult:
    case_id: str
    status: str
    steps: list[Step]
    # Free-form reasoning trail for debugging and reports; never written to output.csv.
    notes: dict = field(default_factory=dict)


def serialize_steps(steps: list[Step]) -> str:
    """Number steps 1..n in the given order and emit compact JSON."""
    return json.dumps([s.to_dict(i) for i, s in enumerate(steps, start=1)], ensure_ascii=False, separators=(",", ":"))


def write_output(results: list[CaseResult], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(results, key=lambda r: case_sort_key(r.case_id))
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(OUTPUT_HEADER)
        for r in ordered:
            writer.writerow((r.case_id, r.status, serialize_steps(r.steps)))


@dataclass
class OutputRow:
    case_id: str
    status: str
    steps_raw: str
    steps: list | None  # parsed JSON, or None if steps_json is not valid JSON
    parse_error: str = ""


def read_output(path: Path) -> tuple[list[str], list[OutputRow]]:
    """Return (header, rows) without enforcing the contract; see validate.py."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, [])
        rows = []
        for record in reader:
            record = (record + ["", "", ""])[:3]
            case_id, status, raw = record
            try:
                steps, error = json.loads(raw), ""
            except json.JSONDecodeError as exc:
                steps, error = None, str(exc)
            rows.append(OutputRow(case_id, status, raw, steps, error))
    return header, rows
