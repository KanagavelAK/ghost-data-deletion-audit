"""Link records that only the deletion workflow's activity stream ties to a case.

Evidence links accounts and identifiable records through identifiers, but some
records carry none: a deleted account seen only as a 404 without a lookup key,
or an analytics profile that was anonymized. For those, the workflow's own
activity is the link, matched on three grounded signals:

* Time window: a case's workflow run happens around its reported_at (account
  deletion up to ~2h before; follow-up checks up to a few hours after).
* Claim agreement: the reported status is derived from the account deletion
  result (COMPLETE <- SUCCESS, PARTIALLY_COMPLETE <- PARTIAL, NOT_STARTED <- FAILED).
* Run phase: events of one workflow run are scheduled on a common clock, so
  their offsets from the run's account deletion fall on a 30-second grid:
  +9..11 s (mod 30) for actions before the status report, +20..22 s for the
  checks after it (measured on every generated sample case). Events of a
  different run sharing the window are usually off that grid.

Assignment is global: a record linked to one case by evidence is never given to
another, and each account goes to exactly one case.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .context import CaseContext, ResourceLink
from .dataset import Activity, Dataset
from .evidence import EvidenceStore

CLAIM_TO_RESULT = {"COMPLETE": "SUCCESS", "PARTIALLY_COMPLETE": "PARTIAL", "NOT_STARTED": "FAILED"}
ACCOUNT_BEFORE = timedelta(hours=2)
ACCOUNT_AFTER = timedelta(minutes=15)
RUN_BEFORE = timedelta(hours=2)
RUN_AFTER = timedelta(hours=4)
NEAR_REPORT = timedelta(minutes=15)
WORKFLOW_SOURCES = {"deletion_worker"}
# Seconds (mod 30) after the run's account deletion, for events before / after the status report.
PHASE_BEFORE_REPORT = (8.5, 11.5)
PHASE_AFTER_REPORT = (19.5, 22.5)


# Typical offset per pre-report action, used only to choose between two runs that both fit.
TYPICAL_BEFORE_REPORT = {"ANONYMIZE_RECORD": 10.8, "REDACT_RECORD": 10.2, "DISABLE_DATA_FLOW": 9.7, "BLOCK_RESTORE": 9.0}
TYPICAL_AFTER_REPORT = 21.4
CLEAR_MARGIN = 1.5  # seconds


def typical_offset(event: Activity, reported_at: datetime) -> float:
    return TYPICAL_BEFORE_REPORT.get(event.event_type, 10.0) if event.event_time < reported_at else TYPICAL_AFTER_REPORT


def phase_offset(anchor: datetime, event: datetime) -> float:
    return (event - anchor).total_seconds() % 30


def in_phase(anchor: datetime, event: datetime, reported_at: datetime) -> bool:
    lo, hi = PHASE_BEFORE_REPORT if event < reported_at else PHASE_AFTER_REPORT
    return lo <= phase_offset(anchor, event) <= hi


@dataclass
class AccountChoice:
    resource_id: str | None
    method: str  # evidence+activity | evidence | activity | activity-phase | activity-guess | none
    activity: Activity | None = None
    alternatives: list[str] = field(default_factory=list)


class ActivityLinker:
    def __init__(self, dataset: Dataset, store: EvidenceStore):
        self.dataset = dataset
        self.store = store
        self.workflow = sorted((a for a in dataset.activities if a.source_system in WORKFLOW_SOURCES), key=lambda a: a.event_time)
        self.account_deletes = [a for a in self.workflow if a.event_type == "DELETE_RECORD" and a.target_system == "account_service"]
        self.by_resource = defaultdict(list)
        for a in dataset.activities:
            self.by_resource[a.resource_id].append(a)

    # --- accounts ---------------------------------------------------------------

    def account_candidates(self, ctx: CaseContext) -> list[Activity]:
        r = ctx.request
        lo, hi = max(r.requested_at, r.reported_at - ACCOUNT_BEFORE), min(r.reported_at + ACCOUNT_AFTER, r.audit_at)
        return [a for a in self.account_deletes if lo <= a.event_time <= hi]

    def _anchors(self, ctx: CaseContext) -> list[Activity]:
        """Workflow events on records the evidence already ties to this case, inside its run window."""
        r = ctx.request
        lo, hi = r.reported_at - RUN_BEFORE, min(r.reported_at + RUN_AFTER, r.audit_at)
        return [
            a
            for rid in ctx.links
            if not rid.startswith("account_")
            for a in self.by_resource.get(rid, ())
            if a.source_system in WORKFLOW_SOURCES and lo <= a.event_time <= hi
        ]

    def assign_accounts(self, contexts: dict[str, CaseContext]) -> dict[str, AccountChoice]:
        deletion = {cid: ctx for cid, ctx in contexts.items() if ctx.intent.authorizes_deletion}
        evidence_accounts = {cid: set(ctx.resources("account_service")) for cid, ctx in deletion.items()}
        owners = defaultdict(set)
        for cid, accounts in evidence_accounts.items():
            for account in accounts:
                owners[account].add(cid)

        choices: dict[str, AccountChoice] = {}
        options: dict[str, list[Activity]] = {}
        for cid, ctx in deletion.items():
            candidates = self.account_candidates(ctx)
            linked = evidence_accounts[cid]
            both = [a for a in candidates if a.resource_id in linked]
            if both:
                choices[cid] = AccountChoice(both[-1].resource_id, "evidence+activity", both[-1])
            elif linked:
                latest = max(linked, key=lambda rid: ctx.history[rid][-1].observed_at if ctx.history.get(rid) else datetime.min.replace(tzinfo=ctx.request.audit_at.tzinfo))
                choices[cid] = AccountChoice(latest, "evidence")
            else:
                wanted = CLAIM_TO_RESULT[ctx.request.reported_status]
                # Accounts the evidence ties to another case are not available.
                free = [a for a in candidates if not owners.get(a.resource_id)]
                matching = [a for a in free if a.reported_result == wanted]
                options[cid] = matching or free

        taken = {c.resource_id for c in choices.values() if c.resource_id}
        # Constraint propagation: settle cases left with a single available account, repeatedly.
        progress = True
        while progress:
            progress = False
            for cid in sorted(options):
                if cid in choices:
                    continue
                available = [a for a in options[cid] if a.resource_id not in taken]
                if len(available) == 1:
                    choices[cid] = AccountChoice(available[0].resource_id, "activity", available[0])
                    taken.add(available[0].resource_id)
                    progress = True
                    continue
                anchors = self._anchors(deletion[cid])
                reported_at = deletion[cid].request.reported_at
                if anchors and len(available) > 1:
                    phased = [a for a in available if all(in_phase(a.event_time, x.event_time, reported_at) for x in anchors)]
                    if len(phased) == 1:
                        choices[cid] = AccountChoice(phased[0].resource_id, "activity-phase", phased[0], [a.resource_id for a in available])
                        taken.add(phased[0].resource_id)
                        progress = True

        # Whatever is still open gets the closest-to-report candidate, flagged as a guess.
        for cid in sorted(options):
            if cid in choices:
                continue
            available = [a for a in options[cid] if a.resource_id not in taken]
            if available:
                pick = min(available, key=lambda a: abs((a.event_time - deletion[cid].request.reported_at).total_seconds()))
                choices[cid] = AccountChoice(pick.resource_id, "activity-guess", pick, [a.resource_id for a in available])
                taken.add(pick.resource_id)
            else:
                choices[cid] = AccountChoice(None, "none")
        return choices

    # --- other activity-only records ---------------------------------------------

    def link_unclaimed_records(self, contexts: dict[str, CaseContext], accounts: dict[str, AccountChoice]) -> dict[str, list[str]]:
        """Give workflow-touched records that no evidence links (e.g. anonymized analytics) to the run they belong to."""
        claimed = {rid for ctx in contexts.values() for rid in ctx.links}
        claimed |= {c.resource_id for c in accounts.values() if c.resource_id}
        runs = {}
        for cid, choice in accounts.items():
            if choice.activity is not None:
                runs[cid] = choice.activity
        by_case = defaultdict(list)
        for event in self.workflow:
            if event.resource_id in claimed or event.target_system == "account_service" or event.event_type == "CLASSIFY_REQUEST":
                continue
            fits = []
            for cid, anchor in runs.items():
                r = contexts[cid].request
                if not (r.reported_at - RUN_BEFORE <= event.event_time <= min(r.reported_at + RUN_AFTER, r.audit_at)):
                    continue
                if in_phase(anchor.event_time, event.event_time, r.reported_at) and not self._has_system(contexts[cid], event.target_system):
                    distance = abs(phase_offset(anchor.event_time, event.event_time) - typical_offset(event, r.reported_at))
                    fits.append((distance, cid))
            fits.sort()
            if len(fits) == 1 or (len(fits) > 1 and fits[1][0] - fits[0][0] >= CLEAR_MARGIN):
                by_case[fits[0][1]].append(event.resource_id)
                claimed.add(event.resource_id)
                continue
            if not fits:
                # Runs without an account anchor: accept an event right at one case's status report.
                near = [
                    cid
                    for cid, ctx in contexts.items()
                    if ctx.intent.authorizes_deletion
                    and cid not in runs
                    and abs((event.event_time - ctx.request.reported_at).total_seconds()) <= NEAR_REPORT.total_seconds()
                    and not self._has_system(ctx, event.target_system)
                ]
                if len(near) == 1:
                    by_case[near[0]].append(event.resource_id)
                    claimed.add(event.resource_id)
        return by_case

    def _has_system(self, ctx: CaseContext, system_id: str) -> bool:
        return any(link.system_id == system_id for link in ctx.links.values())

    # --- apply ---------------------------------------------------------------------

    def link(self, contexts: dict[str, CaseContext]) -> dict[str, AccountChoice]:
        accounts = self.assign_accounts(contexts)
        for cid, choice in accounts.items():
            if choice.resource_id and choice.resource_id not in contexts[cid].links:
                self._attach(contexts[cid], choice.resource_id, "account_service", f"activity:{choice.activity.activity_id}" if choice.activity else "activity")
        for cid, resources in self.link_unclaimed_records(contexts, accounts).items():
            for rid in resources:
                system = self.by_resource[rid][0].target_system
                self._attach(contexts[cid], rid, system, "activity-run")
        return accounts

    def _attach(self, ctx: CaseContext, resource_id: str, system_id: str, via: str) -> None:
        link = ctx.links.setdefault(resource_id, ResourceLink(resource_id, system_id))
        link.via.add(via)
        ctx.history[resource_id] = self.store.history(resource_id, ctx.request.audit_at)
        ctx.activities[resource_id] = [a for a in self.by_resource.get(resource_id, ()) if a.event_time <= ctx.request.audit_at]
