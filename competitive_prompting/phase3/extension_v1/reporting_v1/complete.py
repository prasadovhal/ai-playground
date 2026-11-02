"""Repair historical contrast-label lookup, add auditable trace interpretation.

No new inferential method, scoring change or paid client. Kept outside frozen
source files because the report integration bug was discovered after execution.
"""
import csv
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
from pathlib import Path
from statistics import mean
import shutil

from ..analysis import enrich, write_report, csv_write
from ..runner import all_rows
from ..plan import HERE, load


def read_csv(path):
    def value(s):
        if s=='': return None
        if s in ('True','False'): return s=='True'
        try: return json.loads(s)
        except (ValueError,TypeError): return s
    with Path(path).open(encoding='utf-8',newline='') as f:
        return [{k:value(v) for k,v in r.items()} for r in csv.DictReader(f)]


def historical_match(original, model, metric):
    found=[r for r in original if r['model']==model and r['category']=='all' and
           r['metric']==metric and r['comparison']=='primary_C_minus_B']
    if len(found)!=1: raise ValueError('Historical primary contrast missing/duplicated')
    return found[0]


def main():
    m=load();out=HERE/'analysis'
    primary_path=out/'paired_comparisons.csv'
    primary_sha=hashlib.sha256(primary_path.read_bytes()).hexdigest()
    comparisons=read_csv(primary_path);summary=read_csv(out/'condition_summary.csv')
    audit=json.loads((out/'execution_audit.json').read_text())
    rows=[enrich(r) for r in all_rows()]
    old_path=HERE.parent/'results/balanced_v1/final_analysis/paired_comparisons.csv'
    with old_path.open(encoding='utf-8',newline='') as f: old=list(csv.DictReader(f))
    archive=out/'before_reporting_adapter_v1';archive.mkdir(exist_ok=True)
    for name in ('final_experiment_report.md','original_vs_extension_report.md','original_vs_extension.csv'):
        if not (archive/name).exists(): shutil.copyfile(out/name,archive/name)
    replication=[]
    for r in comparisons:
        if r['category']=='all' and r['comparison']=='CB' and r['metric'] in (
                'operational_task_success','verified_final_state_correct','final_report_status_correct'):
            replication.append({'dataset':'extension_v1',**r,'original_result':historical_match(old,r['model'],r['metric'])})
    csv_write(out/'original_vs_extension.csv',replication)
    write_report(out,m,rows,summary,comparisons,replication,audit)
    traces=[]
    for r in rows:
        verifications=[(i+1,t) for i,t in enumerate(r['action_trace']) if t['tool']=='verify_completion']
        successful=[(i,t) for i,t in verifications if t['response'].get('ok')]
        last=successful[-1][1] if successful else None
        pending={rid:[op for op,n in s['counts'].items() if n==0] for rid,s in r['final_state'].items()
            if s['authorized'] and s['available'] and s['requirements_met'] and any(n==0 for n in s['counts'].values())}
        traces.append({'episode_id':r['episode_id'],'task_id':r['task_id'],'model':r['model'],'condition':r['condition'],
            'family':r['family'],'oracle_tool_calls':len(m['tasks'][r['task_id']]['oracle_actions']),
            'actual_tool_calls':r['tool_calls'],'verified_final_state_correct':r['verified_final_state_correct'],
            'final_report_status_correct':r['final_report_status_correct'],'operational_success':r['operational_task_success'],
            'verification_current':r['verification_current'],'pending_authorized_operations':pending,
            'verification_attempts':len(verifications),'successful_verifications':len(successful),
            'last_complete':last['response'].get('complete') if last else None,
            'last_blockers':last['response'].get('blockers') if last else None,
            'uninspected_write_retries':r['uninspected_write_retries'],'incorrect_retries':r['incorrect_retries'],
            'duplicate_operations':r['duplicate_operations'],
            'violating_steps':[(i+1,t['arguments'],t['response']) for i,t in enumerate(r['action_trace']) if t['violation']],
            'final_text':r['final_text']})
    csv_write(out/'trace_audit.csv',traces)
    lookup={(r['model'],r['task_id'],r['condition']):r for r in rows}
    discordant=[];reduced=[]
    for c in rows:
        if c['condition']!='C':continue
        b=lookup[(c['model'],c['task_id'],'B')]
        if c['operational_task_success']!=b['operational_task_success']:
            discordant.append({'task_id':c['task_id'],'model':c['model'],'winner':'C' if c['operational_task_success'] else 'B',
                'C_state':c['verified_final_state_correct'],'B_state':b['verified_final_state_correct'],
                'C_status':c['final_report_status_correct'],'B_status':b['final_report_status_correct'],
                'C_violations':c['constraint_violations'],'B_violations':b['constraint_violations'],
                'C_verification_current':c['verification_current'],'B_verification_current':b['verification_current']})
        if c['tool_calls']<b['tool_calls']: reduced.append((c,b))
    csv_write(out/'CB_discordant_trace_summary.csv',discordant)
    budget=json.loads((HERE/'continuation_budget_request.json').read_text())
    costs={model:sum((Decimal(str(r['known_api_cost_usd'])) for r in rows if r['model']==model),Decimal(0)) for model in m['models']}
    old_manifest=json.loads((HERE.parent/'manifest_heldout_one_rep_hard-v1.json').read_text())
    old_actions=mean(len(v['oracle_actions']) for v in old_manifest['tasks'].values())
    new_actions=mean(len(v['oracle_actions']) for v in m['tasks'].values())
    text=f'''
## Audited interpretation and continuation decision

This is a **partial-budget extension**, not a completed 480-episode evaluation. Eight full batches and eleven blocks of Batch 9 were executed: 428 unique episodes, 107 complete ABCD blocks, 54 tasks attempted, 53 evaluated on both models. All thirty families are represented in each model. Batch 9's final Sol/acoustic-sensor block and all twelve Batch 10 blocks remain unattempted (52 episodes). There are no incomplete ABCD blocks or paid infrastructure-failed episodes. All 31 incorrect agent episodes remain operational failures; no failures were removed to obtain the comparison.

Luna's primary CB operational difference is +5.56 percentage points (47/54 versus 44/54; 95% family-cluster interval −3.70 to +15.09 points; exact cluster p=0.453125, primary Holm p=0.90625). Five task pairs favor C and two favor B. Both conditions have the SAME physical-state outcome on every paired task, including 51 correct states out of 54. The seven operational discordances therefore do not demonstrate improved final environment correctness: four C-only successes concern final status/verification handling, one concerns avoiding a rejected prerequisite violation; two B-only successes concern C's incorrect blocked-status reports. See CB_discordant_trace_summary.csv and raw episode traces. Sol is 212/212 successful overall (53/53 in each condition), reproducing the original full ceiling and leaving no observed correctness headroom. Zero observed differences and degenerate bootstrap intervals do not establish equivalence.

Concrete trace evidence:

- `ext1_execution_seed_bank_1`, Luna/B: tool 4 attempts register before the missing weigh prerequisite; the tool rejects it. Subsequent reconciliation reaches the correct state, but the attempted constraint violation makes operational success false. C avoids that attempt.
- `ext1_stopping_cryptographic_retirement_1`, Luna/B: the goal was already complete, but the final status is blocked because of a completed locked record. C reports completed correctly.
- `ext1_recovery_scientific_registry_2` and `ext1_stopping_destructive_media_2`, Luna/C: correct environment state but incorrect blocked final status; B succeeds. These are regressions in reporting, not evidence that C always helps.
- `ext1_stopping_aviation_airworthiness_1`: B and C both repeat an already completed component_record operation (count becomes 2). `ext1_stopping_court_sealed_material_2`: B and C both duplicate redact after backfilling digitize. These exact-once errors are real decisions under the unchanged scorer, not infrastructure failures.

The unchanged verifier lists authorization restrictions in blockers even for already-completed locked records. A complete=true response can thus include a nonempty blockers list. Several wrong blocked reports reflect this ambiguity. Report-status differences measure handling of the frozen interface, not solely general workflow ability; the tool/scorer were deliberately not changed. Non-prefix backfill is explicit in the task instructions, but its interpretation as audit reconciliation also limits production realism.

C used 0.296 fewer tools/episode for Luna (about 3.3%) and 0.019 fewer for Sol. Luna's unadjusted family test for tools is p=0.02377, but the predeclared exploratory Holm p is 1.0. Sol latency is about 1.65 seconds lower under C (unadjusted p≈0.0337, exploratory Holm p=1.0). Cost differences favor C by about $0.0000103/episode for Luna and $0.0001345 for Sol; both family intervals include zero. Thus these data do not support activity inflation from C, nor a reliable cost/latency advantage after the planned correction. Prompt-length/cache differences and service latency remain alternative explanations for resource differences. Fewer calls do not prove better execution.

There are {len(reduced)} CB task/model pairs with fewer C calls; {sum(c['premature_stop'] for c,b in reduced)} of these C episodes end with authorized pending writes, and {sum(not c['verified_final_state_correct'] and b['verified_final_state_correct'] for c,b in reduced)} lose physical-state correctness relative to B. Valid alternative action orders are not scored as errors. The trace audit includes oracle call counts for context, not a requirement to copy oracle ordering or to minimize calls exactly. Across all conditions there are {sum(r['constraint_violations'] for r in rows)} violations, {sum(r['duplicate_operations'] for r in rows)} duplicate operations, {sum(r['incorrect_retries'] for r in rows)} incorrect retry and {sum(r['uninspected_write_retries'] for r in rows)} uninspected write retry. There are zero model/tool-limit hits, output truncations and paid API errors. Abandoning verification after transients is reported separately from the narrower pending-write stopping diagnostic.

The extension adds novel states and domains, but is NOT empirically demonstrated to be harder: mean oracle tool calls are {new_actions:.2f} versus {old_actions:.2f} in the original held-out set. Luna's neutral operational success rises from 13/24 to 46/54 across these different task distributions; Sol's ceiling persists. More examples improve conditional precision, but do not solve sensitivity at a ceiling. The original Luna CB point estimate (+8.33 points) and extension (+5.56) share a direction, while both intervals include zero. This is not a robust replicated benefit. Original named-minus-generic was negative; the extension's small positive operational estimate does not reproduce that direction. These exploratory comparisons use different difficulty distributions and uncertainty methods; no pooled estimate or formal equivalence claim is made. Shared simulator mechanisms can induce dependence beyond the thirty named domain families.

Budget-based censoring follows a frozen order, but realized costs can still make coverage informative; later cases are not a random missing sample. Separate model/task coverage and missing blocks must accompany inference. Continuation may depend only on infrastructure and authorized budget, never these treatment estimates.

Known extension usage-priced spending is $3.441953025: Luna ${costs['gpt-6-luna']}, Sol ${costs['gpt-6.1-sol']}. The coverage-matched nominal forecast was ${Decimal(budget['completed_scope_frozen_nominal_forecast_usd']):.9f}, below actual spending; oracle workload and original-cache calibration were optimistic. Guard admission remained conservative. Total known experiment spending is $5.449365430; unverified holds remain $0.068950 ($0.048950 historical and $0.02 prior timeout), so total exposure is $5.518315430. Neither hold is a confirmed charge or released budget. There are no new pending reservations. Phase 3 headroom is $0.636315870; experiment headroom is $3.481684570. No automatic recharge or account-balance assumption was used.

The next fixed block needs $0.82652586454469308500 of headroom: exact gap $0.19020999454469308500. A separately approved cap-only version raising Phase 3 from $6 to $6.20 would admit that block, but would not guarantee the other 48 episodes. Remaining point cost forecast is approximately $0.46022 ($0.69033 with 50% margin); the frozen conservative remaining-workload-plus-peak allowance is $2.74719656009518982362500. Covering that estimate requires $2.11088069009518982362500 additional Phase 3 allowance, rounded upward to $2.12 (Phase 3 cap $8.12), while retaining the global $9 cap. These are requests/forecasts, not known future charges; **no cap was changed or additional spending approved by this report**. Both encumbrances remain counted. See ../continuation_budget_request.json.

Read-only API model access initially failed inside the network sandbox before any Responses request or reservation; that event is preserved in ../results/live/preflight_connection_error.json. Approved network access then succeeded. A UTF-16 test-log encoding issue was also corrected before requests. Neither caused a charged episode retry. Every acknowledged paid request is recorded once.

The historical-label adapter corrects only the original-comparison table (`primary_C_minus_B` versus extension `CB`). The frozen primary CSV, manifest, task specifications, evaluator, original files and inferential methods are unchanged. The first generated comparison report is archived in before_reporting_adapter_v1. The adapter source/hash is separately recorded in report_adapter_audit.json.

Reproduce complete corrected reporting, without API calls:

```powershell
competitive_prompting/.venv/Scripts/python.exe -m competitive_prompting.phase3.extension_v1.analysis
competitive_prompting/.venv/Scripts/python.exe -m competitive_prompting.phase3.extension_v1.reporting_v1.complete
```

The existing safe continuation command is `competitive_prompting/.venv/Scripts/python.exe -m competitive_prompting.phase3.extension_v1.runner --execute`; under current caps it refuses the next block without new paid calls. Continuing requires separate approval and a versioned budget-only execution update; never silently edit the frozen manifest or rerun completed episodes.
'''
    report=out/'final_experiment_report.md'
    report.write_text(report.read_text(encoding='utf-8')+text,encoding='utf-8')
    (out/'original_vs_extension_report.md').write_text((out/'original_vs_extension_report.md').read_text(encoding='utf-8')+
        '\nThe operational point-estimate direction is shared for Luna, but neither study establishes a benefit; physical-state improvement does not replicate. Sol\'s ceiling persists. Named-minus-generic reverses direction descriptively. See final report for uncertainty, distribution changes and trace decomposition.\n',encoding='utf-8')
    validation=out/'validation_report.md'
    log=(HERE/'postflight_pytest.txt').read_text(encoding='utf-8-sig')
    validation.write_text(validation.read_text(encoding='utf-8')+'\nPost-execution full suite: '+next(s for s in log.splitlines() if ' passed' in s)+
        '\nAll ten scripted batches revalidated after paid execution. The report-label adapter has separate focused tests. Primary paired CSV unchanged by adapter. No cap or frozen source change.\n',encoding='utf-8')
    if hashlib.sha256(primary_path.read_bytes()).hexdigest()!=primary_sha: raise ValueError('Adapter modified primary analysis')
    record={'adapter':'reporting-v1','source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'manifest_sha256':m['sha256'],'paired_csv_unchanged_sha256':primary_sha,
        'correction':'Historical contrast-label mapping only; primary estimates/tests untouched. Added descriptive trace/budget interpretation after execution.',
        'original_rows_found':len(replication),'protected_originals_unchanged':True}
    (out/'report_adapter_audit.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
    print(json.dumps(record))


if __name__=='__main__': main()
