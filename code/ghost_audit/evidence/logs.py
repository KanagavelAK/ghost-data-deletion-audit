"""Operational logs in six syntaxes, reduced to observations.

Line syntaxes: key=value, JSON payload, RFC5424 syslog, [LEVEL] [service] [trace],
pipe-delimited, and cloud-runtime lines with an escaped message="..." blob.

Lookups are split across lines: the *_started line carries the lookup key and
the *_completed line carries the resource and the result, joined by trace id.
"""

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from ..dataset import EvidenceFile, parse_ts
from .model import (
    ABSENT,
    BLOCKED,
    IDENTIFIABLE,
    PRESENT,
    RESOURCE_RE,
    RESTORABLE,
    Coverage,
    Observation,
    classify_token,
    map_record,
    map_result_record,
    tokens_from,
)

KV_RE = re.compile(r'([A-Za-z_][\w.]*)=("(?:[^"\\]|\\.)*"|\S+)')
BRACKET_RE = re.compile(r"\[(?P<level>\w+)\] \[(?P<service>[^\]]+)\] \[(?P<trace>[^\]]+)\] (?P<event>\w+): ?(?P<rest>.*)")

# Fields that carry the key a lookup was made with (not the record's content).
KEY_FIELDS = ("lookup_key", "match_key", "subject_key", "scan_key", "owner_key", "subject_ref", "correlation_key", "query")
# Fields whose presence means the line reports the record's own content.
CONTENT_FIELDS = (
    "email", "phone", "email_hash", "customer_reference", "customer_email", "requester_name", "requester_email",
    "requester_phone", "subject_reference", "message_text", "full_name", "display_name", "matched_value",
)


@dataclass
class LogLine:
    line_no: int
    ts: str
    service: str
    trace_id: str
    event: str
    fields: dict


def _kv(text: str) -> dict:
    out = {}
    for key, value in KV_RE.findall(text):
        if value.startswith('"'):
            value = value[1:-1].replace('\\"', '"')
        out[key] = value
    return out


def parse_line(line_no: int, line: str) -> LogLine | None:
    ts, _, rest = line.strip().partition(" ")
    if not ts[:4].isdigit():
        return None
    if rest.startswith("{"):
        try:
            fields = json.loads(rest)
        except json.JSONDecodeError:
            return None
        return LogLine(line_no, ts, fields.get("service") or fields.get("logger", ""), fields.get("trace_id", ""), fields.get("event", ""), fields)
    if rest.startswith("<"):
        parts = rest.split(" ", 6)
        if len(parts) < 7:
            return None
        fields = _kv(parts[6])
        return LogLine(line_no, ts, parts[2], parts[3], fields.get("event", ""), fields)
    if rest.startswith("["):
        match = BRACKET_RE.match(rest)
        if not match:
            return None
        return LogLine(line_no, ts, match["service"], match["trace"], match["event"], _kv(match["rest"]))
    if rest.startswith("| "):
        parts = [p.strip() for p in rest[2:].split(" | ", 4)]
        if len(parts) < 5:
            return None
        return LogLine(line_no, ts, parts[1], parts[2], parts[3], _kv(parts[4]))
    fields = _kv(rest)
    if "message" in fields:  # cloud runtime: the payload is an escaped key=value blob
        fields.update(_kv(fields.pop("message")))
        return LogLine(line_no, ts, fields.get("function", ""), fields.get("requestId", ""), fields.get("event", ""), fields)
    return LogLine(line_no, ts, fields.get("service", ""), fields.get("trace_id", ""), fields.get("event", ""), fields)


def _flag(fields: dict, name: str) -> bool | None:
    value = str(fields.get(name, "")).lower()
    return True if value == "true" else False if value == "false" else None


def _count(fields: dict, *names: str) -> int | None:
    for name in names:
        value = str(fields.get(name, ""))
        if value.isdigit():
            return int(value)
    return None


def _presence(line: LogLine) -> str | None:
    """PRESENT / ABSENT for events that report a lookup result; None for state-only events."""
    f, event = line.fields, line.event
    if event == "profile_lookup_completed":
        status, rows = _count(f, "http_status"), _count(f, "rows")
        if status == 404 or rows == 0:
            return ABSENT
        return PRESENT if status == 200 or (rows or 0) > 0 else None
    if event == "index_query_completed":
        hits = _count(f, "total_hits")
        return ABSENT if f.get("result") == "miss" or hits == 0 else PRESENT
    if event in ("contact_query_completed", "contact_lookup_completed"):
        return PRESENT if (_count(f, "rows") or 0) > 0 else ABSENT
    if event == "restore_policy_read_completed":
        return ABSENT if _flag(f, "archive_present") is False else PRESENT
    if event == "deletion_marker_check":
        return ABSENT if _flag(f, "marker_found") else PRESENT
    return PRESENT


# Events that describe a resource's state; anything else with a resource_id is reported as unhandled.
HANDLED_EVENTS = {
    "profile_lookup_completed", "index_query_completed", "analytics_profile_read_completed", "analytics_profile_read",
    "analytics_identity_scan", "message_pii_scan", "attachment_pii_scan", "message_read", "support_ticket_read_completed",
    "audit_record_read_completed", "connector_config_read_completed", "connector_heartbeat", "connector_config_loaded",
    "restore_policy_read_completed", "restore_candidate_evaluated", "restore_authorization_completed",
    "export_download_check_completed", "export_metadata_read_completed", "contact_query_completed",
    "contact_lookup_completed", "deletion_marker_check", "retained_order_loaded", "record_lookup",
}
# Started events also carry resource ids in some formats; they are only used for trace keys.
KEY_ONLY_EVENTS = {"thumbnail_failed"}


def _interpret(evidence: EvidenceFile, line: LogLine, trace_keys: set[str]) -> Observation | None:
    f = line.fields
    resource_id = f.get("resource_id", "")
    when = parse_ts(line.ts)
    source = f"log:line {line.line_no}"
    if line.event == "record_lookup":
        return map_result_record(
            file_id=evidence.file_id, observed_at=when, system=f.get("system"), resource_id=resource_id,
            record_type=f.get("record_type", ""), result=f.get("result", ""), subject=f.get("subject_reference"), source=source,
        )

    record = {k: v for k, v in f.items() if k in CONTENT_FIELDS and k != "matched_value"}
    record["resource_id"] = resource_id
    for name in ("enabled", "restore_allowed", "message_text", "file", "available", "retention_until", "event_code", "connector_name", "last_delivery_status", "archive_key", "owner_email", "object_key", "email_opt_in", "scheduler_registered", "last_run", "reason", "access"):
        if name in f:
            record.setdefault(name, f[name])
    if "file" in f:
        record["file_name"] = f["file"]

    # Scanners report the identifier they matched inside the record.
    matched = classify_token(f.get("matched_value"))
    if matched:
        record[{"email": "email", "phone": "phone", "hash": "email_hash", "user": "customer_reference"}.get(matched[0], "email")] = f["matched_value"]

    presence = _presence(line)
    own_keys = {f.get(k) for k in KEY_FIELDS}
    keys = tokens_from(own_keys) | trace_keys
    # A successful key lookup with no content fields means the record contains that key.
    if presence == PRESENT and not any(k in f for k in CONTENT_FIELDS):
        for tok in sorted(keys):
            kind, value = tok.split(":", 1)
            record.setdefault({"email": "email", "phone": "phone", "hash": "email_hash", "user": "customer_reference"}[kind], value)

    collection = ""
    if line.event in ("message_pii_scan", "message_read", "support_ticket_read_completed"):
        collection = "support_conversations"
    elif line.event == "attachment_pii_scan":
        collection = "ticket_attachments"

    obs = map_record(record, file_id=evidence.file_id, observed_at=when, collection=collection, source=source, presence=presence or PRESENT)
    if obs is None:
        return None
    obs.lookup_keys |= keys
    obs.attributes["event"] = line.event

    # Event-specific state that the record fields alone do not express.
    if line.event in ("analytics_identity_scan", "message_pii_scan", "attachment_pii_scan") or f.get("direct_identifiers") == "1":
        obs.condition = IDENTIFIABLE
    elif line.event == "restore_candidate_evaluated":
        obs.condition = BLOCKED if _flag(f, "deletion_block") or _flag(f, "eligible") is False else RESTORABLE
    elif line.event == "restore_authorization_completed":
        obs.condition = BLOCKED if f.get("decision") == "deny" else RESTORABLE
    return obs


def parse_log(evidence: EvidenceFile) -> tuple[list[Observation], list[Coverage], list[str]]:
    lines, problems = [], []
    for line_no, raw in enumerate(evidence.path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        parsed = parse_line(line_no, raw)
        if parsed is None:
            problems.append(f"{evidence.file_id}: line {line_no} not parsed")
        else:
            lines.append(parsed)

    trace_keys: dict[str, set[str]] = defaultdict(set)
    for line in lines:
        if line.trace_id:
            trace_keys[line.trace_id] |= tokens_from(line.fields.get(k) for k in KEY_FIELDS)

    observations, unhandled = [], Counter()
    for line in lines:
        resource_id = line.fields.get("resource_id", "")
        if not resource_id or not RESOURCE_RE.fullmatch(resource_id):
            continue
        if line.event not in HANDLED_EVENTS:
            if not line.event.endswith("_started") and line.event not in KEY_ONLY_EVENTS:
                unhandled[line.event] += 1
            continue
        obs = _interpret(evidence, line, trace_keys.get(line.trace_id, set()))
        if obs:
            observations.append(obs)
    for event, n in unhandled.items():
        problems.append(f"{evidence.file_id}: {n} line(s) of unhandled event {event!r} with a resource_id")
    return observations, [], problems
