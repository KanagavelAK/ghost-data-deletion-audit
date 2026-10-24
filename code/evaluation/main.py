"""Score predictions against canonical answers (default: the 32 public samples).

    python code/evaluation/main.py                     # solve samples, then score them
    python code/evaluation/main.py --pred FILE         # score an existing prediction file
    python code/evaluation/main.py --pred dataset/sample_output.csv   # self-check (expect 100%)

Writes runs/sample_score.md (metrics) and runs/sample_diff.md (per-case differences).
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ghost_audit import paths  # noqa: E402
from ghost_audit.dataset import load_dataset, load_requests  # noqa: E402
from ghost_audit.output import read_output, write_output  # noqa: E402
from ghost_audit.solver import solve_all  # noqa: E402
from ghost_audit.validate import validate_rows  # noqa: E402

from scorer import POSITION_FIELDS, Summary, score  # noqa: E402


def load_rows(path: Path) -> tuple[list[str], list, dict]:
    header, rows = read_output(path)
    return header, rows, {r.case_id: (r.status, r.steps) for r in rows}


def pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def render_report(summary: Summary, title: str) -> str:
    s = summary
    lines = [
        f"# {title}",
        "",
        f"Cases scored: {s.n}" + (f" (no usable prediction: {', '.join(s.unscored)})" if s.unscored else ""),
        "",
        "## Headline",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Case-level status accuracy | {pct(s.status_accuracy)} |",
        f"| Exact row match (status + full steps_json) | {pct(s.exact_match_rate)} |",
        f"| Step-level micro F1 (all fields) | {pct(s.steps.f1)} (P {pct(s.steps.precision)}, R {pct(s.steps.recall)}) |",
        f"| Step-level micro F1 (ignoring evidence_id) | {pct(s.struct.f1)} |",
        f"| Mean positional field accuracy | {pct(s.mean_field_accuracy)} |",
        f"| Sequence exact match | {pct(s.sequence_exact_rate)} |",
        f"| Mean sequence LCS ratio | {pct(s.mean_lcs_ratio)} |",
        f"| Mean pairwise order agreement | {pct(s.mean_order_agreement)} |",
        f"| Account identity resolution | {pct(s.account_accuracy)} |",
        f"| Resource discovery micro F1 | {pct(s.resources.f1)} (P {pct(s.resources.precision)}, R {pct(s.resources.recall)}) |",
        f"| Evidence citation accuracy | {pct(s.evidence_accuracy)} |",
        f"| Composite (local proxy) | {pct(s.composite)} |",
        "",
        "## Field-level accuracy (by step position)",
        "",
        "| Field | Accuracy |",
        "| --- | --- |",
    ]
    lines += [f"| {name} | {pct(s.field_accuracy(name))} |" for name in POSITION_FIELDS]

    lines += ["", "## Step F1 by phase", "", "| Phase | P | R | F1 |", "| --- | --- | --- | --- |"]
    for phase, prf in s.by_phase.items():
        lines.append(f"| {phase} | {pct(prf.precision)} | {pct(prf.recall)} | {pct(prf.f1)} |")

    lines += ["", "## Recall by gold operation", "", "| Operation | Predicted exactly / gold |", "| --- | --- |"]
    for op, (hit, total) in sorted(s.operation_recall().items()):
        lines.append(f"| {op} | {hit}/{total} |")

    statuses = ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")
    confusion = s.confusion()
    lines += ["", "## Status confusion (rows = gold, columns = predicted)", "", "| gold \\ pred | " + " | ".join(statuses) + " | other |"]
    lines.append("| --- " * (len(statuses) + 2) + "|")
    for g in statuses:
        other = sum(n for (gs, ps), n in confusion.items() if gs == g and ps not in statuses)
        lines.append(f"| {g} | " + " | ".join(str(confusion.get((g, p), 0)) for p in statuses) + f" | {other} |")
    return "\n".join(lines) + "\n"


def render_diff(summary: Summary, notes: dict[str, dict]) -> str:
    lines = ["# Per-case differences", ""]
    wrong = [c for c in summary.cases if not c.exact]
    if not wrong:
        return "# Per-case differences\n\nAll cases match exactly.\n"
    lines.append(f"{len(wrong)} of {summary.n} cases differ.\n")
    for c in wrong:
        status = c.gold_status if c.status_correct else f"gold {c.gold_status} / pred {c.pred_status or '(none)'}"
        lines.append(f"## {c.case_id} - {status}")
        if c.case_id in notes:
            lines.append(f"- solver notes: `{notes[c.case_id]}`")
        for label, keys in (("missing (in gold, not predicted)", c.missing), ("extra (predicted, not in gold)", c.extra)):
            if keys:
                lines.append(f"- {label}:")
                lines += [f"  - `{k[1]} {k[2]} {k[3]} {k[4]} [{k[5] or '-'}]`" for k in keys]
        if not c.missing and not c.extra and c.status_correct:
            lines.append("- same steps, different order")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pred", type=Path, help="prediction CSV to score (default: solve the requests first)")
    parser.add_argument("--gold", type=Path, default=paths.SAMPLE_GOLD)
    parser.add_argument("--requests", type=Path, default=paths.SAMPLE_REQUESTS)
    parser.add_argument("--report", type=Path, default=paths.RUNS_DIR / "sample_score.md")
    parser.add_argument("--diff", type=Path, default=paths.RUNS_DIR / "sample_diff.md")
    args = parser.parse_args(argv)

    dataset = load_dataset()
    requests = load_requests(args.requests)
    notes = {}
    pred_path = args.pred
    if pred_path is None:
        results = solve_all(requests, dataset)
        notes = {r.case_id: r.notes for r in results}
        pred_path = paths.RUNS_DIR / "sample_predictions.csv"
        write_output(results, pred_path)
        print(f"solved {len(results)} requests -> {pred_path}")

    header, rows, pred = load_rows(pred_path)
    print(validate_rows(header, rows, requests, dataset).summary())

    _, _, gold = load_rows(args.gold)
    summary = score(gold, pred)
    report = render_report(summary, f"Score: {pred_path.name} vs {args.gold.name}")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report, encoding="utf-8")
    args.diff.write_text(render_diff(summary, notes), encoding="utf-8")

    headline = report.split("## Field-level")[0]
    print(headline.strip())
    print(f"\nreport -> {args.report}\ndiff   -> {args.diff}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
