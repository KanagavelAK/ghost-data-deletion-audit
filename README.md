# Ghost Data Deletion Audit

An AI-powered audit agent that proves whether a person's personal data is really gone after a deletion request.

Built for the HackerRank Orchestrate (October 2026) challenge **"Ghost Data Still Exists"**. A deletion workflow can report *Complete* while copies of the data survive in replicas, search indexes, support tickets, analytics, exports, logs and backups, and sync or restore jobs can bring deleted data back. For every request this agent:

1. decides whether the request actually asks for deletion;
2. resolves the person's current **and historical** identifiers;
3. finds every record that belongs to them across all evidence;
4. reconstructs each record's state at the audit time from evidence (workflow statuses are only claims);
5. applies the deletion policy and returns `COMPLETE`, `INCOMPLETE` or `NOT_APPLICABLE`;
6. writes the ordered findings, remediation and verification steps, citing the evidence for each.

## Results

Local scorer on the 32 public sample cases:

| Metric | Score |
| --- | --- |
| Case-level status accuracy | **100%** |
| Exact row match (status + all steps) | **90.6%** (29 / 32) |
| Step-level F1 | **99.2%** (precision 100%) |
| Evidence citation accuracy | **98.7%** |
| Account identity resolution | **96.9%** |

The three differences are records that appear in none of the supplied data, so they cannot be derived. A full run over the 518 evaluation requests takes about 2 seconds and produces [`output.csv`](output.csv) with zero contract errors and makes no model calls.

Also checked: 4 team-built evaluation cases with hand-derived answers (4/4 exact), the cached screenshot extractions against facts read from the images (24/25 exact), and every rule-based intent decision against an LLM (398/398 agree). Details are in [`code/evaluation/evaluation_report.md`](code/evaluation/evaluation_report.md); token usage and cost are in [`code/evaluation/usage_report.md`](code/evaluation/usage_report.md).

## How it works

```text
deletion request
      │
      ▼
 ┌────────────────┐   multilingual rules, LLM fallback for undecided texts
 │ Intent         │──▶ not a deletion request ──▶ NOT_APPLICABLE, []
 └────────────────┘
      │
      ▼
 ┌────────────────┐   current + historical emails, phones, user ids,
 │ Identity       │   sha256 hashes of every email it ever had
 └────────────────┘
      │
      ▼
 ┌────────────────┐   94 evidence files (SQL, JSON, logs, text, PDF, screenshots)
 │ Evidence store │   normalised into one table of observations
 └────────────────┘
      │
      ▼
 ┌────────────────┐   identifier matches, record references, catalog links,
 │ Linking        │   workflow-run timing, one account per case (global)
 └────────────────┘
      │
      ▼
 ┌────────────────┐   latest evidence at or before audit_at; any replica copy
 │ State          │   present = present; absence proven by complete listings
 └────────────────┘
      │
      ▼
 ┌────────────────┐   deletion_rules.csv: delete, redact, anonymize, retain,
 │ Policy         │   block restore, disable data flow
 └────────────────┘
      │
      ▼
 ┌────────────────┐   findings → remediation → verification, canonical order,
 │ Plan           │   newest evidence captured by audit_at as citation
 └────────────────┘
```

### What the investigation handles

- **Claims are not proof.** A deletion reported as SUCCESS can leave a full profile in a failover replica; one reported as FAILED can already be gone. Only evidence decides.
- **Historical identities.** About one in five requests uses an old email address. Records keyed only by the hash of an old email are still found.
- **Replication drift.** SQL snapshots hold primary, read, reporting, regional and failover copies; a record left in any copy is still present.
- **Absence by omission.** A full table dump, or a customer snapshot that includes the person, proves a record is gone when it is missing from it.
- **Decoys.** Look-alike emails (`name.jr@...`) and other customers in the same files are never linked.
- **Repeat requesters.** Evidence from a person's earlier deletion request does not leak into a later one.
- **Activity-only records.** Deleted accounts and anonymized analytics carry no identifiers; they are linked through the timing of the deletion workflow run that touched them, assigned globally so no record goes to two cases.
- **Time cutoff.** Only evidence up to `audit_at` is used, and only files captured by then are cited.
- **Catalog claims vs. content.** A data-catalog link is metadata; when the record itself shows a different person, it is not linked to the case.
- **Unproven safety.** A backup or data flow that is present but shows no restore or sync state is treated as still able to recreate data.
- **Policy nuances.** Support messages are redacted while attachments are deleted, account tombstones keep only the stable `user_id`, security audit records and legal holds are retained, and backups must have restores blocked.

### Where AI is used

The core is deterministic so the output is reproducible and explainable. A language model is used only where it adds the most:

- **Screenshot reading** – admin-console screenshots are read once with a vision model and cached (`code/extract_images.py`). Each request sends the full screen plus two zoomed halves so small IDs and phone numbers survive downscaling, and records shown only by a display reference (a file name, a customer key) are mapped back through the data catalog. `code/evaluation/verify_screenshots.py` checks every cached extraction against facts read from the images by hand.
- **Request intent** – multilingual rules decide clear cases; texts they cannot decide go to the model in one batched, cached call, and `code/verify_intent.py` cross-checks every rule decision.

The LLM layer is provider-pluggable (`code/ghost_audit/llm.py`):

| Provider | Vision model | Text model | Select with |
| --- | --- | --- | --- |
| **Groq** (default, free tier available) | `qwen/qwen3.8-27b` | `openai/gpt-oss-20b` | `GHOST_LLM_PROVIDER=groq` |
| Anthropic (optional) | `claude-sonnet-5-5` | `claude-haiku-5-5` | `GHOST_LLM_PROVIDER=anthropic` |

Models can be overridden with `GHOST_VISION_MODEL` and `GHOST_TEXT_MODEL`. Calls retry on rate limits (honouring `retry-after`), and every call is recorded with its provider, model and token usage in `code/cache/llm_usage.jsonl`.

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv
.venv/Scripts/pip install -r code/requirements.txt      # Windows
.venv/bin/pip install -r code/requirements.txt          # macOS / Linux
```

The challenge dataset is not included in this repository. Copy the `dataset/` folder from the challenge repository into the project root:

```bash
git clone https://github.com/interviewstreet/hackerrank-orchestrate-october26.git
cp -r hackerrank-orchestrate-october26/dataset ./dataset
```

Optional, for the AI steps: create `.env` in the project root (see `code/.env.example`) with a free [Groq API key](https://console.groq.com/keys):

```text
GROQ_API_KEY=your-key
```

To use Anthropic instead, set `GHOST_LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY`, and `pip install anthropic`.

## Usage

| Command | What it does |
| --- | --- |
| `python code/main.py` | Audit all 518 evaluation requests and write `output.csv` |
| `python code/main.py --split sample` | Audit the 32 sample requests into `runs/sample_predictions.csv` |
| `python code/evaluation/main.py` | Solve the samples and score them (`runs/sample_score.md`, `runs/sample_diff.md`) |
| `python code/extract_images.py` | Read the screenshots with the vision model and cache the results |
| `python code/evaluation/run_custom_cases.py` | Run the team-built evaluation cases against their hand-derived answers |
| `python code/evaluation/verify_screenshots.py` | Check cached screenshot extractions against hand-read facts |
| `python code/evaluation/usage_report.py` | Regenerate `usage_report.md` from the token-usage ledger |
| `python code/package_submission.py` | Build `code.zip` and a secret-redacted `agent-trace.zip` |
| `python code/verify_intent.py` | Cross-check every intent decision with the text model |
| `python code/inspect_evidence.py --case case_17` | Show a case's identity, linked records and evidence |
| `python code/inspect_evidence.py --resource account_005` | Show every observation of one record |
| `python -m unittest discover -s code/tests` | Run the test suite |

## Output

`output.csv` has one row per request, sorted by `case_id`:

```text
case_id,deletion_status,steps_json
```

Each step in `steps_json`:

```json
{"step_no": 1, "phase": "FINDING", "system_id": "search_service", "resource_id": "search_052",
 "operation": "REPORT_REMAINING_RECORD", "state": "PRESENT", "evidence_id": "evidence_03"}
```

Steps are ordered by phase (findings, remediation, verification), then by operation, `system_id` and `resource_id`. Findings cite the evidence that proves them; remediation and post-remediation checks describe future work and carry an empty `evidence_id`.

## Project structure

```text
code/
├── main.py                    entry point: audit requests, write output.csv
├── extract_images.py          one-time screenshot extraction (vision model, cached)
├── verify_intent.py           LLM cross-check of request intent
├── inspect_evidence.py        debugging views of evidence, records and cases
├── requirements.txt
├── ghost_audit/
│   ├── evidence/              evidence normalizer
│   │   ├── model.py           Observation / Coverage model and record mapping
│   │   ├── sql_snapshots.py   SQLite snapshots (primary and replica copies)
│   │   ├── records.py         JSON exports and text handover notes
│   │   ├── logs.py            six log syntaxes joined by trace id
│   │   ├── documents.py       PDF text extraction
│   │   ├── images.py          cached screenshot extractions
│   │   └── catalog_links.py   data-catalog identity links
│   ├── identity.py            time-aware identity resolution
│   ├── intent.py              deletion vs non-deletion requests
│   ├── context.py             per-case evidence linking
│   ├── linker.py              workflow-activity linking and account assignment
│   ├── state.py               state reconstruction at audit_at
│   ├── policy.py              deletion_rules.csv application
│   ├── planner.py             status and ordered steps
│   ├── solver.py              pipeline orchestration
│   ├── llm.py, config.py      provider-pluggable LLM client (Groq / Anthropic) and settings
│   ├── schema.py, output.py   output contract and serialization
│   └── validate.py            contract validator
├── package_submission.py      builds code.zip and a redacted agent-trace.zip
├── cache/                     cached screenshot extractions and the token-usage ledger
├── evaluation/
│   ├── main.py                local scorer CLI
│   ├── scorer.py              status, step, field, sequence, identity and evidence metrics
│   ├── run_custom_cases.py    team-built cases (custom_cases/) against hand-derived answers
│   ├── verify_screenshots.py  screenshot extractions against hand-read facts
│   ├── usage_report.py        writes usage_report.md from the ledger
│   ├── evaluation_report.md   accuracy, custom cases, failures found and improvements
│   └── usage_report.md        model calls, tokens and cost
└── tests/                     regression tests
output.csv                     predictions for the 518 evaluation requests
log.txt                        development session log
```
