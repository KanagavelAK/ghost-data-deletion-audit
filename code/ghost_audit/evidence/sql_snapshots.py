"""SQLite-compatible SQL snapshot exports.

Topologies seen: inventory (typed tables + snapshot_scope), primary/failover
profile tables, primary-only search documents, and generic record_json tables
for primary, read, reporting, failover and regional replicas.
"""

import json
import re
import sqlite3
from collections import defaultdict

from ..dataset import EvidenceFile, parse_ts
from .model import Coverage, Observation, map_record, system_for

# Typed tables -> logical system.
TABLE_SYSTEMS = {
    "customer_profiles": "account_service",
    "customer_search_documents": "search_service",
    "search_documents": "search_service",
    "customer_analytics_profiles": "analytics_platform",
    "support_records": "support_platform",
    "customer_exports": "export_service",
    "connector_subscriptions": "integration_service",
    "backup_restore_entries": "backup_service",
    "security_audit_records": "security_service",
}
COPY_SUFFIXES = ("primary", "failover_replica", "read_replica", "reporting_replica")


def _split_table(table: str) -> tuple[str, str]:
    """'customer_profiles_failover_replica' -> ('customer_profiles', 'failover_replica')."""
    for suffix in COPY_SUFFIXES:
        if table.endswith("_" + suffix):
            return table[: -len(suffix) - 1], suffix
    match = re.match(r"(.+?)_(replica_[a-z_]+)$", table)
    if match:
        return match.group(1), match.group(2)
    return table, ""


def parse_sql(evidence: EvidenceFile) -> tuple[list[Observation], list[Coverage], list[str]]:
    script = evidence.path.read_text(encoding="utf-8")
    con = sqlite3.connect(":memory:")
    try:
        con.executescript(script)
    except sqlite3.Error as exc:
        return [], [], [f"{evidence.file_id}: SQL failed to load: {exc}"]
    con.row_factory = sqlite3.Row

    captured = evidence.captured_at
    header = re.search(r"--\s*Captured at:\s*(\S+)", script)
    problems = []
    if header and parse_ts(header.group(1)) != captured:
        problems.append(f"{evidence.file_id}: header capture time {header.group(1)} differs from manifest")

    observations: list[Observation] = []
    coverage: list[Coverage] = []
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]

    if "snapshot_scope" in tables:
        for row in con.execute("SELECT origin_system, captured_at FROM snapshot_scope"):
            coverage.append(Coverage(evidence.file_id, parse_ts(row["captured_at"]), system_for("", row["origin_system"]), source="sql:snapshot_scope"))

    for table in tables:
        if table == "snapshot_scope":
            continue
        base, copy = _split_table(table)
        columns = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
        systems = defaultdict(int)
        for row in con.execute(f'SELECT * FROM "{table}"'):
            row = dict(row)
            if "record_json" in columns:
                record = json.loads(row["record_json"])
                record.setdefault("resource_id", row["resource_id"])
                record["updated_at"] = row.get("updated_at")
                hint, collection = row.get("origin_system"), row.get("collection_name", "")
            else:
                record, hint, collection = row, TABLE_SYSTEMS.get(base), base
            obs = map_record(
                record,
                file_id=evidence.file_id,
                observed_at=captured,
                system_hint=hint,
                collection=collection,
                copy=copy,
                source=f"sql:{table}",
            )
            if obs:
                observations.append(obs)
                systems[obs.system_id] += 1

        # Without a declared scope, a copy's table is the best available listing:
        # a single-system table dump lists that system; a record_json table is a
        # snapshot of the included customers' data across every system ("*").
        if "snapshot_scope" not in tables:
            if "record_json" in columns:
                coverage.append(Coverage(evidence.file_id, captured, "*", copy=copy, collection=base, strength="mixed", source=f"sql:{table}"))
            elif len(systems) == 1:
                coverage.append(Coverage(evidence.file_id, captured, next(iter(systems)), copy=copy, collection=base, strength="table", source=f"sql:{table}"))
    con.close()
    return observations, coverage, problems
