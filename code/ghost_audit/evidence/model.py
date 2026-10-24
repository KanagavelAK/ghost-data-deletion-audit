"""Common observation model shared by every evidence parser.

Every parser reduces its format to the same two things:

* Observation - "at time T, file F showed resource R in system S in this state,
  linked to these identifiers".
* Coverage - "file F holds a complete listing of system S at time T", so a
  resource missing from it can be treated as absent.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime

PRESENT, ABSENT, UNKNOWN = "PRESENT", "ABSENT", "UNKNOWN"

# Content conditions (None when the format says nothing beyond presence).
IDENTIFIABLE, ANONYMIZED, REDACTED = "IDENTIFIABLE", "ANONYMIZED", "REDACTED"
ACTIVE, DISABLED = "ACTIVE", "DISABLED"  # data flows
RESTORABLE, BLOCKED = "RESTORABLE", "BLOCKED"  # backups

# Resource-id prefix -> (logical system, default data category).
PREFIXES = {
    "account": ("account_service", "USER_PROFILE"),
    "search": ("search_service", "SEARCH_INDEX_ENTRY"),
    "analytics": ("analytics_platform", "ANALYTICS_PROFILE"),
    "support": ("support_platform", None),  # message vs attachment depends on the record
    "export": ("export_service", "USER_EXPORT"),
    "data_flow": ("integration_service", "DATA_FLOW"),
    "backup": ("backup_service", "BACKUP_ARCHIVE"),
    "audit_record": ("security_service", "SECURITY_AUDIT_LOG"),
    "tombstone": ("identity_service", "ACCOUNT_TOMBSTONE"),
    "legal_hold": ("legal_service", "LEGAL_HOLD_RECORD"),
    "marketing": ("marketing_platform", "MARKETING_PROFILE"),
    "cache": ("cache_service", "CACHE_ENTRY"),
    "email_delivery": ("email_delivery_service", "EMAIL_DELIVERY_LOG"),
    "object": ("object_storage", None),
}
RESOURCE_RE = re.compile(r"\b(" + "|".join(sorted(PREFIXES, key=len, reverse=True)) + r")_\d+\b")

# Physical copies and services that report under a logical system.
SYSTEM_ALIASES = {
    "account_failover_replica": "account_service",
    "account_replication_cluster": "account_service",
    "search_index_service": "search_service",
    "analytics_identity_store": "analytics_platform",
    "user_export_store": "export_service",
    "object_store": "object_storage",
}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
USER_RE = re.compile(r"^user_\d+$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
PHONE_RE = re.compile(r"^\+?[\d\s().-]{7,}$")

NULLS = {"", "null", "none", "n/a", "no direct identifier", "-"}


def resource_prefix(resource_id: str) -> str:
    match = RESOURCE_RE.fullmatch(resource_id or "")
    return match.group(1) if match else ""


def system_for(resource_id: str, hint: str | None = None) -> str:
    """Logical system id, preferring an explicit hint over the resource prefix."""
    if hint:
        hint = SYSTEM_ALIASES.get(hint, hint)
        if hint != "unknown":
            return hint
    return PREFIXES.get(resource_prefix(resource_id), ("unknown", None))[0]


def default_category(resource_id: str) -> str | None:
    return PREFIXES.get(resource_prefix(resource_id), (None, None))[1]


def normalize_phone(value: str) -> str:
    return re.sub(r"\D", "", value)


def classify_token(value) -> tuple[str, str] | None:
    """Return (kind, normalized) for an identity-like value, or None.

    Kinds: email, user, hash, phone, resource. Emails are lower-cased because
    the dataset mixes case (SIPHO.MOKOENA@GMAIL.COM).
    """
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if text.lower() in NULLS:
        return None
    if EMAIL_RE.fullmatch(text):
        return "email", text.lower()
    if USER_RE.match(text):
        return "user", text
    if HASH_RE.match(text.lower()):
        return "hash", text.lower()
    if RESOURCE_RE.fullmatch(text):
        return "resource", text
    if PHONE_RE.match(text) and sum(c.isdigit() for c in text) >= 7:
        return "phone", normalize_phone(text)
    return None


def token(kind: str, value: str) -> str:
    """Identifier tokens are 'kind:value' so emails, hashes and user ids never collide."""
    return f"{kind}:{value}"


def tokens_from(values) -> set[str]:
    out = set()
    for value in values:
        classified = classify_token(value)
        if classified and classified[0] != "resource":
            out.add(token(*classified))
    return out


def emails_in_text(text: str | None) -> set[str]:
    return {token("email", e.lower()) for e in EMAIL_RE.findall(text or "")}


@dataclass
class Observation:
    file_id: str
    observed_at: datetime
    system_id: str
    resource_id: str
    presence: str
    condition: str | None = None
    category: str | None = None
    # Identity tokens found in the record's own content.
    identifiers: set[str] = field(default_factory=set)
    # Identity tokens used to look the record up (trace keys, lookup_value, query).
    lookup_keys: set[str] = field(default_factory=set)
    # Which identifying fields are populated: name, email, phone, email_hash, user_ref.
    pii_fields: set[str] = field(default_factory=set)
    copy: str = ""  # primary / failover_replica / replica_eu_west / ... ; "" when not applicable
    source: str = ""  # parser and location, for traceability
    attributes: dict = field(default_factory=dict)
    # When the file itself was captured (manifest). A log line can describe an event before
    # audit_at inside a file captured after it: usable for reasoning, but not citable.
    captured_at: datetime | None = None

    @property
    def all_identifiers(self) -> set[str]:
        return self.identifiers | self.lookup_keys

    def citable_by(self, moment: datetime) -> bool:
        return self.captured_at is not None and self.captured_at <= moment

    def describe(self) -> str:
        bits = [self.observed_at.strftime("%Y-%m-%dT%H:%M:%SZ"), self.file_id, self.system_id, self.resource_id, self.presence]
        if self.condition:
            bits.append(self.condition)
        if self.category:
            bits.append(self.category)
        if self.copy:
            bits.append(f"copy={self.copy}")
        return " ".join(bits)


@dataclass
class Coverage:
    """A complete listing of one system (optionally one copy) at a point in time."""

    file_id: str
    observed_at: datetime
    system_id: str
    copy: str = ""
    collection: str = ""
    # declared: the file states its scope (snapshot_scope, explicit collection, even if empty);
    #           these exports are curated per customer, so they prove absence only for customers they include
    # table: a full single-system database table dump; proves absence for anyone
    # mixed: the system appears in a multi-system snapshot table; like declared, proof only for customers it includes
    strength: str = "declared"
    source: str = ""
    captured_at: datetime | None = None  # file capture time (manifest)


# --- record mapping shared by SQL rows, JSON records and screenshot extractions ---

NAME_FIELDS = ("full_name", "display_name", "name", "requester_name")
EMAIL_FIELDS = ("email", "owner_email", "requester_email", "customer_email", "uploaded_by", "subject_selector")
PHONE_FIELDS = ("phone", "owner_phone", "requester_phone")
HASH_FIELDS = ("email_hash",)
REF_FIELDS = ("user_id", "customer_reference", "subject_reference", "subject_ref")
TEXT_FIELDS = ("message_text", "message_excerpt")
KIND_TO_PII = {"email": "email", "user": "user_ref", "hash": "email_hash", "phone": "phone"}
KEPT_ATTRIBUTES = (
    "account_status", "enabled", "restore_allowed", "last_delivery_status", "connector_name", "destination", "mode",
    "file_name", "storage_class", "download_available", "available", "expires_at", "created_at", "retention_until",
    "retained_until", "retention_reason", "event_code", "event_name", "ticket_id", "index_name", "source_resource_id",
    "email_opt_in", "archive_key", "archive_path", "object_key", "message_text", "message_excerpt", "updated_at",
    "audience_cohort", "channel", "order_reference", "included_sections",
)


def _populated(value) -> bool:
    return value is not None and not isinstance(value, bool) and str(value).strip().lower() not in NULLS


def _flatten(record: dict) -> dict:
    """Merge one level of nesting (search 'document', lookup 'profile') into the record."""
    flat = {}
    for key, value in record.items():
        if isinstance(value, dict) and key in ("document", "profile"):
            for k, v in value.items():
                flat.setdefault(k, v)
        else:
            flat[key] = value
    return flat


def _truthy(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "allowed"):
        return True
    if text in ("0", "false", "no", "denied"):
        return False
    return None


def infer_support_category(collection: str, record: dict) -> str | None:
    hint = f"{collection} {record.get('storage_class', '')} {record.get('file_name', '')}".lower()
    if "attachment" in hint or record.get("file_name") or record.get("uploaded_by"):
        return "SUPPORT_ATTACHMENT"
    if "conversation" in hint or "ticket" in hint or any(k in record for k in TEXT_FIELDS) or "support_records" in hint:
        return "SUPPORT_MESSAGE"
    return None


def content_condition(category: str | None, pii: set[str], record: dict) -> str | None:
    """Condition implied by a record's content, by data category."""
    if category == "DATA_FLOW":
        enabled = _truthy(record.get("enabled"))
        return None if enabled is None else (ACTIVE if enabled else DISABLED)
    if category == "BACKUP_ARCHIVE":
        allowed = _truthy(record.get("restore_allowed"))
        return None if allowed is None else (RESTORABLE if allowed else BLOCKED)
    direct = pii - {"user_ref"}
    if category == "ANALYTICS_PROFILE":
        # Rule 07: anonymized only when nothing still attributes it to a user.
        return IDENTIFIABLE if pii else ANONYMIZED
    if category in ("SUPPORT_MESSAGE", "ACCOUNT_TOMBSTONE"):
        # The stable subject reference may remain; direct identifiers may not.
        if str(record.get("message_text", "")).strip() == "[REDACTED]" and not direct:
            return REDACTED
        return IDENTIFIABLE if direct else REDACTED
    return IDENTIFIABLE if direct else None


def map_record(
    record: dict,
    *,
    file_id: str,
    observed_at: datetime,
    system_hint: str | None = None,
    collection: str = "",
    copy: str = "",
    source: str = "",
    presence: str = PRESENT,
    lookup_values=(),
) -> Observation | None:
    """Turn one structured record (any format) into an Observation."""
    record = _flatten(record)
    resource_id = str(record.get("resource_id") or "").strip()
    if not resource_id:
        return None
    system_id = system_for(resource_id, system_hint)
    category = default_category(resource_id)
    if resource_prefix(resource_id) == "support":
        category = infer_support_category(collection, record)

    pii, identifiers = set(), set()
    for group, names in (("name", NAME_FIELDS), ("email", EMAIL_FIELDS), ("phone", PHONE_FIELDS), ("email_hash", HASH_FIELDS), ("user_ref", REF_FIELDS)):
        for name in names:
            value = record.get(name)
            if not _populated(value):
                continue
            classified = classify_token(value)
            # Reference fields sometimes carry an email instead of a user id.
            pii.add(KIND_TO_PII.get(classified[0], group) if classified and group == "user_ref" else group)
            if classified and classified[0] != "resource":
                identifiers.add(token(*classified))
    for name in TEXT_FIELDS:
        found = emails_in_text(record.get(name))
        if found:
            identifiers |= found
            pii.add("email")

    attributes = {k: record[k] for k in KEPT_ATTRIBUTES if k in record and record[k] is not None}
    if collection:
        attributes["collection"] = collection
    return Observation(
        file_id=file_id,
        observed_at=observed_at,
        system_id=system_id,
        resource_id=resource_id,
        presence=presence,
        condition=content_condition(category, pii, record) if presence == PRESENT else None,
        category=category,
        identifiers=identifiers,
        lookup_keys=tokens_from(lookup_values),
        pii_fields=pii,
        copy=copy,
        source=source,
        attributes=attributes,
    )


# --- "result records": system / resource / record_type / result / subject rows ---

RESULT_STATES = {
    "FOUND": (PRESENT, None),
    "NOT_FOUND": (ABSENT, None),
    "IDENTIFIABLE": (PRESENT, IDENTIFIABLE),
    "ANONYMIZED": (PRESENT, ANONYMIZED),
    "REDACTED": (PRESENT, REDACTED),
    "DISABLED": (PRESENT, DISABLED),
    "BLOCKED": (PRESENT, BLOCKED),
}


def map_result_record(
    *,
    file_id: str,
    observed_at: datetime,
    system: str,
    resource_id: str,
    record_type: str,
    result: str,
    subject: str | None,
    source: str,
) -> Observation:
    """Rows such as 'account_service | account_146 | USER_PROFILE | NOT_FOUND | e@x.com'."""
    result = (result or "").strip().upper()
    category = (record_type or "").strip().upper() or default_category(resource_id)
    if result == "ACTIVE":
        presence, condition = PRESENT, RESTORABLE if category == "BACKUP_ARCHIVE" else ACTIVE
    else:
        presence, condition = RESULT_STATES.get(result, (UNKNOWN, None))
    subject_tokens = tokens_from([subject])
    # The subject column is the record's own reference when it exists, and only a lookup key when it does not.
    shows_identifiers = condition == IDENTIFIABLE or (presence == PRESENT and condition is None)
    return Observation(
        file_id=file_id,
        observed_at=observed_at,
        system_id=system_for(resource_id, system),
        resource_id=resource_id,
        presence=presence,
        condition=condition,
        category=category,
        identifiers=subject_tokens if presence == PRESENT else set(),
        lookup_keys=subject_tokens,
        pii_fields={KIND_TO_PII[t.split(":", 1)[0]] for t in subject_tokens} if shows_identifiers else set(),
        source=source,
        attributes={"result": result},
    )
