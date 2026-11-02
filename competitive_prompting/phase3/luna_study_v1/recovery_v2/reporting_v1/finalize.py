"""Add audit and interpretation notes to the completed endpoint report offline."""
import csv
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
OUT = HERE / 'analysis'
MARKER = '\n## Final interpretation and accounting notes\n'


def main():
    with (OUT/'paired_comparisons.csv').open(newline='',encoding='utf-8') as f:
        rows=list(csv.DictReader(f))
    primary=next(r for r in rows if r['category']=='all' and r['contrast']=='CB'
                 and r['metric']=='operational_task_success')
    assert primary['paired_tasks']=='500' and primary['endpoint_reached']=='True'
    report=OUT/'final_experiment_report.md'
    text=report.read_text(encoding='utf-8').split(MARKER)[0]
    text=text.replace('Each continuation is linked to the original planned slot and carries\nan execution-attempt identifier.',
        'Each continuation is linked to the original planned slot. The second\ncontinuation carries an execution-attempt identifier; the first is linked by its\noriginal episode ID and recovered reservation ID.')
    text=text.replace('excludes the recovered task\'s whole ABCD block.',
                      'excludes both recovered tasks\' whole ABCD blocks.')
    text+=MARKER+'''

Operational success was A 446/500 (89.2%), B 437/500 (87.4%), C 436/500
(87.2%), and D 429/500 (85.8%). Verified business-state accuracy was A 95.2%,
B 94.6%, C 93.4%, and D 91.8%. Expected final-status accuracy was A 97.2%,
B 96.0%, C 96.0%, and D 94.0%. Final-status accuracy compares the report to
the task's expected outcome; it does not independently judge every sentence.

The preregistered primary C-B operational effect was -0.2 percentage points
(95% family bootstrap interval -4.0 to +3.6 points; exact family sign-flip
p=1.0). Among 500 matched tasks, C alone succeeded 41 times and B alone
succeeded 42 times. Excluding both infrastructure-recovered task blocks gives
0.0 points over 498 tasks (interval approximately -3.82 to +3.81 points;
p=1.0). These intervals do not establish equivalence or rule out smaller effects.

C-B final-state accuracy was -1.2 points (95% interval -3.6 to +1.2); final-status
accuracy was 0.0 points (interval -1.8 to +1.8). Operational C-B by category:
execution -1.76 points (170 tasks), recovery +3.03 points (165), stopping -1.82
points (165). Category comparisons are exploratory and their intervals are wide.

Across matched tasks C averaged 0.746 fewer tool calls than B (95% cluster
interval -0.990 to -0.500), 1.55 seconds lower active episode latency (interval
-2.25 to -0.85), and approximately $0.0000358 lower known cost (498 pairs;
interval -$0.0000516 to -$0.0000202). Cost and token totals for the two recovered
episodes remain unknown because their failed requests are unverified. The observed
activity, latency and cost differences are secondary and do not establish a
correctness improvement. C averaged 0.424 unnecessary calls versus B 0.460;
constraint violations averaged 0.218 versus 0.206 per episode. Incorrect retries
averaged 0.024 versus 0.018; duplicate operations 0.064 versus 0.054. Event rates
may exceed episode-level rates, and different valid action orders are permitted.

Several secondary Holm-adjusted Monte Carlo p-values are 0.0431, derived from
the minimum 1/10001 raw p-value. This is close to the resolution limit of 10,000
draws, so whether those secondary effects pass a 0.05 threshold is sensitive to
Monte Carlo error; treat them as exploratory rather than robust significance.
The exact primary test does not have that Monte Carlo limitation. Latency can
include API and local execution variation; recovered episode latency excludes
the interruption between attempts.

The 500 dependency graphs are non-isomorphic but procedurally generated from a
shared interpreter and grammar. Cluster inference assumes independent structural
profile families and sign symmetry; the broad four-mechanism sensitivity is a
limited check, not proof of independent real-world generalization. Model aliases,
cache effects and temporal ordering may add dependence. State success of 91.8%
to 95.2% shows ceiling pressure, although observed failures leave variation.
The frozen N=500 had about 49% simulated power for a five-point effect under the
planning assumptions, so the study is underpowered for small differences.
One model and one repetition limit broader generalization.

All 2,000 planned slots are represented once, with 500 complete blocks. Two
provider-interrupted attempts are retained in the audit; both were continued
from verified local state and have valid endpoint outcomes. The original and
first recovery directories remain unchanged. Reported usage costs total
$1.432849965 for this study. The two study-specific unresolved requests add
$0.04 of encumbered exposure, not verified charges; historical holds add
$0.068950. Study exposure is $1.472849965, leaving $2.527150035 under its $4
limit. Account-wide credit balance and final provider invoices were not verified.
Known acknowledged usage is 41,474,602 input tokens (35,783,694 cached),
792,775 output tokens including 238,740 reasoning tokens. Do not add reasoning
tokens again to calculate total usage or cost.

Reproduce the analysis and this addendum, offline:

```powershell
competitive_prompting/.venv/Scripts/python.exe -m competitive_prompting.phase3.luna_study_v1.recovery_v2.analysis
competitive_prompting/.venv/Scripts/python.exe competitive_prompting/phase3/luna_study_v1/recovery_v2/reporting_v1/finalize.py
```
'''
    report.write_text(text,encoding='utf-8')


if __name__=='__main__':main()
