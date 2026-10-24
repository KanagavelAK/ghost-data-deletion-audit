"""Run the team-built evaluation cases and score them against hand-derived answers.

    python code/evaluation/run_custom_cases.py

Cases live in code/evaluation/custom_cases/: requests.csv (same columns as
deletion_requests.csv) and expected_output.csv (same format as output.csv). The
expected answers were derived by hand from the evidence, not from the solver.
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from ghost_audit import paths  # noqa: E402
from ghost_audit.dataset import load_dataset, load_requests  # noqa: E402
from ghost_audit.evidence import load_evidence  # noqa: E402
from ghost_audit.output import read_output, write_output  # noqa: E402
from ghost_audit.solver import solve_all  # noqa: E402
from ghost_audit.validate import validate_rows  # noqa: E402

from scorer import score  # noqa: E402

CASES = HERE / "custom_cases"
WHAT = {
    "custom_01": "sample case_12's person writing from their OLD email (historical identity + old-email hash)",
    "custom_02": "sample case_24's person, upper-case email, request in German",
    "custom_03": "Spanish access request that says not to delete anything (must be NOT_APPLICABLE)",
    "custom_04": "Haruka audited on 20 Sep, before the 26 Sep screenshot (time cutoff + old phone on an attachment)",
}


def main() -> int:
    dataset = load_dataset()
    requests = load_requests(CASES / "requests.csv")
    results = solve_all(requests, dataset, load_evidence(dataset), use_llm=False)
    out = paths.RUNS_DIR / "custom_predictions.csv"
    write_output(results, out)

    header, rows = read_output(out)
    print(validate_rows(header, rows, requests, dataset).summary())
    _, gold_rows = read_output(CASES / "expected_output.csv")
    summary = score({r.case_id: (r.status, r.steps) for r in gold_rows}, {r.case_id: (r.status, r.steps) for r in rows})
    for case in summary.cases:
        verdict = "exact" if case.exact else f"status {case.pred_status} (expected {case.gold_status}); missing {case.missing}; extra {case.extra}"
        print(f"{case.case_id}: {verdict} - {WHAT.get(case.case_id, '')}")
    print(f"status accuracy {summary.status_accuracy:.0%}, exact {sum(c.exact for c in summary.cases)}/{summary.n}, step F1 {summary.steps.f1:.1%}")
    return 0 if all(c.exact for c in summary.cases) else 1


if __name__ == "__main__":
    sys.exit(main())
