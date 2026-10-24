"""Admin-console screenshots, read once with a vision model and cached.

Extraction is a separate, explicit step (python code/extract_images.py) so the
main pipeline is deterministic and makes no vision calls: it only reads the
cache. Each cache entry stores the image's sha256 so a changed file is re-read.
"""

import base64
import hashlib
import json
from datetime import datetime, timezone

from .. import config, paths
from ..dataset import EvidenceFile, parse_ts
from ..llm import call_claude, extract_json
from .model import (
    ABSENT,
    ACTIVE,
    ANONYMIZED,
    BLOCKED,
    DISABLED,
    IDENTIFIABLE,
    PRESENT,
    REDACTED,
    RESTORABLE,
    UNKNOWN,
    Coverage,
    Observation,
    map_record,
    tokens_from,
)

CACHE_PATH = paths.REPO_ROOT / "code" / "cache" / "image_extractions.json"
CONDITIONS = {IDENTIFIABLE, ANONYMIZED, REDACTED, ACTIVE, DISABLED, RESTORABLE, BLOCKED}

PROMPT = """You are reading a screenshot of an internal Northstar Shop admin console for a personal-data deletion audit.
Transcribe what is visible; do not guess values that are not shown.

Return ONLY a JSON object with this shape:
{
  "screen": "<console name and what it shows, one sentence>",
  "records": [
    {
      "resource_id": "<id exactly as shown, e.g. account_5762, search_9729, support_9538, backup_9287, data_flow_1121, audit_record_003, tombstone_12, export_77, analytics_19, marketing_001, object_19>",
      "system": "<account_service | search_service | analytics_platform | support_platform | export_service | integration_service | backup_service | security_service | identity_service | marketing_platform | object_storage | legal_service | unknown>",
      "record_type": "<USER_PROFILE | SEARCH_INDEX_ENTRY | ANALYTICS_PROFILE | SUPPORT_MESSAGE | SUPPORT_ATTACHMENT | USER_EXPORT | DATA_FLOW | BACKUP_ARCHIVE | SECURITY_AUDIT_LOG | ACCOUNT_TOMBSTONE | MARKETING_PROFILE | LEGAL_HOLD_RECORD | unknown>",
      "presence": "<PRESENT | ABSENT | UNKNOWN>",
      "condition": "<IDENTIFIABLE | ANONYMIZED | REDACTED | ACTIVE | DISABLED | RESTORABLE | BLOCKED | null>",
      "status_text": "<status/state text shown for this record, verbatim, or null>",
      "emails": ["<every email shown for this record>"],
      "user_ids": ["<user_N references shown for this record>"],
      "phones": ["<phone numbers shown for this record>"],
      "names": ["<person names shown for this record>"],
      "observed_at": "<ISO-8601 timestamp shown for this record, or null>"
    }
  ],
  "page_identifiers": {"emails": [], "user_ids": [], "phones": [], "names": [], "resource_ids": []},
  "timeline": [{"when": "<as shown>", "text": "<verbatim>", "resource_ids": []}]
}

Rules:
- Include every row of every table and the record in any detail panel. A record that appears in several places is listed once with all of its identifiers.
- When the page is about one customer, put that customer's identifiers on each record that belongs to them.
- condition: IDENTIFIABLE when the record still shows a name, email, phone or email hash; ANONYMIZED/REDACTED when those fields are blank, masked or marked so; ACTIVE/DISABLED for connectors or data flows (enabled, healthy, continuous sync = ACTIVE); RESTORABLE/BLOCKED for backups (restore allowed vs restore blocked). Use null when the screenshot does not say.
- presence: ABSENT only when the screen says the record is deleted, missing or not found."""


def _sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_cache() -> dict:
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    return {}


def save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def extract_image(evidence: EvidenceFile, model: str | None = None) -> dict:
    model = model or config.vision_model()
    data = base64.standard_b64encode(evidence.path.read_bytes()).decode("ascii")
    reply = call_claude(
        [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}},
            {"type": "text", "text": PROMPT},
        ],
        purpose=f"image_extraction:{evidence.file_id}",
        model=model,
        max_tokens=4000,
    )
    return {
        "file": evidence.file_path,
        "sha256": _sha256(evidence.path),
        "model": model,
        "extracted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "extraction": extract_json(reply),
    }


def parse_image(evidence: EvidenceFile, cache: dict) -> tuple[list[Observation], list[Coverage], list[str]]:
    entry = cache.get(evidence.file_id)
    if entry is None:
        return [], [], [f"{evidence.file_id}: screenshot not extracted yet (run python code/extract_images.py)"]
    problems = []
    if entry.get("sha256") != _sha256(evidence.path):
        problems.append(f"{evidence.file_id}: screenshot changed since extraction; re-run extract_images.py")

    observations = []
    for i, rec in enumerate(entry["extraction"].get("records", [])):
        resource_id = str(rec.get("resource_id") or "").strip()
        if not resource_id:
            continue
        emails, users, phones, names = (rec.get(k) or [] for k in ("emails", "user_ids", "phones", "names"))
        record = {
            "resource_id": resource_id,
            "email": emails[0] if emails else None,
            "customer_reference": users[0] if users else None,
            "phone": phones[0] if phones else None,
            "full_name": names[0] if names else None,
            "status_text": rec.get("status_text"),
        }
        presence = rec.get("presence") if rec.get("presence") in (PRESENT, ABSENT, UNKNOWN) else PRESENT
        try:
            when = parse_ts(rec["observed_at"]) if rec.get("observed_at") else evidence.captured_at
        except ValueError:
            when = evidence.captured_at
        system = rec.get("system") if rec.get("system") not in (None, "", "unknown") else None
        obs = map_record(record, file_id=evidence.file_id, observed_at=when, system_hint=system, source=f"image:records[{i}]", presence=presence)
        if obs is None:
            continue
        obs.identifiers |= tokens_from(emails + users + phones)
        if rec.get("record_type") not in (None, "", "unknown"):
            obs.category = rec["record_type"]
        if rec.get("condition") in CONDITIONS and presence == PRESENT:
            obs.condition = rec["condition"]
        if rec.get("status_text"):
            obs.attributes["status_text"] = rec["status_text"]
        observations.append(obs)
    return observations, [], problems
