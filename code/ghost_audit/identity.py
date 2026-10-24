"""Resolve a request email to the person's current and historical identifiers.

Each user's identifiers are rebuilt as time spans from the current snapshot and
the audit log, so callers can ask which identifiers existed at a point in time.
Email hashes are recomputed as sha256(lowercase email), which matches every hash
in the dataset, so records keyed only by an old email's hash still resolve.
"""

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from .dataset import Dataset
from .evidence.model import normalize_phone, token


def email_hash(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IdentifierSpan:
    kind: str  # email | hash | phone | user
    value: str  # normalized (lower-case email, digits-only phone)
    valid_from: datetime | None  # None: held before any recorded change
    valid_to: datetime | None  # None: still current
    source: str  # "identities", an audit_id, or "sha256(email)"

    @property
    def token(self) -> str:
        return token(self.kind, self.value)

    def existed_by(self, moment: datetime | None) -> bool:
        return moment is None or self.valid_from is None or self.valid_from <= moment


@dataclass
class Identity:
    query_email: str
    user_id: str | None
    name: str | None
    matched_via: str  # current_email | historical_email | unknown
    spans: list[IdentifierSpan] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def tokens(self, until: datetime | None = None) -> set[str]:
        """Identity tokens that existed at or before `until` (all of them when None)."""
        return {s.token for s in self.spans if s.existed_by(until)}

    def values(self, kind: str) -> set[str]:
        return {s.value for s in self.spans if s.kind == kind}

    @property
    def emails(self) -> set[str]:
        return self.values("email")


_FIELD_KIND = {"email": "email", "email_hash": "hash", "phone": "phone"}


def _normalize(kind: str, value: str) -> str:
    if kind == "email":
        return value.strip().lower()
    if kind == "hash":
        return value.strip().lower()
    if kind == "phone":
        return normalize_phone(value)
    return value.strip()


class IdentityResolver:
    def __init__(self, dataset: Dataset):
        self.spans_by_user: dict[str, list[IdentifierSpan]] = {}
        self.users_by_email: dict[str, set[str]] = defaultdict(set)
        changes = defaultdict(list)
        for entry in dataset.audit_log:
            if entry.field_name in _FIELD_KIND:
                changes[(entry.user_id, entry.field_name)].append(entry)

        for user in dataset.identities:
            spans = [IdentifierSpan("user", user.user_id, None, None, "identities")]
            for field_name, kind in _FIELD_KIND.items():
                current = getattr(user, field_name)
                history = sorted(changes.get((user.user_id, field_name), []), key=lambda e: e.event_time)
                if not history:
                    spans.append(IdentifierSpan(kind, _normalize(kind, current), None, None, "identities"))
                    continue
                spans.append(IdentifierSpan(kind, _normalize(kind, history[0].old_value), None, history[0].event_time, history[0].audit_id))
                for i, change in enumerate(history):
                    until = history[i + 1].event_time if i + 1 < len(history) else None
                    spans.append(IdentifierSpan(kind, _normalize(kind, change.new_value), change.event_time, until, change.audit_id))
                if _normalize(kind, current) != _normalize(kind, history[-1].new_value):
                    spans.append(IdentifierSpan(kind, _normalize(kind, current), user.updated_at, None, "identities"))
            # Recompute hashes for every email so old-email hashes are always present.
            for span in [s for s in spans if s.kind == "email"]:
                spans.append(IdentifierSpan("hash", email_hash(span.value), span.valid_from, span.valid_to, "sha256(email)"))
            self.spans_by_user[user.user_id] = _dedupe(spans)
            for span in self.spans_by_user[user.user_id]:
                if span.kind == "email":
                    self.users_by_email[span.value].add(user.user_id)
        self.names = {u.user_id: u.name for u in dataset.identities}
        self.known_emails = set(self.users_by_email)

    def resolve(self, email: str, at: datetime | None = None) -> Identity:
        query = email.strip().lower()
        users = sorted(self.users_by_email.get(query, ()))
        if not users:
            spans = [IdentifierSpan("email", query, None, None, "request"), IdentifierSpan("hash", email_hash(query), None, None, "sha256(email)")]
            return Identity(email, None, None, "unknown", spans, ["request email matches no known user"])

        notes = []
        if len(users) > 1:
            # Prefer the user who held the email at the given moment.
            holding = [u for u in users if any(s.kind == "email" and s.value == query and _covers(s, at) for s in self.spans_by_user[u])]
            notes.append(f"email shared by {users}; chose by validity at {at}")
            users = holding or users
        user_id = users[0]
        spans = self.spans_by_user[user_id]
        current = any(s.kind == "email" and s.value == query and s.valid_to is None for s in spans)
        return Identity(email, user_id, self.names.get(user_id), "current_email" if current else "historical_email", list(spans), notes)

    def lookalikes(self, identity: Identity, candidate_emails) -> set[str]:
        """Emails that resemble the subject's but belong to no known user (likely decoys)."""
        mine = {_email_stem(e) for e in identity.emails}
        return {e for e in candidate_emails if e not in self.known_emails and _email_stem(e) in mine}


def _covers(span: IdentifierSpan, at: datetime | None) -> bool:
    if at is None:
        return span.valid_to is None
    return (span.valid_from is None or span.valid_from <= at) and (span.valid_to is None or at < span.valid_to)


def _dedupe(spans: list[IdentifierSpan]) -> list[IdentifierSpan]:
    seen, out = set(), []
    for span in spans:
        key = (span.kind, span.value, span.valid_from, span.valid_to)
        if key not in seen:
            seen.add(key)
            out.append(span)
    return out


def _email_stem(email: str) -> str:
    """Local part without '.jr' suffixes, digits and separators: smartinez6461.jr@x -> smartinez@x."""
    local, _, domain = email.lower().partition("@")
    local = re.sub(r"\.jr$", "", local)
    return re.sub(r"[\d._-]", "", local) + "@" + domain
