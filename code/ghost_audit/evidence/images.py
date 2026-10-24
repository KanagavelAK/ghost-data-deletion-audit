"""Admin-console screenshots, read once with a vision model and cached.

Extraction is a separate, explicit step (python code/extract_images.py) so the
main pipeline is deterministic and makes no vision calls: it only reads the
cache. Each cache entry stores the image's sha256 so a changed file is re-read.

Vision models downscale large screenshots, which costs digits in IDs and phone
numbers, so each request carries the full screen plus two zoomed halves.
Records a screen shows only by a display reference (a file name, a customer
key) are mapped back to their resource through the data-catalog aliases.
"""

import hashlib
import io
import json
from datetime import datetime, timezone

from .. import config, paths
from ..dataset import EvidenceFile
from ..llm import call_llm, extract_json, image_block, text_block
from .model import (
    ABSENT,
    ACTIVE,
    ANONYMIZED,
    BLOCKED,
    DISABLED,
    IDENTIFIABLE,
    PRESENT,
    REDACTED,
    RESOURCE_RE,
    RESTORABLE,
    UNKNOWN,
    Coverage,
    Observation,
    map_record,
    tokens_from,
)

CACHE_PATH = paths.REPO_ROOT / "code" / "cache" / "image_extractions.json"
CONDITIONS = {IDENTIFIABLE, ANONYMIZED, REDACTED, ACTIVE, DISABLED, RESTORABLE, BLOCKED}
CONTENT_CONDITIONS = {IDENTIFIABLE, ANONYMIZED, REDACTED}
CATEGORY_CONDITIONS = {"DATA_FLOW": {ACTIVE, DISABLED}, "BACKUP_ARCHIVE": {RESTORABLE, BLOCKED}}

PROMPT = """You are reading a screenshot of an internal Northstar Shop admin console for a personal-data deletion audit.
Image 1 is the full screen. Images 2 and 3 are zoomed left and right halves of the same screen:
use them to read small text exactly. Copy IDs, emails and phone numbers character by character; never guess.

Return ONLY a JSON object with this shape:
{
  "screen": "<console name and what it shows, one sentence>",
  "records": [
    {
      "resource_id": "<Northstar record id of the form name_number exactly as shown, e.g. account_5762, search_428, support_9538, backup_9287, data_flow_1121, audit_record_003, tombstone_12, export_1260, analytics_7438, marketing_001, object_19; null if the screen shows no such id for this record>",
      "reference": "<the record's other visible key if any: file name, customer key such as user_17, ticket number such as CS-8538, statement number; else null>",
      "system": "<account_service | search_service | analytics_platform | support_platform | export_service | integration_service | backup_service | security_service | identity_service | marketing_platform | object_storage | legal_service | unknown>",
      "record_type": "<USER_PROFILE | SEARCH_INDEX_ENTRY | ANALYTICS_PROFILE | SUPPORT_MESSAGE | SUPPORT_ATTACHMENT | USER_EXPORT | DATA_FLOW | BACKUP_ARCHIVE | SECURITY_AUDIT_LOG | ACCOUNT_TOMBSTONE | MARKETING_PROFILE | LEGAL_HOLD_RECORD | unknown>",
      "presence": "<PRESENT | ABSENT | UNKNOWN>",
      "condition": "<IDENTIFIABLE | ANONYMIZED | REDACTED | ACTIVE | DISABLED | RESTORABLE | BLOCKED | null>",
      "status_text": "<status/state text shown for this record, verbatim, or null>",
      "emails": ["<every email shown for this record>"],
      "user_ids": ["<user_N references shown for this record>"],
      "phones": ["<phone numbers shown for this record>"],
      "names": ["<person names shown for this record>"],
      "shown_time": "<date/time shown for this record, verbatim, or null>"
    }
  ],
  "page_identifiers": {"emails": [], "user_ids": [], "phones": [], "names": [], "resource_ids": []},
  "timeline": [{"when": "<as shown>", "text": "<verbatim>", "resource_ids": []}]
}

Rules:
- Include every row of every table and the record in any detail panel, listed once with all of its identifiers.
- Order numbers (ORD-...), invoice and finance references are not records; leave them out of "records".
- When the page is about one customer, put that customer's identifiers on each record that belongs to them.
- A page about one customer key (for example user_17) with no name_number id is still a record: set resource_id null and reference to that key.
- condition: IDENTIFIABLE when the record still shows a name, email, phone or email hash; ANONYMIZED or REDACTED when those fields are blank, masked or marked so; ACTIVE or DISABLED for connectors and data flows (enabled, healthy, continuous sync = ACTIVE; paused, disabled, stopped = DISABLED); RESTORABLE or BLOCKED for backups (restore allowed vs restore blocked). Use null when the screen does not say.
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


def screenshot_views(path) -> list[bytes]:
    """The full screenshot plus zoomed, overlapping left and right halves (navigation sidebar dropped)."""
    try:
        from PIL import Image
    except ImportError:
        return [path.read_bytes()]
    image = Image.open(path).convert("RGB")
    width, height = image.size
    left, overlap = int(width * 0.12), int(width * 0.05)
    middle = (left + width) // 2
    views = [path.read_bytes()]
    for box in ((left, 0, middle + overlap, height), (middle - overlap, 0, width, height)):
        buffer = io.BytesIO()
        image.crop(box).save(buffer, format="PNG")
        views.append(buffer.getvalue())
    return views


def extract_image(evidence: EvidenceFile, model: str | None = None, provider: str | None = None) -> dict:
    provider = provider or config.llm_provider()
    model = model or config.model_for(provider, "vision")
    blocks = [image_block(view, "image/png") for view in screenshot_views(evidence.path)]
    reply = call_llm(
        blocks + [text_block(PROMPT)],
        purpose=f"image_extraction:{evidence.file_id}",
        task="vision",
        provider=provider,
        model=model,
        max_tokens=4000,
        json_mode=True,
    )
    return {
        "file": evidence.file_path,
        "sha256": _sha256(evidence.path),
        "provider": provider,
        "model": model,
        "views": len(blocks),
        "extracted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "extraction": extract_json(reply),
    }


def snap_emails(emails: list[str], known: set[str], tolerance: int = 2) -> tuple[list[str], dict[str, str]]:
    """Correct transcription slips: an unknown email within `tolerance` edits of exactly one known email
    is replaced by it. Look-alike decoys ('name.jr@...') are 3+ edits away and are never snapped."""
    from ..context import edit_distance  # local import: context imports the evidence package

    out, corrections = [], {}
    for email in emails:
        value = str(email).strip().lower()
        if value in known or not known:
            out.append(value)
            continue
        close = [k for k in known if abs(len(k) - len(value)) <= tolerance and edit_distance(k, value) <= tolerance]
        if len(close) == 1:
            corrections[value] = close[0]
            out.append(close[0])
        else:
            out.append(value)
    return out, corrections


def resolve_resource(rec: dict, aliases: dict[str, str]) -> str | None:
    """The record's resource id, directly or through a catalog alias of its visible reference."""
    resource_id = str(rec.get("resource_id") or "").strip()
    if RESOURCE_RE.fullmatch(resource_id):
        return resource_id
    for candidate in (rec.get("reference"), resource_id):
        if candidate and str(candidate).strip().lower() in aliases:
            return aliases[str(candidate).strip().lower()]
    return None


def parse_image(
    evidence: EvidenceFile, cache: dict, aliases: dict[str, str] | None = None, known_emails: set[str] = frozenset()
) -> tuple[list[Observation], list[Coverage], list[str]]:
    """aliases: catalog display references -> resource id; known_emails: emails of known people (for snapping)."""
    entry = cache.get(evidence.file_id)
    if entry is None:
        return [], [], [f"{evidence.file_id}: screenshot not extracted yet (run python code/extract_images.py)"]
    problems = []
    if entry.get("sha256") != _sha256(evidence.path):
        problems.append(f"{evidence.file_id}: screenshot changed since extraction; re-run extract_images.py")

    observations = []
    for i, rec in enumerate(entry["extraction"].get("records", [])):
        resource_id = resolve_resource(rec, aliases or {})
        if resource_id is None:
            continue
        emails, users, phones, names = (rec.get(k) or [] for k in ("emails", "user_ids", "phones", "names"))
        emails, corrections = snap_emails(emails, set(known_emails))
        record = {
            "resource_id": resource_id,
            "email": emails[0] if emails else None,
            "customer_reference": users[0] if users else None,
            "phone": phones[0] if phones else None,
            "full_name": names[0] if names else None,
        }
        presence = rec.get("presence") if rec.get("presence") in (PRESENT, ABSENT, UNKNOWN) else PRESENT
        system = rec.get("system") if rec.get("system") not in (None, "", "unknown") else None
        # The screen shows the state at capture time; dates on screen describe the record, not the capture.
        obs = map_record(record, file_id=evidence.file_id, observed_at=evidence.captured_at, system_hint=system, source=f"image:records[{i}]", presence=presence)
        if obs is None:
            continue
        obs.identifiers |= tokens_from(emails + users + phones)
        if rec.get("record_type") not in (None, "", "unknown"):
            obs.category = rec["record_type"]
        # Accept the model's condition only when it fits the record type: a tombstone or profile
        # cannot be "ACTIVE" (that is a data-flow state), whatever status the screen shows next to it.
        condition = rec.get("condition")
        if condition in CONDITIONS and presence == PRESENT and condition in CATEGORY_CONDITIONS.get(obs.category, CONTENT_CONDITIONS):
            obs.condition = condition
        for name in ("status_text", "reference", "shown_time"):
            if rec.get(name):
                obs.attributes[name] = rec[name]
        if corrections:
            obs.attributes["corrected_emails"] = corrections
        observations.append(obs)
    return observations, [], problems
