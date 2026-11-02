# hard-v1 balanced exploratory execution

The user elected to proceed despite development ceilings and cost uncertainty.
This prepares eight batches; it does not authorize or issue paid calls. The original
one-repetition manifest is unchanged. No prompt, task, model, system instruction,
schema, simulator, scorer, limit, price or cap was edited.

`execution_plan_v1.json` is write-once and binds the original semantic/file hashes,
source hash, development record hashes, intended project hash, added orchestration
source hash, complete batch/block/condition order and budget admission estimates.
Its semantic SHA256 is
`27ce41c40501e03e3e68217fe096b2ddcecc615acff603e46d66c1bfe135d658`.
Loading recomputes the plan and checks the original frozen manifest and calibration
review. Changing execution code requires a new versioned plan; never overwrite v1.
The new code lives in a subpackage so original source hashes remain valid.

## Predeclared order and estimates

Within each original stage/category, the first task appearing in the parent plan
goes into the odd batch and the second into the next even batch. A stable partition
preserves parent block order inside each batch. Each episode object, episode ID and
four-condition randomization order is retained verbatim, including its parent order
field. This partition does not use model outcomes or costs to choose membership.

Every batch has three distinct tasks, both models and all four original conditions:
24 episodes, six four-condition blocks. A task appears in one batch only.

| Batch | Execution | Recovery | Stopping | Forecast USD | Cumulative USD | Conservative USD |
| --- | --- | --- | --- | ---: | ---: | ---: |
| 1 | entitlement_rollout | inventory_commit | logical_release_hold | 0.4280 | 0.4280 | 1.8252 |
| 2 | archival_job | renewal_validate | revoked_processing | 0.3711 | 0.7990 | 1.5643 |
| 3 | catalog_publish | publish_retry_chain | approval_boundary | 0.3662 | 1.1652 | 1.5211 |
| 4 | course_enrollment | release_dual_faults | partial_dispatch | 0.4102 | 1.5753 | 1.6599 |
| 5 | shipping_wave | receipts_lost_ack | immutable_history | 0.3558 | 1.9312 | 1.5019 |
| 6 | equipment_returns | onboarding_partial | export_dependency | 0.3845 | 2.3157 | 1.6629 |
| 7 | invoice_batch | backup_scan | locked_settlement | 0.3521 | 2.6678 | 1.4823 |
| 8 | payroll_cycle | warehouse_outages | unavailable_printing | 0.3452 | 3.0130 | 1.4973 |

Forecast uses the two neutral development tasks per model/category, scaled for the
frozen larger-task/growing-history workload, plus 50%. Conservative estimates use
the larger observed no-discount cost ratio, price input at the unchanged 30m write
rate, add 50% and 256 output tokens per oracle request. Total conservative cost is
$12.7149. These are scenarios, not confidence bounds. Exact per-batch/cumulative
values and largest block allowances are in `cost_estimates_v1.csv`; forecasts inside
the original manifest remain intact.

## Budget and stopping

Remaining Phase 3 exposure is $5.880950570. Confirmed experiment spending is
$0.224730730; historical unverified encumbrance remains $0.048950, counted once.
Global exposure is $0.273680730, leaving $8.726319270 under the $9 experiment cap.
Phase 3 exposure, including $0.119049430 development cost, remains capped at $6.
All real requests use the original shared ledger and reservation/settlement code.
These controls protect this experiment, not the entire API account balance.

Before EACH task/model four-condition block, the runner checks headroom for that
block's conservative estimate PLUS the maximum permitted single-request reservation.
This adds liquidity for the existing per-request guard. For example, the first
block needs $1.224496132. Admission is not a second reservation or a confirmed charge.
It cannot guarantee completion under arbitrary token tails. No call bypasses the
original guard. The paid process holds the original ledger lock throughout a batch.

Insufficient block headroom starts zero episodes in that block and stops the batch.
Unknown API usage retains its reservation and stops immediately. API, replay,
evaluator and unexplained incomplete-response failures also stop. Raw traces,
admission decisions, errors, validation and missing coverage are preserved.
Ordinary agent failures, including limits and configured output truncation, stay
operational failures and do not prevent the remaining conditions from running.
The CLI exits nonzero on incomplete/infrastructure batches. Later batches require
complete prior coverage and replay audits, the same plan and the same project.
No skipped conditions, task substitutions, automatic retries or selective resumption
are implemented. Never create alternate outputs to rerun attempted episode IDs.

## Commands

Run from `D:\Study\Git_repo\ai-playground`. Existing `OPENAI_API_KEY` and
`OPENAI_PROJECT_ID` must be securely exported for paid execution; `.env` is not
loaded automatically and credentials are never printed. The project must match
neutral development; both exact model IDs are checked without substitution.

Stage 1 / batch 1, **paid, only after separate execution approval**:

```powershell
competitive_prompting/.venv/Scripts/python.exe -m competitive_prompting.phase3.balanced.runner --plan competitive_prompting/phase3/balanced/execution_plan_v1.json --batch 1 --execute --output-dir competitive_prompting/phase3/results/balanced_v1
```

Use `--batch 2` through `--batch 8` in order, with the same plan and output root.
Each invocation runs one batch only. Omitting `--execute` runs the oracle script
offline. The eight offline batches have already been validated under that output
root; repeating offline validation requires a different output root.

Combine live records using the balanced analysis module:

```powershell
competitive_prompting/.venv/Scripts/python.exe -m competitive_prompting.phase3.balanced.analysis path/to/batch1/episodes.jsonl path/to/batch2/episodes.jsonl --output-dir competitive_prompting/phase3/results/balanced_v1/combined_live
```

Analysis rejects duplicate/unplanned IDs, changed settings/source/plan hashes and
mixed live/offline records. Coverage reports all 192 planned episodes, unobserved
conditions and incomplete four-condition blocks. Only complete infrastructure-valid
blocks enter paired contrasts, even if B/C alone were recorded. All ordinary agent
failures stay in those blocks. Descriptive summaries retain original exclusions,
limit rates and operational denominators.

## Separate outcome diagnostics

* `verified_final_state_correct`: independent exact gold-state match from the saved
  trace/state; does not depend on what the agent reports or whether it invoked verify.
* `final_report_status_correct`: schema-valid final status equals frozen expected
  status. This is target-status correctness; it is not a judge of free-text reasoning.
* `verification_current`: latest successful verification still matches final state.
* `report_consistent_with_verification`: final status agrees with a fresh successful
  verification; unavailable when no current verification exists. A target-status
  match with unfinished work is distinguished by state/verification diagnostics.
* `operational_task_success`: the unchanged scorer and original denominator, including
  incorrect execution/reports, limits and output truncation as failures.

Primary contrast remains C−B. D−C, B−A, C−A and D−A remain secondary; combined rivalry
is exploratory. Reports include model/category/task summaries, activity, incorrect
retries, duplicates, violations, tokens, cost and latency. Reasoning remains part of
output tokens and is never charged twice. Paired intervals bootstrap tasks within
category only with at least eight paired tasks. They are conditional exploratory
benchmark intervals: shared templates limit independence and generalization. A zero
observed difference is not evidence of equivalence. Development ceiling effects
remain a risk, and early budget stopping may leave unbalanced model/category coverage.
