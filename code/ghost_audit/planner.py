"""Turn per-resource assessments into a case status and ordered steps_json.

INCOMPLETE: findings (citing evidence), the remediation plan, then verification
of the required final state of every relevant record (future work: no citation).
COMPLETE: verification steps only, each citing the evidence of the current state.
"""

from collections.abc import Callable

from .policy import Assessment
from .schema import Step, canonical_order


def plan(assessments: list[Assessment], cite: Callable[[Assessment], str]) -> tuple[str, list[Step]]:
    """cite(assessment) returns the evidence id proving that resource's current state."""
    relevant = [a for a in assessments if a.verification]
    violations = [a for a in relevant if a.finding]
    steps: list[Step] = []
    if violations:
        for a in violations:
            steps.append(Step(a.finding, a.system_id, a.resource_id, cite(a)))
            steps.append(Step(a.remediation, a.system_id, a.resource_id))
        steps += [Step(a.verification, a.system_id, a.resource_id) for a in relevant]
        return "INCOMPLETE", canonical_order(steps)
    steps = [Step(a.verification, a.system_id, a.resource_id, cite(a)) for a in relevant]
    return "COMPLETE", canonical_order(steps)
