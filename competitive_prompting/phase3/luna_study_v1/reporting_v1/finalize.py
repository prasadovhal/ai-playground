"""Reproduce the partial report and read-only postflight audit, without API calls."""
from collections import Counter
import json

from .run import main
from .audit import audit
from ..plan import HERE
from ..runner import rows, complete


def finalize():
    execution = main()
    checked = audit()
    records = rows()
    evaluable = [r for r in records if complete(r)]
    failed = [r for r in records if not complete(r)]
    errors = [e for r in failed for e in r['api_errors']]
    counts = Counter(r['category'] for r in records)
    # This addendum describes this specific terminated run, not a future resume.
    assert execution['status'] == 'PARTIAL_INFRASTRUCTURE'
    assert len(records) == 8 and len(evaluable) == 7 and len(failed) == 1
    assert all(r['verified_task_success'] and r['state_correct'] for r in evaluable)
    assert checked['paid_requests_attempted'] == 91 and checked['acknowledged_responses'] == 90
    assert errors[0]['code'] == 'invalid_prompt'
    note = f'''
## Postflight evidence and continuation blocker

This is a terminated partial study, not a completed 500-task experiment. There
are {len(evaluable)} infrastructure-complete episodes, all seven operationally
successful with correct business states and reports, and one provider-error
episode. Only the first task has a complete ABCD block. The second task's A/B/D
records remain diagnostic data but are excluded from paired comparisons.
Attempted categories: {dict(counts)}; recovery and stopping were not reached.
There are no evaluable limit hits or truncations. One unnecessary call occurred
in an evaluable D episode. Seven successes cannot establish a ceiling effect.

The rejected C episode still contains genuine observed decision errors before
the provider rejection: it repeated already-completed op4 (count 1 to 2), then
attempted an operation with unsatisfied prerequisites. Its retained prefix has
15 tool calls, four unnecessary calls, three constraint violations, one duplicate
operation and one incorrect retry, with an incorrect final business state.
These observations are not erased, but the episode is classified as a provider
failure, not an operational failure, under the frozen denominator rules. They
cannot establish a prompting effect or be substituted into the complete pair.

For the sole complete task, C-B operational, final-state and final-status
differences are all zero. C used four more tool calls, 7,678 more total tokens,
$0.000166815 more cost and 11.1195541 more seconds. Neither condition made an
unnecessary call on that task. These are single-task descriptions, not evidence
of a population activity/correctness trade-off. No p-value, interval, statistical
significance, equivalence or generalization claim is supported.

The audit matched {checked['paid_requests_attempted']} requested payloads,
{checked['acknowledged_responses']} distinct acknowledged response IDs, 182
journal events and all ledger reservation IDs. Known acknowledged usage is
127,744 input tokens (107,997 cached and 14,961 cache-write), 2,924 output tokens,
including 903 reasoning tokens. Reasoning is not added to output a second time.
The rejected request's usage and charge remain unknown; totals are lower bounds
for all attempted requests. Its full $0.02 remains pending in addition to
historical encumbrances of $0.068950. Known cumulative experiment spending is
$5.454256125; exposure including all unresolved holds is $5.543206125.

Provider error evidence (non-sensitive):

```json
{json.dumps(errors, indent=2)}
```

Pending reservation: `091f3198f854487a967146c812c33571`, request sequence 16,
episode `phase3:luna-dag-v1:luna1_execution_f061_v2:0:C`. HTTP 400 is not assumed
free. Reconciliation requires externally verified billing evidence matching the
project/request/time, then an auditable append-only settlement for that exact
charge. No settlement, release, new encumbrance designation, prompt edit or
retry has been made. An incomplete episode also blocks ordinary resumption;
accounting reconciliation alone does not authorize duplicating its 15 already
acknowledged responses. Future continuation needs an audited recovery plan and
resolution of the provider rejection, while retaining the frozen study and
its predeclared stopping rule. Do not run the paid command as an automatic retry.

The frozen analysis had a missing `pathlib.Path` import in CSV output. The
separately versioned reporting wrapper supplies only that dependency; task,
simulator, scoring, source hashes and inference gates remain unchanged. All
568 protected prior files and the historical ledger byte prefix are intact.
Reporting-source hashes and the literal manifest file hash are recorded in
`postflight_audit.json`. `manifest_sha256` elsewhere denotes its canonical content
hash. No new paid calls occur during reporting or tests.

The predeclared power analysis remains valid as planning, not an observed result:
at five tasks/family and prior paired ICC about .358, N500 has about 49% power
for a five-point effect. Simulation-checked sufficient sizes for 80%/90% power
were 1,095/1,455 tasks at 5 points, 900/1,185 at 5.56 points, 480/645 at 7.5 points,
and 255/330 at 10 points. They are conservative checked sizes, not exact minima.
Results depend on discordance/correlation assumptions estimated from few prior
tasks. Exact topology uniqueness does not prove independent scenario families.

Use this corrected offline reproduction command:

```powershell
competitive_prompting/.venv/Scripts/python.exe -m competitive_prompting.phase3.luna_study_v1.reporting_v1.finalize
competitive_prompting/.venv/Scripts/python.exe -m pytest competitive_prompting -q
```
'''
    report = HERE / 'analysis/final_experiment_report.md'
    text = report.read_text(encoding='utf-8')
    text = text.replace('competitive_prompting.phase3.luna_study_v1.analysis',
                        'competitive_prompting.phase3.luna_study_v1.reporting_v1.finalize')
    text = text.replace('ICCâ‰ˆ.358', 'ICC approximately .358')
    report.write_text(text + note, encoding='utf-8')
    validation = HERE / 'analysis/validation_report.md'
    with validation.open('a', encoding='utf-8') as f:
        f.write('\nPostflight journal/ledger audit passed: 91 request IDs, 90 unique acknowledged '
                'responses, one pending request; all usage and recorded costs reconcile. '
                'The original frozen analysis import issue is isolated in reporting_v1. '
                'See postflight_audit.json and the report addendum for errors/exclusions.\n')
        log = (HERE / 'postflight_pytest.txt').read_text(encoding='utf-8-sig')
        result = next((s for s in reversed(log.splitlines()) if ' passed in ' in s), None)
        if result:
            f.write('\nPostflight pytest: ' + result + '. Preflight: 2062 passed; '
                    'all 2000 scripted episodes and 500 blocks validated before paid execution.\n')
    return execution


if __name__ == '__main__':
    finalize()
