"""Build the submission archives at the repository root.

    python code/package_submission.py                     # code.zip and agent-trace.zip
    python code/package_submission.py --sessions DIR ...  # explicit Claude Code session folders

code.zip      README.md + code/ (no bytecode, no .env, no nested archives).
agent-trace.zip  Claude Code session logs for this challenge with secrets redacted
                 (API keys and tokens). Run this again at the very end, after
                 closing the coding session, so the trace is complete. Add the
                 readable transcript from /export into agent-trace/claude-code/
                 before re-running if you want it included.

Both archives are verified after writing: code.zip must not contain .env or
nested archives, and no file in agent-trace.zip may still match a secret pattern.
"""

import argparse
import re
import shutil
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ghost_audit import paths  # noqa: E402

ROOT = paths.REPO_ROOT
NESTED_ARCHIVES = (".zip", ".tar", ".gz", ".tgz", ".7z", ".rar")
SKIP_PARTS = {"__pycache__", ".venv", "venv", "node_modules", ".git"}
SECRET_PATTERNS = [
    (re.compile(r"gsk_[A-Za-z0-9]{20,}"), "[REDACTED_GROQ_KEY]"),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), "[REDACTED_ANTHROPIC_KEY]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"((?:GROQ|ANTHROPIC)_API_KEY[ \t]*=[ \t]*)(?!your-key|\[REDACTED)[^\s\"'\\\[\]]+"), r"\1[REDACTED]"),
]


def build_code_zip(target: Path) -> list[str]:
    files = [ROOT / "README.md"]
    for path in sorted((ROOT / "code").rglob("*")):
        if path.is_dir() or SKIP_PARTS & set(path.parts) or path.suffix == ".pyc" or path.name == ".env":
            continue
        if path.suffix.lower() in NESTED_ARCHIVES:
            raise SystemExit(f"refusing to package nested archive {path}")
        files.append(path)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(ROOT).as_posix())
    return [p.relative_to(ROOT).as_posix() for p in files]


def default_session_dirs() -> list[Path]:
    """Claude Code stores sessions per working directory, with path separators replaced by '-'."""
    projects = Path.home() / ".claude" / "projects"
    names = {re.sub(r"[:\\/]", "-", str(p)) for p in (ROOT, ROOT.parent)}
    return [projects / name for name in sorted(names) if (projects / name).is_dir()]


def redact(text: str) -> tuple[str, int]:
    count = 0
    for pattern, replacement in SECRET_PATTERNS:
        text, n = pattern.subn(replacement, text)
        count += n
    return text, count


def build_agent_trace(session_dirs: list[Path], target: Path) -> tuple[int, int]:
    staging = ROOT / "agent-trace" / "claude-code"
    exports = [p for p in staging.glob("*") if p.is_file() and not p.suffix == ".jsonl"] if staging.exists() else []
    kept = {p.name: p.read_bytes() for p in exports}  # keep /export transcripts the user added
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    for name, data in kept.items():
        (staging / name).write_bytes(data)

    copied = redactions = 0
    for directory in session_dirs:
        for path in sorted(directory.rglob("*")):
            if path.is_dir() or "memory" in path.relative_to(directory).parts:
                continue
            dest = staging / directory.name / path.relative_to(directory)
            dest.parent.mkdir(parents=True, exist_ok=True)
            text, n = redact(path.read_text(encoding="utf-8", errors="replace"))
            dest.write_text(text, encoding="utf-8")
            copied += 1
            redactions += n
    for path in staging.glob("*"):
        if path.is_file():
            text, n = redact(path.read_text(encoding="utf-8", errors="replace"))
            path.write_text(text, encoding="utf-8")
            redactions += n

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted((ROOT / "agent-trace").rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(ROOT).as_posix())
    return copied, redactions


def verify(code_zip: Path, trace_zip: Path | None) -> list[str]:
    problems = []
    with zipfile.ZipFile(code_zip) as archive:
        for name in archive.namelist():
            if name.endswith(".env") or name.lower().endswith(NESTED_ARCHIVES):
                problems.append(f"code.zip contains {name}")
            text = archive.read(name).decode("utf-8", errors="ignore")
            if any(p.search(text) for p, _ in SECRET_PATTERNS):
                problems.append(f"code.zip: secret pattern in {name}")
    if trace_zip:
        with zipfile.ZipFile(trace_zip) as archive:
            for name in archive.namelist():
                text = archive.read(name).decode("utf-8", errors="ignore")
                if any(p.search(text) for p, _ in SECRET_PATTERNS):
                    problems.append(f"agent-trace.zip: secret pattern left in {name}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sessions", nargs="*", type=Path, help="Claude Code session folders (default: this project's)")
    parser.add_argument("--no-trace", action="store_true", help="only build code.zip")
    args = parser.parse_args(argv)

    code_zip = ROOT / "code.zip"
    files = build_code_zip(code_zip)
    print(f"code.zip: {len(files)} files ({code_zip.stat().st_size // 1024} KB)")

    trace_zip = None
    if not args.no_trace:
        dirs = args.sessions or default_session_dirs()
        if not dirs:
            print("no Claude Code session folder found; pass --sessions")
        else:
            trace_zip = ROOT / "agent-trace.zip"
            copied, redactions = build_agent_trace(dirs, trace_zip)
            print(f"agent-trace.zip: {copied} session files from {[d.name for d in dirs]}, {redactions} secret(s) redacted "
                  f"({trace_zip.stat().st_size // 1024} KB)")

    problems = verify(code_zip, trace_zip)
    for problem in problems:
        print("PROBLEM: " + problem)
    print("archives verified: no .env, no nested archives, no secrets" if not problems else f"{len(problems)} problem(s)")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
