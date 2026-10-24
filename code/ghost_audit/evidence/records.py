"""JSON record exports and plain-text handover notes."""

import json
import re

from ..dataset import EvidenceFile, parse_ts
from .model import ABSENT, PRESENT, UNKNOWN, Coverage, Observation, map_record, map_result_record, system_for, tokens_from


def parse_json(evidence: EvidenceFile) -> tuple[list[Observation], list[Coverage], list[str]]:
    data = json.loads(evidence.path.read_text(encoding="utf-8"))
    if "snapshots" in data:
        return _snapshot_bundle(evidence, data)
    if "service_exports" in data:
        return _service_export(evidence, data)
    records = data.get("records")
    if isinstance(records, list) and records and "record_type" in records[0]:
        return _result_records(evidence, records), [], []
    if isinstance(records, list):
        return _plain_collection(evidence, data), [], []
    return _walk(evidence, data), [], [f"{evidence.file_id}: unrecognised JSON shape, used generic walker"]


def _ts(value, fallback):
    try:
        return parse_ts(value) if value else fallback
    except ValueError:
        return fallback


def _result_records(evidence: EvidenceFile, records: list[dict], default_system: str | None = None) -> list[Observation]:
    out = []
    for i, r in enumerate(records):
        out.append(
            map_result_record(
                file_id=evidence.file_id,
                observed_at=_ts(r.get("recorded_at"), evidence.captured_at),
                system=r.get("system") or default_system,
                resource_id=r["resource_id"],
                record_type=r.get("record_type", ""),
                result=r.get("result", ""),
                subject=r.get("subject_reference", r.get("lookup_value")),
                source=f"json:records[{i}]",
            )
        )
    return out


def _snapshot_bundle(evidence: EvidenceFile, data: dict):
    observations, coverage = [], []
    for s_index, snap in enumerate(data["snapshots"]):
        captured = _ts(snap.get("captured_at"), evidence.captured_at)
        store, collection = snap.get("data_store", ""), snap.get("collection", "")
        records = snap.get("records", [])
        if collection == "operational_record_history" or (records and "record_type" in records[0]):
            observations += _result_records(evidence, records)
            continue
        system_id = system_for("", store)
        # An explicit collection snapshot is a complete listing, even when empty.
        coverage.append(Coverage(evidence.file_id, captured, system_id, collection=collection, source=f"json:snapshots[{s_index}]"))
        for r_index, record in enumerate(records):
            obs = map_record(record, file_id=evidence.file_id, observed_at=captured, system_hint=store, collection=collection, source=f"json:snapshots[{s_index}].records[{r_index}]")
            if obs:
                observations.append(obs)
    return observations, coverage, []


def _service_export(evidence: EvidenceFile, data: dict):
    observations = []
    for service, collections in data["service_exports"].items():
        for collection, records in collections.items():
            for i, record in enumerate(records):
                when = _ts(record.get("observed_at"), evidence.captured_at)
                where = f"json:{service}.{collection}[{i}]"
                if collection == "profile_lookups":
                    status = int(record.get("status_code") or 0)
                    presence = PRESENT if status == 200 else ABSENT if status == 404 else UNKNOWN
                    obs = map_record({"resource_id": record["resource_id"], **(record.get("profile") or {})}, file_id=evidence.file_id, observed_at=when, system_hint=service, collection=collection, source=where, presence=presence)
                    obs.attributes["status_code"] = status
                    observations.append(obs)
                elif collection == "index_queries":
                    hits = record.get("hits") or {}
                    documents = hits.get("documents") or []
                    for doc in documents:
                        obs = map_record(doc, file_id=evidence.file_id, observed_at=when, system_hint=service, collection=collection, source=where, lookup_values=[record.get("query")])
                        if obs:
                            observations.append(obs)
                    if not documents and record.get("resource_id"):
                        observations.append(Observation(evidence.file_id, when, system_for(record["resource_id"], service), record["resource_id"], ABSENT, lookup_keys=tokens_from([record.get("query")]), source=where))
                else:
                    obs = map_record(record, file_id=evidence.file_id, observed_at=when, system_hint=service, collection=collection, source=where)
                    if obs:
                        observations.append(obs)
    return observations, [], []


def _plain_collection(evidence: EvidenceFile, data: dict) -> list[Observation]:
    """{captured_at, source, collection, records: [...]} single-collection exports."""
    when = _ts(data.get("captured_at"), evidence.captured_at)
    collection = data.get("collection", "")
    out = []
    for i, record in enumerate(data["records"]):
        obs = map_record(record, file_id=evidence.file_id, observed_at=when, system_hint=None, collection=collection, source=f"json:records[{i}]")
        if obs:
            out.append(obs)
    return out


def _walk(evidence: EvidenceFile, node, path="json") -> list[Observation]:
    out = []
    if isinstance(node, dict):
        if node.get("resource_id"):
            obs = map_record(node, file_id=evidence.file_id, observed_at=evidence.captured_at, source=path)
            if obs:
                out.append(obs)
        for key, value in node.items():
            out += _walk(evidence, value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            out += _walk(evidence, value, f"{path}[{i}]")
    return out


HANDOVER_LINE = re.compile(r"^\s*-\s*(?P<ts>\S+)\s*\|\s*(?P<system>[^|]+?)\s*\|\s*(?P<resource>[^|]+?)\s*\|\s*(?P<type>[^|]+?)\s*\|\s*(?P<result>[^|]+?)\s*\|\s*(?P<subject>.*?)\s*$")


def parse_text(evidence: EvidenceFile) -> tuple[list[Observation], list[Coverage], list[str]]:
    observations, problems = [], []
    for line_no, line in enumerate(evidence.path.read_text(encoding="utf-8").splitlines(), start=1):
        match = HANDOVER_LINE.match(line)
        if not match:
            continue
        observations.append(
            map_result_record(
                file_id=evidence.file_id,
                observed_at=_ts(match["ts"], evidence.captured_at),
                system=match["system"],
                resource_id=match["resource"],
                record_type=match["type"],
                result=match["result"],
                subject=match["subject"],
                source=f"txt:line {line_no}",
            )
        )
    if not observations:
        problems.append(f"{evidence.file_id}: no record lines recognised in text file")
    return observations, [], problems
