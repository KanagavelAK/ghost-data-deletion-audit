"""Inspect the normalized evidence table.

    python code/inspect_evidence.py                      # per-file summary -> runs/evidence_summary.md, runs/observations.jsonl
    python code/inspect_evidence.py --resource account_005
    python code/inspect_evidence.py --identifier leila.haddad@yahoo.com
    python code/inspect_evidence.py --gold-check         # can every sample answer step be traced to evidence?
    python code/inspect_evidence.py --case case_17       # identity + linked resources for one request
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ghost_audit import paths  # noqa: E402
from ghost_audit.context import ContextBuilder  # noqa: E402
from ghost_audit.dataset import load_all_requests, load_dataset, load_requests  # noqa: E402
from ghost_audit.intent import classify_intent  # noqa: E402
from ghost_audit.evidence import EvidenceStore, load_evidence  # noqa: E402
from ghost_audit.evidence.model import classify_token, token  # noqa: E402
from ghost_audit.output import read_output  # noqa: E402

# What the cited evidence must show for each gold operation.
EXPECTED = {
    "REPORT_REMAINING_RECORD": ("PRESENT", None),
    "REPORT_EXPOSED_PII": ("PRESENT", "IDENTIFIABLE"),
    "REPORT_ACTIVE_DATA_FLOW": ("PRESENT", "ACTIVE"),
    "REPORT_RESTORE_RISK": ("PRESENT", "RESTORABLE"),
    "VERIFY_RECORD_ABSENT": ("ABSENT", None),
    "VERIFY_RECORD_PRESENT": ("PRESENT", None),
    "VERIFY_RECORD_REDACTED": ("PRESENT", "REDACTED"),
    "VERIFY_RECORD_ANONYMIZED": ("PRESENT", "ANONYMIZED"),
    "VERIFY_DATA_FLOW_DISABLED": ("PRESENT", "DISABLED"),
    "VERIFY_RESTORE_BLOCKED": ("PRESENT", "BLOCKED"),
}


def summary(store: EvidenceStore, dataset) -> str:
    lines = ["# Evidence normalization summary", ""]
    lines.append(f"{len(store.observations)} observations, {len(store.coverage)} coverage entries, {len(store.by_resource)} distinct resources.")
    lines += ["", "| file | kind | captured_at | observations | coverage | resources | problems |", "| --- | --- | --- | --- | --- | --- | --- |"]
    per_file = Counter(o.file_id for o in store.observations)
    resources = {}
    for o in store.observations:
        resources.setdefault(o.file_id, set()).add(o.resource_id)
    for s in store.summaries:
        captured = dataset.evidence_by_id[s.file_id].captured_at.strftime("%Y-%m-%dT%H:%MZ")
        lines.append(f"| {s.file_id} | {s.kind} | {captured} | {per_file[s.file_id]} | {s.coverage} | {len(resources.get(s.file_id, ()))} | {'; '.join(s.problems)} |")
    lines += ["", "## Presence / condition", ""]
    for (presence, condition), n in Counter((o.presence, o.condition) for o in store.observations).most_common():
        lines.append(f"- {presence} / {condition}: {n}")
    return "\n".join(lines) + "\n"


def dump(store: EvidenceStore, path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for o in store.observations:
            row = {
                "file_id": o.file_id, "observed_at": o.observed_at.isoformat(), "system_id": o.system_id, "resource_id": o.resource_id,
                "presence": o.presence, "condition": o.condition, "category": o.category, "identifiers": sorted(o.identifiers),
                "lookup_keys": sorted(o.lookup_keys), "pii_fields": sorted(o.pii_fields), "copy": o.copy, "source": o.source,
                "attributes": {k: str(v) for k, v in o.attributes.items()},
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def show(observations) -> None:
    for o in observations:
        ids = ", ".join(sorted(o.identifiers)) or "-"
        keys = ", ".join(sorted(o.lookup_keys - o.identifiers))
        print(f"  {o.describe()}  ids[{ids}]" + (f" keys[{keys}]" if keys else "") + f"  ({o.source})")


def gold_check(store: EvidenceStore, dataset) -> int:
    requests = {r.case_id: r for r in load_requests(paths.SAMPLE_REQUESTS)}
    _, rows = read_output(paths.SAMPLE_GOLD)
    tally, details = Counter(), []
    for row in rows:
        audit_at = requests[row.case_id].audit_at
        for step in row.steps:
            rid, ev, op = step["resource_id"], step["evidence_id"], step["operation"]
            if op in ("DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD", "DISABLE_DATA_FLOW", "BLOCK_RESTORE"):
                continue
            seen = store.history(rid, audit_at)
            tally["steps"] += 1
            tally["resource seen in evidence <= audit_at" if seen else "resource NOT seen in evidence <= audit_at"] += 1
            if not ev:
                continue
            tally["cited steps"] += 1
            want_presence, want_condition = EXPECTED[op]
            in_file = [o for o in store.by_resource.get(rid, []) if o.file_id == ev]
            if in_file:
                latest = in_file[-1]
                ok = latest.presence == want_presence and (want_condition is None or latest.condition == want_condition)
                verdict = "cited file shows expected state" if ok else "cited file shows a different state"
                if not ok:
                    details.append(f"{row.case_id} {op} {rid} [{ev}]: file shows {latest.presence}/{latest.condition}")
            elif want_presence == "ABSENT" and any(c.file_id == ev and c.system_id == step["system_id"] for c in store.coverage):
                verdict = "cited file covers the system (absence by omission)"
            elif ev in dataset.evidence_by_id and dataset.evidence_by_id[ev].path.suffix == ".png":
                verdict = "cited file is a screenshot (not extracted yet)"
                details.append(f"{row.case_id} {op} {rid} [{ev}]: screenshot")
            else:
                verdict = "cited file does not mention the resource"
                details.append(f"{row.case_id} {op} {rid} [{ev}]: not found in cited file")
            tally[verdict] += 1
    print("Sample-answer traceability (findings and verifications):")
    for key in ("steps", "resource seen in evidence <= audit_at", "resource NOT seen in evidence <= audit_at", "cited steps",
                "cited file shows expected state", "cited file covers the system (absence by omission)",
                "cited file shows a different state", "cited file is a screenshot (not extracted yet)", "cited file does not mention the resource"):
        print(f"  {key}: {tally[key]}")
    if details:
        print("Details:")
        for d in details:
            print("  " + d)
    unseen = []
    for row in rows:
        audit_at = requests[row.case_id].audit_at
        for step in row.steps:
            if step["phase"] != "REMEDIATION" and not store.history(step["resource_id"], audit_at):
                unseen.append(f"{row.case_id}:{step['resource_id']}")
    if unseen:
        print("Resources with no evidence at or before audit_at: " + ", ".join(sorted(set(unseen))))
    return 0


def show_case(case_id: str, store: EvidenceStore, dataset) -> int:
    requests = {r.case_id: r for r in load_all_requests()}
    if case_id not in requests:
        print(f"unknown case {case_id}")
        return 1
    request = requests[case_id]
    ctx = ContextBuilder(dataset, store, list(requests.values())).build(request, classify_intent(request.request_text))
    ident = ctx.identity
    print(f"{case_id} {request.email} requested {request.requested_at:%Y-%m-%d} audit {request.audit_at:%Y-%m-%d} claim {request.reported_status}")
    print(f"intent: {ctx.intent.authorizes_deletion} ({ctx.intent.method}: {ctx.intent.reason})")
    print(f"user: {ident.user_id} {ident.name!r} via {ident.matched_via}; window_start {ctx.window_start}")
    for span in sorted(ident.spans, key=lambda s: (s.kind, str(s.valid_from))):
        print(f"  {span.kind:5s} {span.value}  from {span.valid_from or '-'} to {span.valid_to or '-'}  [{span.source}]")
    for rid, link in sorted(ctx.links.items()):
        print(f"linked {link.system_id} {rid} via {sorted(link.via)}")
        show(ctx.history[rid])
    for label, items in (("lookalike decoys", ctx.lookalikes), ("linked only after audit_at", ctx.later_only), ("linked only before window", ctx.earlier_only), ("catalog link contradicted by content", ctx.conflicting)):
        if items:
            print(f"{label}: {sorted(items)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--resource")
    parser.add_argument("--identifier")
    parser.add_argument("--gold-check", action="store_true")
    parser.add_argument("--case")
    args = parser.parse_args(argv)

    dataset = load_dataset()
    store = load_evidence(dataset)
    if args.case:
        return show_case(args.case, store, dataset)
    if args.resource:
        show(store.by_resource.get(args.resource, []))
        return 0
    if args.identifier:
        classified = classify_token(args.identifier)
        if not classified:
            print("not an identifier (email, user_N, sha256 hash or phone)")
            return 1
        show(sorted(store.by_identifier.get(token(*classified), []), key=lambda o: o.observed_at))
        return 0
    if args.gold_check:
        return gold_check(store, dataset)

    paths.RUNS_DIR.mkdir(exist_ok=True)
    (paths.RUNS_DIR / "evidence_summary.md").write_text(summary(store, dataset), encoding="utf-8")
    dump(store, paths.RUNS_DIR / "observations.jsonl")
    problems = store.problems
    print(f"{len(store.observations)} observations, {len(store.coverage)} coverage entries, {len(store.by_resource)} resources, {len(problems)} problem(s)")
    for p in problems:
        print("  " + p)
    print(f"summary -> {paths.RUNS_DIR / 'evidence_summary.md'}\nrows    -> {paths.RUNS_DIR / 'observations.jsonl'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
