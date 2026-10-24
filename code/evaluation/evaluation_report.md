# Evaluation Report — Ghost Data Deletion Audit

All numbers below are reproducible with the commands in each section. The hidden grader's exact
formula is unknown, so the local scorer (`code/evaluation/scorer.py`) reports every dimension the
problem statement names.

## 1. How the solution is evaluated

| Check | Command | What it measures |
| --- | --- | --- |
| Public samples | `python code/evaluation/main.py` | Solves the 32 sample requests and scores them against `sample_output.csv` |
| Custom cases | `python code/evaluation/run_custom_cases.py` | 4 team-built cases with hand-derived answers (section 4) |
| Screenshot facts | `python code/evaluation/verify_screenshots.py` | Every cached screenshot extraction against facts read from the images by hand |
| Intent cross-check | `python code/verify_intent.py` | Rule-based intent decisions against an LLM on every unique request text |
| Contract | `python code/main.py` | Validator: header, 518 rows, order, JSON, step numbering, phases, operation/state pairs, citation rules |
| Regression tests | `python -m unittest discover -s code/tests` | 43 tests, including a guard that the sample score does not drop |

**Metric definitions.**

- *Case-level status accuracy*: share of cases whose `deletion_status` equals the answer.
- *Exact row match*: status and the full ordered `steps_json` equal the answer.
- *Step-level P / R / F1*: each step is compared as the tuple (phase, operation, system_id, resource_id, state, evidence_id); micro-averaged over all cases. Also reported without `evidence_id`.
- *Field-level accuracy*: for each field, the share of step positions where prediction and answer agree (positions missing on either side count as wrong).
- *Sequence accuracy*: exact order match, longest-common-subsequence ratio, and pairwise order agreement of shared steps.
- *Identity resolution*: the account resolved for the case equals the answer's account.
- *Resource discovery*: P / R / F1 over the (system_id, resource_id) pairs a case touches.
- *Evidence citation accuracy*: share of cited answer steps reproduced with the same `evidence_id`.

## 2. Results on the 32 public samples

| Metric | Value |
| --- | --- |
| Case-level status accuracy | **100.0%** (10/10 COMPLETE, 22/22 INCOMPLETE) |
| Exact row match | **90.6%** (29/32) |
| Step-level micro F1 (all fields) | **99.2%** (precision 100.0%, recall 98.4%) |
| Step-level micro F1 (ignoring evidence_id) | 99.2% |
| Mean positional field accuracy | 96.6% |
| Sequence exact match | 90.6% |
| Mean sequence LCS ratio | 97.8% |
| Mean pairwise order agreement | 100.0% |
| Account identity resolution | 96.9% |
| Resource discovery micro F1 | 98.6% (precision 100.0%, recall 97.3%) |
| Evidence citation accuracy | 98.7% |

**Field-level accuracy (by step position)**

| phase | system_id | resource_id | operation | state | evidence_id |
| --- | --- | --- | --- | --- | --- |
| 98.4% | 95.9% | 95.9% | 95.9% | 95.9% | 97.9% |

**Step F1 by phase**: FINDING 100%, REMEDIATION 100%, VERIFICATION 98.6% (precision 100%, recall 97.3%).

**Recall by operation**: every finding and remediation operation is reproduced exactly (21/21
REPORT_REMAINING_RECORD, 11/11 REPORT_EXPOSED_PII, 6/6 REPORT_ACTIVE_DATA_FLOW, 3/3 REPORT_RESTORE_RISK,
and all 41 remediation steps). Verification: VERIFY_RECORD_ABSENT 49/50, VERIFY_RECORD_PRESENT 22/24,
all other verification operations 100%.

### Remaining sample differences

The three differing cases each miss one verification step for a record that appears **nowhere in
the participant-visible data** (no evidence file, activity row or audit entry mentions it), so it
cannot be derived:

| Case | Missing answer step | Why it cannot be derived |
| --- | --- | --- |
| case_04 | VERIFY_RECORD_ABSENT account_004 | `account_004` appears in no file and no activity row |
| case_18 | VERIFY_RECORD_PRESENT audit_record_6645 | appears only in `sample_output.csv` |
| case_20 | VERIFY_RECORD_PRESENT audit_record_5569 | appears only in `sample_output.csv` |

No predicted step is wrong (precision 100%).

## 3. Evaluation-set profile (518 requests)

There are no answers for the evaluation set, so it is checked for consistency rather than accuracy.

| Property | Value |
| --- | --- |
| Rows / contract errors / warnings | 518 / 0 / 0 |
| Status | INCOMPLETE 355, COMPLETE 153, NOT_APPLICABLE 10 |
| Claimed COMPLETE but actually INCOMPLETE | 163 of 254 |
| Claimed NOT_STARTED but actually COMPLETE | 26 of 112 |
| Requests made from a historical email | 100 (all resolved) |
| Cases where look-alike decoy emails were present and rejected | 14 |
| Steps written | 3,248 |
| Cases citing a single evidence file | 507 of 508 with citations |
| Evidence parsed | 94 files → 2,933 observations, 219 complete-listing entries, 0 parse problems |

**How each case's account was resolved**

| Method | Cases |
| --- | --- |
| Evidence and workflow activity agree | 234 |
| Workflow activity, unique after elimination | 180 |
| Workflow activity, decided by run timing | 51 |
| Evidence only | 2 |
| Best guess between two equally consistent accounts | 31 |
| No account in the data (hand-built and catalog cases) | 10 |

**Known uncertainties**

- *31 best-guess accounts* (about 15 pairs): two cases share a run window, a claimed status and the exact
  timing phase, and neither candidate account appears in any evidence. Request order was tested as a
  tie-breaker on 157 resolved pairs and showed no relation (81 vs 76), so it was not used. A wrong
  guess costs one account step per case.
- *Legal holds (8 cases)*: retained under rule 13 with a `VERIFY_RECORD_PRESENT` step. No sample case
  contains a legal hold, so this follows the rules but is untested against answers.
- *case_63*: the data catalog links `marketing_001` to the requester, while the screenshot shows
  contact details (`sj.park@naver.com`) that belong to no known customer. Unknown details cannot prove
  the record belongs to someone else, so the record is kept and reported as remaining. A record is
  disowned only when its content shows a *different known customer* (unit-tested).

## 4. Custom evaluation cases (team-built)

Four cases in `code/evaluation/custom_cases/` (`requests.csv`, `expected_output.csv`). Expected
answers were derived by hand from the raw evidence, not from the solver.

| Case | Scenario | What it tests | Expected | Result |
| --- | --- | --- | --- | --- |
| custom_01 | Sample case_12's person writes from their **old** email `minjun.kim90@gmail.com` | Historical identity resolution; a record keyed only by the old email's sha256 hash | INCOMPLETE, 5 steps (delete `analytics_9914`) | **exact** |
| custom_02 | Sample case_24's person, `KEIKO.NAKAMURA@GMAIL.COM`, request in German | Case-insensitive identity; multilingual deletion intent | INCOMPLETE, 7 steps | **exact** |
| custom_03 | Spanish request for a copy of the data that says *no borren nada* | Non-deletion detection in another language despite a deletion word | NOT_APPLICABLE, `[]` | **exact** |
| custom_04 | Haruka (user_11) audited on 20 Sep instead of 27 Sep | Time cutoff: the 26 Sep screenshot showing her account again must be ignored; a support attachment carries her **old** phone | INCOMPLETE, 5 steps (account absent, attachment remaining) | **exact** |

Result: 4/4 exact, status accuracy 100%, step F1 100%, 0 contract errors.

## 5. Screenshot extraction check

The 11 admin-console screenshots are read once by `qwen/qwen3.8-27b` on Groq and cached.
`verify_screenshots.py` holds 25 facts read from the images by hand (ids, emails, user ids, phones, states).

| Version | Exact records | Problems found |
| --- | --- | --- |
| Full image only (smoke test) | — | phone `+81 90 7610 2507` read as `+81 90 180 2507`; order numbers returned as records; on-screen dates used as observation time |
| Full image + two zoomed halves | 23/25 | `kagyeman8947@fastmail.com` read as `kagayeman…` (stable across re-runs); `summit-retail@company.com` read as `.co` |
| + snapping to the single known email within two edits | **24/25** | remaining slip is a business email on a backup no case uses |

Look-alike decoys (`name.jr@…`) are three edits from the real address, so snapping never moves a
decoy onto a customer.

## 6. Intent cross-check

Multilingual rules decide all 398 unique request texts (388 deletion, 10 non-deletion: cases 533–542),
so the production run makes no intent calls. `verify_intent.py` asks `openai/gpt-oss-20b` on Groq to judge
every text independently; see `runs/intent_crosscheck.md` for the full list.

| Unique texts | Judged by the LLM | Rules and LLM agree | Disagreements |
| --- | --- | --- | --- |
| 398 | 398 | **398** | 0 |

The 10 non-deletion requests (phone or name correction, marketing opt-out, data copy, login help,
email change, information request) are recognised by both, including texts that mention deletion
only to rule it out.

## 7. Failures found and improvements made

| # | Failure found | Fix | Effect (samples) |
| --- | --- | --- | --- |
| 1 | Baseline trusted the workflow's claimed status (status 56.2%, exact 0%, step F1 6.4%) | Rebuilt the solver on evidence: state at `audit_at`, policy, plan | status 56.2% → 84.4%, exact 0% → 56.2% |
| 2 | Curated snapshot bundles treated as complete listings of a whole system: 18 citations of a file captured after `audit_at`, wrong absences | Absence by omission only from full table dumps, or from curated exports that include the customer; never cite files captured after `audit_at` | status 84.4% → **100%**, exact 56.2% → **90.6%** |
| 3 | Accounts with no evidence cited workflow claims in 45 COMPLETE eval cases | Customer snapshots that include the person prove absence; cite the newest file captured by `audit_at` | claim citations 45 → 0 |
| 4 | Activity-timing window too wide: 3 wrong extra links (`search_428`, `analytics_3264`) and a wrong account in case_23 | Separate before/after-report timing windows measured on the samples; tie-break by typical offset per action | account resolution 30/32 → 31/32, false links 3 → 0 |
| 5 | Same person with two requests (user_17, March and August) leaked an earlier account | Ignore evidence older than the person's previous request's audit | linking precision → 100% |
| 6 | Request-order tie-breaker for ambiguous accounts | Tested on 157 resolved pairs: 81 vs 76, no signal | rejected |
| 7 | 9 hand-built eval cases produced no steps | Use `LINK_RESOURCE_IDENTITY` catalog rows as identity evidence and their display references as aliases | 9 → 0 empty cases |
| 8 | Screenshot misreads (section 5) and a tombstone labelled "ACTIVE" | Zoomed views, resource-id filter, capture time, conditions must fit the record type, email snapping | 24/25 facts exact |
| 9 | Alias matching in PDF text linked `tombstone_17` to six unrelated sample cases (a register lists `user_17`) | Only record-specific aliases, matched as whole words | regression 71.9% → back to 90.6% exact |
| 10 | Catalog link contradicted by screenshot content (case_63) | Disown only when content shows another known customer; unknown details noted | covered by 3 unit tests |
| 11 | An ambiguous account (case_103) was actually tied to another person by later evidence | Rule out accounts that any evidence ties to a different person (used for exclusion only) | 1 fewer best guess |

## 8. Limitations and next steps

- About 15 account pairs are genuinely indistinguishable from the supplied data.
- Legal-hold handling follows rule 13 but no answer key covers it.
- The vision model is a preview model on Groq; re-extraction with another model should be followed by
  `verify_screenshots.py` (the checker is model-independent).
