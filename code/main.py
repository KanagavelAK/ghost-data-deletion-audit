"""Entry point: audit every deletion request and write output.csv.

    python code/main.py                   # 518 evaluation requests -> output.csv (repo root)
    python code/main.py --split sample    # 32 sample requests -> runs/sample_predictions.csv
    python code/main.py --requests PATH --output PATH
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ghost_audit import paths  # noqa: E402
from ghost_audit.dataset import load_dataset, load_requests  # noqa: E402
from ghost_audit.output import write_output  # noqa: E402
from ghost_audit.solver import solve_all  # noqa: E402
from ghost_audit.validate import validate_file  # noqa: E402

SPLITS = {
    "eval": (paths.EVAL_REQUESTS, paths.OUTPUT_PATH),
    "sample": (paths.SAMPLE_REQUESTS, paths.RUNS_DIR / "sample_predictions.csv"),
}


def run(requests_path: Path, output_path: Path) -> int:
    dataset = load_dataset()
    requests = load_requests(requests_path)
    results = solve_all(requests, dataset)
    write_output(results, output_path)

    counts = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    print(f"wrote {len(results)} rows to {output_path}")
    print("statuses: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    report = validate_file(output_path, requests, dataset)
    print(report.summary())
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--split", choices=sorted(SPLITS), default="eval")
    parser.add_argument("--requests", type=Path, help="override the requests CSV")
    parser.add_argument("--output", type=Path, help="override the output CSV path")
    args = parser.parse_args(argv)

    default_requests, default_output = SPLITS[args.split]
    return run(args.requests or default_requests, args.output or default_output)


if __name__ == "__main__":
    sys.exit(main())
