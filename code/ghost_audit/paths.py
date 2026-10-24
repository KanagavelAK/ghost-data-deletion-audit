"""Filesystem locations, resolved relative to the repository root."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = REPO_ROOT / "dataset"
EVIDENCE_DIR = DATASET_DIR / "evidence"

EVAL_REQUESTS = DATASET_DIR / "deletion_requests.csv"
SAMPLE_REQUESTS = DATASET_DIR / "sample_deletion_requests.csv"
SAMPLE_GOLD = DATASET_DIR / "sample_output.csv"

# Final submission file lives at the repository root.
OUTPUT_PATH = REPO_ROOT / "output.csv"

# Scratch outputs from local runs (sample predictions, score reports, diffs).
RUNS_DIR = REPO_ROOT / "runs"
