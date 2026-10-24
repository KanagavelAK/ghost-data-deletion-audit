"""Per-case context: the person's identity wired to the evidence that concerns them.

A resource is linked to a case when an observation dated at or before audit_at
carries one of the subject's identifiers (content or lookup key), or when it is
referenced by an already-linked record (e.g. a search document's
source_resource_id). Everything later stages need is collected here.

Some people file more than one deletion request (user_17: March and August,
with a different account each time). Links observed before the audit of the
same person's previous request belong to that earlier case and are ignored.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from .dataset import Activity, Dataset, Request
from .evidence import EvidenceStore
from .evidence.model import RESOURCE_RE, Observation
from .identity import Identity, IdentityResolver
from .intent import Intent

REFERENCE_ATTRIBUTES = ("source_resource_id",)


def edit_distance(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def near_match(token_a: str, token_b: str, tolerance: int = 2) -> bool:
    """Same kind of identifier and at most `tolerance` characters apart (exact for user ids)."""
    kind_a, _, value_a = token_a.partition(":")
    kind_b, _, value_b = token_b.partition(":")
    if kind_a != kind_b:
        return False
    if kind_a == "user":
        return value_a == value_b
    return value_a == value_b or (abs(len(value_a) - len(value_b)) <= tolerance and edit_distance(value_a, value_b) <= tolerance)


@dataclass
class ResourceLink:
    resource_id: str
    system_id: str
    via: set[str] = field(default_factory=set)  # identity tokens or "ref:<resource_id>"
    files: set[str] = field(default_factory=set)  # files in which the link was observed


@dataclass
class CaseContext:
    request: Request
    intent: Intent
    identity: Identity
    links: dict[str, ResourceLink]
    history: dict[str, list[Observation]]  # every observation of each linked resource up to audit_at
    activities: dict[str, list[Activity]]  # activity rows touching each linked resource up to audit_at
    later_only: set[str]  # resources linked only by observations after audit_at (diagnostic)
    lookalikes: set[str]  # decoy emails resembling the subject's
    intake_non_deletion: list[Activity]  # privacy-intake activities classifying a linked account as non-deletion
    window_start: datetime | None = None  # audit_at of the same person's previous request, if any
    earlier_only: set[str] = field(default_factory=set)  # linked only before window_start (diagnostic)
    conflicting: set[str] = field(default_factory=set)  # catalog-linked records whose content names someone else

    def resources(self, system_id: str | None = None) -> list[str]:
        return sorted(r for r, link in self.links.items() if system_id is None or link.system_id == system_id)


class ContextBuilder:
    def __init__(self, dataset: Dataset, store: EvidenceStore, all_requests: list[Request] = (), resolver: IdentityResolver | None = None):
        """all_requests: every known request (sample and evaluation) to find a person's earlier requests."""
        self.dataset = dataset
        self.store = store
        self.resolver = resolver or IdentityResolver(dataset)
        self.requests_by_user = defaultdict(list)
        for other in all_requests:
            user_id = self.resolver.resolve(other.email).user_id
            if user_id:
                self.requests_by_user[user_id].append(other)
        self.activities_by_resource = defaultdict(list)
        for activity in dataset.activities:
            self.activities_by_resource[activity.resource_id].append(activity)
        self.referenced_by = defaultdict(set)  # resource -> resources whose records point at it
        for obs in store.observations:
            for name in REFERENCE_ATTRIBUTES:
                target = obs.attributes.get(name)
                if target:
                    self.referenced_by[str(target)].add(obs.resource_id)
        self.evidence_emails = {t[6:] for t in store.by_identifier if t.startswith("email:")}

    def build(self, request: Request, intent: Intent) -> CaseContext:
        audit_at = request.audit_at
        identity = self.resolver.resolve(request.email, request.requested_at)
        tokens = identity.tokens(until=audit_at)
        earlier_audits = [
            other.audit_at
            for other in self.requests_by_user.get(identity.user_id, ())
            if other.case_id != request.case_id and other.audit_at < request.requested_at
        ]
        window_start = max(earlier_audits) if earlier_audits else None

        links: dict[str, ResourceLink] = {}
        later, earlier = set(), set()
        for tok in tokens:
            for obs in self.store.by_identifier.get(tok, ()):
                if obs.observed_at > audit_at:
                    later.add(obs.resource_id)
                    continue
                if window_start and obs.observed_at <= window_start:
                    earlier.add(obs.resource_id)
                    continue
                link = links.setdefault(obs.resource_id, ResourceLink(obs.resource_id, obs.system_id))
                link.via.add(tok)
                link.files.add(obs.file_id)

        # One reference hop in both directions (search document <-> source account), plus
        # records a backup restore repopulated before audit_at (a recreation path).
        for resource_id in list(links):
            neighbours = set(self.referenced_by.get(resource_id, ()))
            for obs in self.store.history(resource_id, audit_at):
                for name in REFERENCE_ATTRIBUTES:
                    if obs.attributes.get(name):
                        neighbours.add(str(obs.attributes[name]))
            for activity in self.activities_by_resource.get(resource_id, ()):
                if activity.event_type == "RESTORE_BACKUP" and activity.event_time <= audit_at:
                    neighbours |= {m.group(0) for m in RESOURCE_RE.finditer(activity.details)} - {resource_id}
            for other in neighbours:
                history = self.store.history(other, audit_at)
                system_id = history[-1].system_id if history else self._system_from_activity(other)
                link = links.setdefault(other, ResourceLink(other, system_id))
                link.via.add(f"ref:{resource_id}")

        # A data-catalog link is metadata; if the record's own content names someone else, the content wins.
        conflicts = set()
        for resource_id, link in list(links.items()):
            if not link.files or not all(f.startswith("activity_") for f in link.files):
                continue
            shown = {
                t
                for obs in self.store.history(resource_id, audit_at)
                if not obs.source.startswith("activity:")
                for t in obs.identifiers
                if t.startswith(("email:", "phone:", "user:"))
            }
            # A one- or two-character difference is a transcription slip, not a different person.
            if shown and not any(near_match(t, mine) for t in shown for mine in tokens):
                conflicts.add(resource_id)
                del links[resource_id]

        history = {r: self.store.history(r, audit_at) for r in links}
        activities = {r: [a for a in self.activities_by_resource.get(r, ()) if a.event_time <= audit_at] for r in links}
        intake = [
            a
            for r in links
            for a in activities[r]
            if a.event_type == "CLASSIFY_REQUEST" and request.requested_at <= a.event_time <= audit_at
        ]
        return CaseContext(
            request=request,
            intent=intent,
            identity=identity,
            links=links,
            history=history,
            activities=activities,
            later_only=later - set(links),
            lookalikes=self.resolver.lookalikes(identity, self.evidence_emails),
            intake_non_deletion=intake,
            window_start=window_start,
            earlier_only=earlier - set(links),
            conflicting=conflicts,
        )

    def _system_from_activity(self, resource_id: str) -> str:
        rows = self.activities_by_resource.get(resource_id)
        return rows[0].target_system if rows else "unknown"
