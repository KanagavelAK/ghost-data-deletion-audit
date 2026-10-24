"""Typed loaders for the participant-facing CSV files in dataset/."""

import csv
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import paths


def parse_ts(value: str) -> datetime:
    """Parse an ISO-8601 UTC timestamp such as 2026-09-15T12:00:00Z."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _read_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


@dataclass(frozen=True)
class Request:
    case_id: str
    email: str
    requested_at: datetime
    reported_status: str
    reported_at: datetime
    audit_at: datetime
    request_text: str


@dataclass(frozen=True)
class UserIdentity:
    user_id: str
    name: str
    email: str
    phone: str
    email_hash: str
    updated_at: datetime


@dataclass(frozen=True)
class AuditEntry:
    audit_id: str
    user_id: str
    event_time: datetime
    field_name: str
    old_value: str
    new_value: str


@dataclass(frozen=True)
class Activity:
    activity_id: str
    event_time: datetime
    event_type: str
    source_system: str
    target_system: str
    resource_id: str
    reported_result: str
    details: str


@dataclass(frozen=True)
class Rule:
    rule_id: str
    data_category: str
    condition: str
    required_action: str


@dataclass(frozen=True)
class EvidenceFile:
    file_id: str
    file_path: str
    origin: str
    captured_at: datetime

    @property
    def path(self) -> Path:
        return paths.DATASET_DIR / self.file_path


def load_requests(path: Path) -> list[Request]:
    return [
        Request(
            case_id=r["case_id"],
            email=r["email"],
            requested_at=parse_ts(r["requested_at"]),
            reported_status=r["reported_status"],
            reported_at=parse_ts(r["reported_at"]),
            audit_at=parse_ts(r["audit_at"]),
            request_text=r["request_text"],
        )
        for r in _read_csv(path)
    ]


def load_all_requests() -> list[Request]:
    """Sample and evaluation requests together (used to find a person's earlier requests)."""
    return load_requests(paths.SAMPLE_REQUESTS) + load_requests(paths.EVAL_REQUESTS)


@dataclass
class Dataset:
    """Everything except the requests, which are loaded per split."""

    identities: list[UserIdentity]
    audit_log: list[AuditEntry]
    activities: list[Activity]
    rules: list[Rule]
    evidence_files: list[EvidenceFile]
    identities_by_user: dict[str, UserIdentity] = field(init=False)
    evidence_by_id: dict[str, EvidenceFile] = field(init=False)
    activity_by_id: dict[str, Activity] = field(init=False)
    audit_by_id: dict[str, AuditEntry] = field(init=False)

    def __post_init__(self):
        self.identities_by_user = {u.user_id: u for u in self.identities}
        self.evidence_by_id = {e.file_id: e for e in self.evidence_files}
        self.activity_by_id = {a.activity_id: a for a in self.activities}
        self.audit_by_id = {a.audit_id: a for a in self.audit_log}

    def evidence_time(self, evidence_id: str) -> datetime | None:
        """Capture/event time of a citable file_id, activity_id or audit_id."""
        if evidence_id in self.evidence_by_id:
            return self.evidence_by_id[evidence_id].captured_at
        if evidence_id in self.activity_by_id:
            return self.activity_by_id[evidence_id].event_time
        if evidence_id in self.audit_by_id:
            return self.audit_by_id[evidence_id].event_time
        return None


def load_dataset(dataset_dir: Path = paths.DATASET_DIR) -> Dataset:
    return Dataset(
        identities=[
            UserIdentity(r["user_id"], r["name"], r["email"], r["phone"], r["email_hash"], parse_ts(r["updated_at"]))
            for r in _read_csv(dataset_dir / "user_identities.csv")
        ],
        audit_log=[
            AuditEntry(r["audit_id"], r["user_id"], parse_ts(r["event_time"]), r["field_name"], r["old_value"], r["new_value"])
            for r in _read_csv(dataset_dir / "user_audit_log.csv")
        ],
        activities=[
            Activity(
                r["activity_id"],
                parse_ts(r["event_time"]),
                r["event_type"],
                r["source_system"],
                r["target_system"],
                r["resource_id"],
                r["reported_result"],
                r["details"],
            )
            for r in _read_csv(dataset_dir / "system_activity.csv")
        ],
        rules=[
            Rule(r["rule_id"], r["data_category"], r["condition"], r["required_action"])
            for r in _read_csv(dataset_dir / "deletion_rules.csv")
        ],
        evidence_files=[
            EvidenceFile(r["file_id"], r["file_path"], r["origin"], parse_ts(r["captured_at"]))
            for r in _read_csv(dataset_dir / "evidence_files.csv")
        ],
    )
