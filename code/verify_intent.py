"""Cross-check the rule-based intent decisions with Claude on every unique request text.

    python code/verify_intent.py      # needs ANTHROPIC_API_KEY; verdicts are cached in code/cache/intent_llm.json

Writes runs/intent_crosscheck.md listing every disagreement between rules and the LLM.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ghost_audit import paths  # noqa: E402
from ghost_audit.dataset import load_all_requests  # noqa: E402
from ghost_audit.intent import IntentClassifier, classify_by_rules  # noqa: E402


def main() -> int:
    requests = load_all_requests()
    texts = list(dict.fromkeys(r.request_text for r in requests))
    classifier = IntentClassifier()
    llm = classifier.llm_judgements(texts)
    if not llm:
        print("no LLM verdicts available (set ANTHROPIC_API_KEY in .env)")
        return 2

    rows, agree = [], 0
    for text in texts:
        rule = classify_by_rules(text)
        verdict = llm.get(text)
        if verdict is None:
            continue
        if rule is not None and rule.authorizes_deletion == verdict["authorizes_deletion"]:
            agree += 1
            continue
        cases = ", ".join(r.case_id for r in requests if r.request_text == text)
        rows.append(f"| {cases} | {'-' if rule is None else rule.authorizes_deletion} | {verdict['authorizes_deletion']} | {verdict.get('reason', '')} | {text[:120]} |")

    lines = [
        "# Intent cross-check: rules vs LLM",
        "",
        f"Unique request texts: {len(texts)}; judged by LLM: {len(llm)}; rules and LLM agree: {agree}.",
        "",
        "| cases | rules | LLM | LLM reason | text |",
        "| --- | --- | --- | --- | --- |",
        *rows,
    ]
    paths.RUNS_DIR.mkdir(exist_ok=True)
    out = paths.RUNS_DIR / "intent_crosscheck.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{agree}/{len(llm)} agree, {len(rows)} disagreement(s) -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
