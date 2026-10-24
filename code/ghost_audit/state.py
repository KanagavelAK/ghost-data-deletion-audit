"""Reconstruct a resource's state at audit_at from evidence only.

* The latest observations at or before audit_at decide; workflow results are claims.
* Replication drift: if any copy (primary, replica, failover) still shows the
  record at that moment, it is present.
* A complete listing of the system (Coverage) captured after the last
  observation that does not contain the record proves it absent. Full table
  dumps count for anyone; curated exports (snapshot bundles, inventories) only
  for customers they include, i.e. files that also hold records of this case.
"""

from dataclasses import dataclass, field
from datetime import datetime

from .evidence import EvidenceStore
from .evidence.model import (
    ABSENT,
    ACTIVE,
    IDENTIFIABLE,
    PRESENT,
    RESTORABLE,
    UNKNOWN,
    Coverage,
    Observation,
)

# Coverage accepted as proof of absence by omission: full tables always, curated exports when they concern the case.
UNIVERSAL_COVERAGE = {"table"}
SUBJECT_COVERAGE = {"declared", "mixed"}
# When copies disagree at the same moment, the riskier condition wins.
CONDITION_RISK = {IDENTIFIABLE: 3, ACTIVE: 3, RESTORABLE: 3}


@dataclass
class ResourceState:
    resource_id: str
    system_id: str
    presence: str = UNKNOWN
    condition: str | None = None
    category: str | None = None
    as_of: datetime | None = None
    supporting: list[Observation] = field(default_factory=list)  # observations that establish the state
    coverage: Coverage | None = None  # listing that proves absence by omission
    copies: dict[str, str] = field(default_factory=dict)  # copy -> presence at the deciding moment
    observed_categories: set[str] = field(default_factory=set)
    pii_fields: set[str] = field(default_factory=set)
    identifiers: set[str] = field(default_factory=set)


def reconstruct(resource_id: str, system_id: str, history: list[Observation], store: EvidenceStore, audit_at: datetime, case_files: set[str] = frozenset()) -> ResourceState:
    """case_files: files holding observations of any record linked to this case."""
    state = ResourceState(resource_id, system_id)
    observations = [o for o in history if o.observed_at <= audit_at and o.presence != UNKNOWN]
    state.observed_categories = {o.category for o in history if o.category}
    if not observations:
        # Identity-only links (data catalog) still show the record existed when it was linked.
        links = [o for o in history if o.observed_at <= audit_at and o.source.startswith("activity:LINK")]
        if links:
            state.presence, state.as_of, state.supporting = PRESENT, links[-1].observed_at, [links[-1]]
            state.category = links[-1].category
            state.identifiers = set(links[-1].identifiers)
    if observations:
        latest = max(o.observed_at for o in observations)
        moment = [o for o in observations if o.observed_at == latest]
        state.as_of = latest
        state.copies = {o.copy or "-": o.presence for o in moment}
        present = [o for o in moment if o.presence == PRESENT]
        state.presence = PRESENT if present else ABSENT
        state.supporting = present or moment
        if present:
            # Condition: riskiest among the present copies now; else carry the latest known one
            # forward as long as the record has stayed present since.
            conditions = [o.condition for o in present if o.condition]
            if conditions:
                state.condition = max(conditions, key=lambda c: CONDITION_RISK.get(c, 1))
            else:
                last_absent = max((o.observed_at for o in observations if o.presence == ABSENT), default=None)
                earlier = [o for o in observations if o.presence == PRESENT and o.condition and (last_absent is None or o.observed_at > last_absent)]
                if earlier:
                    state.condition = earlier[-1].condition
            state.pii_fields = set().union(*(o.pii_fields for o in present))
            state.identifiers = set().union(*(o.identifiers for o in present))
        categories = [o.category for o in observations if o.category]
        state.category = categories[-1] if categories else None

    # A later complete listing that omits the record proves it absent.
    for cov in absence_listings(resource_id, system_id, history, store, audit_at, case_files):
        if state.as_of is not None and cov.observed_at <= state.as_of:
            break
        state.presence, state.condition, state.coverage, state.as_of, state.supporting = ABSENT, None, cov, cov.observed_at, []
        break
    return state


def absence_listings(resource_id: str, system_id: str, history: list[Observation], store: EvidenceStore, audit_at: datetime, case_files: set[str]) -> list[Coverage]:
    """Complete listings of the system (newest first) that omit the record."""
    listed_files = {o.file_id for o in history}
    out = []
    for cov in sorted(store.coverage_for(system_id, audit_at), key=lambda c: c.observed_at, reverse=True):
        if cov.file_id in listed_files:
            continue
        if cov.strength in UNIVERSAL_COVERAGE or (cov.strength in SUBJECT_COVERAGE and cov.file_id in case_files):
            out.append(cov)
    return out


def citable_proof(state: ResourceState, history: list[Observation], store: EvidenceStore, audit_at: datetime, case_files: set[str]) -> str:
    """The newest file captured by audit_at that shows the record in its current state ('' if none).

    For an absent record this may be a 404/NOT_FOUND observation or a complete
    listing that omits it, as long as it is newer than any sighting of the record.
    """
    def same_state(o: Observation) -> bool:
        return o.presence == state.presence and (state.condition is None or o.condition in (None, state.condition))

    candidates: list[tuple[datetime, str]] = [(o.observed_at, o.file_id) for o in history if o.citable_by(audit_at) and same_state(o)]
    candidates += [(o.observed_at, o.file_id) for o in state.supporting if o.source.startswith("activity:LINK") and o.citable_by(audit_at)]
    if state.presence == ABSENT:
        last_seen = max((o.observed_at for o in history if o.presence == PRESENT and o.observed_at <= audit_at), default=None)
        for cov in absence_listings(state.resource_id, state.system_id, history, store, audit_at, case_files):
            if cov.captured_at and cov.captured_at <= audit_at and (last_seen is None or cov.observed_at > last_seen):
                candidates.append((cov.observed_at, cov.file_id))
    return max(candidates)[1] if candidates else ""
