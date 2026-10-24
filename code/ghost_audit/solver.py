"""Case solver: intent -> identity -> linked records -> state at audit_at -> policy -> plan.

Every known request (sample and evaluation) is linked together, because
workflow runs of different cases share time windows and records must be
assigned to exactly one case; only the requested cases are returned.
"""

from .context import CaseContext, ContextBuilder
from .dataset import Dataset, Request, load_all_requests
from .evidence import EvidenceStore, load_evidence
from .intent import Intent, IntentClassifier
from .linker import AccountChoice, ActivityLinker
from .output import CaseResult
from .planner import plan
from .policy import Policy
from .state import citable_proof, reconstruct


def resolve_intent(ctx: CaseContext) -> Intent:
    """Low-confidence 'not deletion' verdicts defer to a privacy-intake classification when one exists."""
    intent = ctx.intent
    if not intent.confident and ctx.intake_non_deletion:
        return Intent(False, f"intake classified {ctx.intake_non_deletion[0].resource_id} as non-deletion", "intake")
    return intent


def solve_case(ctx: CaseContext, account: AccountChoice | None, store: EvidenceStore, policy: Policy) -> CaseResult:
    request = ctx.request
    intent = resolve_intent(ctx)
    notes = {"intent": f"{intent.method}: {intent.reason}", "user": ctx.identity.user_id, "matched_via": ctx.identity.matched_via}
    if ctx.lookalikes:
        notes["lookalikes"] = sorted(ctx.lookalikes)
    if not intent.authorizes_deletion:
        return CaseResult(request.case_id, "NOT_APPLICABLE", [], notes)
    if account:
        notes["account"] = f"{account.resource_id} ({account.method})" + (f" alternatives={account.alternatives}" if account.alternatives else "")

    assessments = []
    case_files = {o.file_id for history in ctx.history.values() for o in history}
    for resource_id, link in sorted(ctx.links.items()):
        state = reconstruct(resource_id, link.system_id, ctx.history.get(resource_id, []), store, request.audit_at, case_files)
        assessments.append(policy.assess(state, ctx.activities.get(resource_id, [])))
    notes["assessments"] = [
        f"{a.resource_id}: {a.state.presence}/{a.state.condition} {a.category} -> {a.action} {'ok' if a.compliant else 'VIOLATION' if a.finding else 'unproven'}"
        for a in assessments
    ]

    def cite(a):
        proof = citable_proof(a.state, ctx.history.get(a.resource_id, []), store, request.audit_at, case_files)
        if proof:
            return proof
        # No evidence file shows the state: fall back to the latest workflow record (a claim), flagged in notes.
        claims = [x for x in ctx.activities.get(a.resource_id, []) if x.event_time <= request.audit_at]
        notes.setdefault("claim_citations", []).append(a.resource_id)
        return claims[-1].activity_id if claims else ""

    status, steps = plan(assessments, cite)
    if not steps:
        notes["warning"] = "no relevant records found"
    return CaseResult(request.case_id, status, steps, notes)


def solve_all(requests: list[Request], dataset: Dataset, store: EvidenceStore | None = None, use_llm: bool = True) -> list[CaseResult]:
    store = store or load_evidence(dataset)
    known = {r.case_id: r for r in load_all_requests()}
    known.update({r.case_id: r for r in requests})
    builder = ContextBuilder(dataset, store, list(known.values()))
    intents = IntentClassifier(use_llm=use_llm).classify_all([r.request_text for r in known.values()])
    contexts = {cid: builder.build(r, intents[r.request_text]) for cid, r in known.items()}
    accounts = ActivityLinker(dataset, store).link(contexts)
    policy = Policy(dataset.rules)
    return [solve_case(contexts[r.case_id], accounts.get(r.case_id), store, policy) for r in requests]
