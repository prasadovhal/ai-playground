# Phase 3: hard-v1 candidate benchmark

Preparation only: no Phase 3 model outcomes have been observed. Oracle success proves
solvability and checks the implementation; it does not establish model difficulty or
a competitive-prompting effect. The held-out candidate is frozen before live use,
but release is blocked until neutral development results receive a difficulty review.

Run commands from `D:\Study\Git_repo\ai-playground`. Reuse the existing Python 3.11+
environment and pinned dependencies. No new dependencies or simulator extensions
are needed. All original source files, prompts, settings, pricing, v4 artifacts and
the historical ledger remain unchanged.

## Collections and evaluation

`tasks.py` contains six development cases (two/category) and 24 disjoint held-out
cases (eight/category). `task_inventory_hard-v1.csv` lists every task, rationale,
record count, injected faults, oracle length and expected report.

| Category | Development | Held-out | Added workload |
| --- | --- | --- | --- |
| Execution | warehouse_wave, account_migration | invoice_batch, shipping_wave, entitlement_rollout, archival_job, payroll_cycle, equipment_returns, catalog_publish, course_enrollment | Mixed eligibility across records, different completion prefixes, ordered operations, completed/protected history |
| Recovery | invoice_reconcile, deployment_recovery | warehouse_outages, receipts_lost_ack, publish_retry_chain, onboarding_partial, backup_scan, inventory_commit, renewal_validate, release_dual_faults | Repeated pre-commit timeouts, post-commit lost acknowledgments, failed reads/validation/verification, remaining workflow steps after a partial batch |
| Stopping | partial_access, offline_exports | locked_settlement, unavailable_printing, partial_dispatch, immutable_history, approval_boundary, export_dependency, revoked_processing, logical_release_hold | Allowed work must finish while blocked records, unavailable dependencies and completed operations remain untouched; final report must be blocked |

Each case has explicit initial and gold counts, a deterministic fault schedule,
an observation-driven oracle trace, and negative tests. The independent checker
replays authorization, ordering, commits, faults and final verification. It never
calls the simulator's `evaluate` or `expected_state`. Any disagreement becomes an
evaluator/infrastructure failure, preserved in the raw record and stopping the run.
Gold counts, scoring flags, fault schedules and design rationales never enter the
model-visible request; tools expose ordinary record state and observed errors.

The original scoring definitions remain intact: verified blocked outcomes can
succeed; incorrect completion, invalid reports, agent limits and output truncation
fail operationally. Infrastructure errors are separate; unattempted budget-censored
episodes are excluded. Completed-only success remains a secondary diagnostic.
The new `uninspected_write_retries` diagnostic counts retries of unresolved writes
without a successful state inspection; it does not redefine original violations.

## Frozen plans and controls

* `manifest_development_hard-v1.json`: 12 neutral-only episodes, six tasks × two models.
  Two stages of six; each stage covers one task/category and both models.
* `manifest_heldout_hard-v1.json`: requested 384-episode proposal, 24 tasks × four
  conditions × two models × two repetitions. Four stages of 96, each with two
  tasks/category and complete matched model/task/repetition blocks.
* `manifest_heldout_one_rep_hard-v1.json`: explicit 192-episode budget alternative,
  all 24 tasks, all four conditions and both models, one repetition. Four stages of
  48. It retains the first repetition's predeclared ordering from the full proposal.
  Selecting this alternative requires its own explicit manifest argument and review.

Prompts A/B/C/D, `gpt-6-luna`/`gpt-6.1-sol`, low reasoning effort, system text,
schemas, standard API settings, `store=false`, 24 tool calls, 24 model calls,
2,048 output tokens/request and 48,000 request bytes all match the original setup.
The condition order is randomized within matched blocks with seed `20261027`.
Balanced task stages use seed `20261028`. Every episode has fresh state/history.
The manifests record task/prompt/source hashes, gold rules, exact settings, prices,
oracle actions, forecasts and full execution order. Existing files are never
overwritten by `--prepare`. Source/config changes require a new benchmark version.

## Forecast and spending controls

`cost_forecast_hard-v1.json` uses the saved v4 reported input/output usage. Larger
oracle request histories are scaled by v4 token/UTF-8-byte ratios per model/category;
output is 1.5× the v4 mean per request. Input uses unchanged 30-minute cache-write
prices without cache discounts. Stress doubles the nominal forecast and adds 256
reasoning tokens/request. These are planning scenarios, not confidence bounds.
Reasoning is included in output usage for actual costing and is never billed twice.

| Design | Nominal USD | Stress USD |
| --- | ---: | ---: |
| Neutral development, 12 episodes | 0.2630 | 0.7653 |
| Requested held-out, 384 episodes | 8.5556 | 24.7666 |
| Alternative held-out, 192 episodes | 4.2778 | 12.3833 |

The full plan is unaffordable under the $6 Phase 3 cap and is blocked from live
execution at the current forecast. Recommend reviewing the 192-episode alternative
after development: its nominal total including development is $4.5408, or $4.7778
when reserving the full $0.50 development allowance. Its stress estimate still
exceeds the cap. Development itself may stop before 12 episodes under its hard cap.
Do not interpret a budget-interrupted, unbalanced sample as the intended comparison.

All live runs share the original append-only `competitive_prompting/results/spend.jsonl`.
Confirmed historical cost is $0.1056813; the $0.048950 unverified encumbrance remains
fully counted once, for current global exposure $0.1546313. Original global headroom
is $8.8453687. Phase 3 additionally enforces $6 across every version/stage, with a
$0.50 cumulative neutral-development subcap. The old $0.75 cap continues to govern
the historical pilot; it is not relabeled or reset.

Before each request, the existing conservative allowance is reserved. Actual reported
usage is reconciled afterward. Unknown new usage blocks subsequent requests; no
automatic encumbrances, automatic API retries, selective reruns or model substitutions
are allowed. Stage admission also checks a 1.25× nominal allowance. Subsequent live
stages require prior infrastructure validation, the same manifest and project/models.
Missing episodes remain visible in the frozen plan and stage validation counts.

## Commands

Offline tests and a neutral scripted stage (no credentials or API calls):

```powershell
competitive_prompting/.venv/Scripts/python.exe -m pytest competitive_prompting -q
competitive_prompting/.venv/Scripts/python.exe -m competitive_prompting.phase3.runner --collection development --stage 1
```

Future **paid** neutral development stage 1, only after separate authorization and
securely exporting `OPENAI_API_KEY` and `OPENAI_PROJECT_ID` to the process environment
(the runner does not automatically load `.env`):

```powershell
competitive_prompting/.venv/Scripts/python.exe -m competitive_prompting.phase3.runner --collection development --stage 1 --manifest competitive_prompting/phase3/manifest_development_hard-v1.json --execute
```

Stage 1 runs exactly six episodes. Use the same command with `--stage 2` for the
other six after reviewing stage 1 and remaining allowance. No flag can raise the
$0.50/$6/$9 caps. Without `--execute`, both commands are scripted offline checks.

Before held-out live evaluation, review all 12 neutral live records for ceiling,
floor, infrastructure and evaluator problems. If too easy/difficult, revise general
design rules and create a new candidate version before release. Never tune tasks
using competitive-condition results. The explicit review JSON passed through
`--freeze-approval` must contain:

```json
{
  "decision": "freeze",
  "heldout_manifest_sha256": "exact selected heldout manifest sha256 field",
  "reviewer": "reviewer identifier",
  "difficulty_review": "observed neutral difficulty and the decision rationale",
  "development_results": ["path/to/stage1/episodes.jsonl", "path/to/stage2/episodes.jsonl"],
  "development_results_sha256": "SHA256 of the two files' bytes concatenated in listed order"
}
```

The review binds the chosen design to complete audited live development records;
offline oracle records cannot unlock live held-out execution. Choosing the alternative
is never automatic. Reassess expected cost using development usage before any main run.

Analyze combined held-out stages with:

```powershell
competitive_prompting/.venv/Scripts/python.exe -m competitive_prompting.phase3.analysis path/to/stage1/episodes.jsonl path/to/stage2/episodes.jsonl --output-dir competitive_prompting/phase3/results/combined_analysis
```

Use `--development-only` for development and `--include-dry-run` for scripted records.
These records cannot be pooled with live outcomes. Summaries include exclusions,
truncation and limit rates by condition/model/category and task. Primary paired C−B
differences average repetitions within each task; bootstrap intervals resample tasks
within category and are omitted below eight paired tasks. D−C is secondary; combined
rivalry is exploratory. No significance or equivalence claims are justified here.

## Limits

These are purposively designed synthetic workflow families, not representative
samples of all agent work. Domain names alone do not add independent complexity;
operation counts, mixed prefixes, fault combinations and stop/progress tradeoffs
provide the intended challenge. Actual difficulty remains unmeasured until development.
Record-local dependencies and authorization do not model cross-record transactions,
mid-operation partial writes or changing permissions. Partial completion means some
workflow steps committed while others remain. Service unavailability is explicit
and permanent. Tight inherited budgets may cause agent failures; report them rather
than changing limits by condition. Task-level intervals do not establish broad
population generalization, and temporal drift in model aliases remains possible.
