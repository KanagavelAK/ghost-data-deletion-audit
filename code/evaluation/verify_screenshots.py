"""Check cached screenshot extractions against facts read from each image by a person.

    python code/evaluation/verify_screenshots.py

Each expected record lists the identifiers that must be transcribed exactly. The
script reports missing records, wrong or missing identifiers, wrong presence or
condition, and records the model invented. Re-run it after re-extracting with a
different provider or model.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ghost_audit.dataset import load_dataset  # noqa: E402
from ghost_audit.evidence.catalog_links import catalog_aliases  # noqa: E402
from ghost_audit.evidence.images import load_cache, resolve_resource, snap_emails  # noqa: E402
from ghost_audit.evidence.model import classify_token  # noqa: E402

# Human-read ground truth: resource -> identifiers that must appear, plus the expected condition
# (None when the screen does not show one). Records without a Northstar id or catalog alias
# (order rows, unnamed connectors, other files in a bucket) are not expected.
EXPECTED = {
    "evidence_04": {  # account admin console, Haruka Sato
        "account_5762": ({"haruka.s1838@zoho.com", "user_11", "+81 90 7610 2507"}, "IDENTIFIABLE"),
        "support_9538": (set(), None),
    },
    "evidence_09": {  # search inspector: the subject's document and a .jr look-alike
        "search_428": ({"g.silva76@gmail.com", "user_13"}, "IDENTIFIABLE"),
        "search_1874": ({"g.silva76.jr@gmail.com"}, None),
    },
    "evidence_14": {  # connector console
        "data_flow_1121": ({"jain.aditi@gmail.com"}, "ACTIVE"),
        "data_flow_9846": ({"lucia-torres@gmail.com"}, "ACTIVE"),
    },
    "evidence_19": {  # analytics profile view
        "analytics_7438": ({"darwish.omar@icloud.com", "user_20"}, "IDENTIFIABLE"),
    },
    "evidence_23": {  # export console
        "export_1258": ({"daniel.park@northstar.shop"}, None),
        "export_1259": ({"sara.kim@northstar.shop"}, None),
        "export_1260": ({"ingrid.dahl@outlook.com"}, None),
        "export_1261": ({"marcus.lee@northstar.shop"}, None),
    },
    "evidence_28": {  # support ticket console
        "support_1034": ({"kagyeman8947@fastmail.com"}, "IDENTIFIABLE"),
    },
    "evidence_32": {  # backup console (no restore state shown)
        "backup_9286": ({"summit-retail@company.com"}, None),
        "backup_9287": ({"areed1433@gmail.com"}, None),
        "backup_9288": ({"miller.logistics@company.com"}, None),
        "backup_9291": ({"bluepeakco@outlook.com"}, None),
    },
    "evidence_37": {  # security audit explorer, user_03
        **{f"audit_record_00{i}": ({"user_03"}, None) for i in range(1, 7)},
    },
    "evidence_42": {  # retained customer reference (catalog alias user_17), no personal fields shown
        "tombstone_17": ({"user_17"}, None),
    },
    "evidence_46": {  # object storage: only delivery_confirmation_19.pdf has a catalog alias
        "object_19": (set(), None),
    },
    "evidence_60": {  # marketing console: marketing_001 shows a different person
        "marketing_001": ({"sj.park@naver.com", "+82 10-5837-9451"}, "IDENTIFIABLE"),
    },
}
# Conditions that are acceptable alternatives to the expected one.
EQUIVALENT = {"IDENTIFIABLE": {"IDENTIFIABLE"}, "ACTIVE": {"ACTIVE"}}


def normalized(values) -> set[str]:
    out = set()
    for value in values:
        token = classify_token(value)
        out.add(f"{token[0]}:{token[1]}" if token else str(value).strip().lower())
    return out


def main() -> int:
    cache = load_cache()
    dataset = load_dataset()
    aliases = catalog_aliases(dataset)
    known = {u.email.lower() for u in dataset.identities} | {a.old_value.lower() for a in dataset.audit_log if a.field_name == "email"}
    problems, notes, checked = [], [], 0
    for file_id, expected in EXPECTED.items():
        entry = cache.get(file_id)
        if entry is None:
            problems.append(f"{file_id}: not extracted")
            continue
        records = {}
        for rec in entry["extraction"].get("records", []):
            rid = resolve_resource(rec, aliases)
            if rid:
                # Check the values the pipeline uses: emails after snapping transcription slips.
                emails, corrections = snap_emails(rec.get("emails") or [], known)
                notes += [f"{file_id}: {rid} model read {raw!r}, corrected to {fixed!r}" for raw, fixed in corrections.items()]
                shown = normalized(emails + (rec.get("user_ids") or []) + (rec.get("phones") or []))
                records.setdefault(rid, {"ids": set(), "condition": rec.get("condition"), "presence": rec.get("presence")})["ids"] |= shown
        for rid, (identifiers, condition) in expected.items():
            checked += 1
            got = records.get(rid)
            if got is None:
                problems.append(f"{file_id}: {rid} missing")
                continue
            missing = normalized(identifiers) - got["ids"]
            if missing:
                problems.append(f"{file_id}: {rid} lacks {sorted(missing)} (got {sorted(got['ids'])})")
            if got["presence"] not in (None, "PRESENT"):
                problems.append(f"{file_id}: {rid} presence {got['presence']}, screen shows the record")
            if condition and got["condition"] not in EQUIVALENT.get(condition, {condition}):
                problems.append(f"{file_id}: {rid} condition {got['condition']}, expected {condition}")
        for rid in sorted(set(records) - set(expected)):
            problems.append(f"{file_id}: unexpected record {rid}")
    print(f"checked {checked} expected records across {len(EXPECTED)} screenshots")
    for note in notes:
        print("  corrected: " + note)
    for problem in problems:
        print("  " + problem)
    print("all screenshot facts match" if not problems else f"{len(problems)} problem(s)")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
