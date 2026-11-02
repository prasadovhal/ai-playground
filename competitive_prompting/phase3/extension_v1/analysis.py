"""Prespecified extension-only family-clustered analysis, with separate replication."""
from collections import defaultdict, Counter
import csv
from decimal import Decimal
import json
from pathlib import Path
import random
from statistics import mean
from types import FunctionType

from ... import analysis as original
from ..balanced.analysis import derive as original_derive
from ..results.balanced_v1.final_analysis.statistics_helpers import mcnemar_exact, signflip, holm, effect
from ..results.balanced_v1.final_analysis.recovery_v1.accounting import replay_ledger, money
from .tasks import BY_ID
from .plan import HERE, LEDGER, SEED, load
from .runner import all_rows, block_coverage, audit_rows, check_reservations, ExtensionBudget

derive=FunctionType(original_derive.__code__,{**original_derive.__globals__,'BY_ID':BY_ID},argdefs=original_derive.__defaults__)
CONTRASTS={'CB':{'C':1,'B':-1},'DC':{'D':1,'C':-1},'BA':{'B':1,'A':-1},
    'CA':{'C':1,'A':-1},'DA':{'D':1,'A':-1},'combined_rivalry_B':{'C':.5,'D':.5,'B':-1}}
METRICS=('operational_task_success','verified_final_state_correct','final_report_status_correct',
    'tool_calls','unnecessary_tool_calls','constraint_violations','incorrect_retries','duplicate_operations',
    'correct_stopping','false_completion_claim','input_tokens','output_tokens','reasoning_tokens',
    'total_tokens','estimated_api_cost_usd','latency_seconds','premature_stop')
BINARY={'operational_task_success','verified_final_state_correct','final_report_status_correct','correct_stopping','false_completion_claim','premature_stop'}


class AuditLedger:
    """Read-only ledger view: can report pending exposure, never reserve/spend."""
    def __init__(self):
        self.entries=[json.loads(s) for s in LEDGER.read_text(encoding='utf-8').splitlines()]
        self.states=replay_ledger(self.entries)

    def exposure(self):
        return sum((money(s['cost'] if s['state']=='settled' else s['allowance']) for s in self.states.values()),Decimal(0))

    def phase_exposure(self):
        return sum((money(s['cost'] if s['state']=='settled' else s['allowance']) for s in self.states.values()
                    if s['episode_id'].startswith('phase3:')),Decimal(0))

    def encumbered_exposure(self):
        return sum((money(s['allowance']) for s in self.states.values() if s['state']=='encumbered_unverified'),Decimal(0))


def interval(pairs, samples=5000):
    """Resample whole families within category; preserve paired task observations."""
    strata=defaultdict(lambda:defaultdict(list))
    for task,family,category,value in pairs: strata[category][family].append(float(value))
    if sum(len(v) for v in strata.values())<8: return None,None
    rng=random.Random(SEED);draws=[]
    for _ in range(samples):
        draw=[]
        for families in strata.values():
            choices=list(families)
            for f in rng.choices(choices,k=len(choices)): draw.extend(families[f])
        draws.append(mean(draw))
    draws.sort()
    return draws[int(samples*.025)],draws[int(samples*.975)]


def paired(rows, coverage):
    complete={b['block_id'] for b in coverage if b['complete']}
    blocks=defaultdict(dict)
    for row in rows:
        if row['block_id'] in complete: blocks[row['block_id']][row['condition']]=row
    groups=defaultdict(list)
    for cond in blocks.values():
        first=next(iter(cond.values()))
        for name,weights in CONTRASTS.items():
            for metric in METRICS:
                if any(cond[c].get(metric) is None for c in weights): continue
                delta=sum(cond[c][metric]*w for c,w in weights.items())
                baseline=sum(cond[c][metric]*-w for c,w in weights.items() if w<0)
                wins=losses=None
                if metric in BINARY and len(weights)==2:
                    a=cond[next(c for c,w in weights.items() if w>0)][metric]
                    b=cond[next(c for c,w in weights.items() if w<0)][metric]
                    wins=int(a and not b);losses=int(b and not a)
                for cat in (first['category'],'all'):
                    groups[(first['model'],cat,name,metric)].append((first['task_id'],first['family'],first['category'],delta,baseline,wins,losses))
    result=[]
    for (model,cat,contrast,metric),values in sorted(groups.items()):
        pairs=[v[:4] for v in values];deltas=[v[3] for v in values]
        clusters=defaultdict(list)
        for v in values: clusters[v[1]].append(v[3])
        p,method,error=signflip([sum(v) for v in clusters.values()],SEED)
        low,high=interval(pairs)
        delta,relative,standardized=effect(deltas,mean(v[4] for v in values))
        wins=sum(v[5] for v in values) if values[0][5] is not None else None
        losses=sum(v[6] for v in values) if values[0][6] is not None else None
        result.append({'model':model,'category':cat,'comparison':contrast,'metric':metric,
            'paired_tasks':len(values),'families':len(clusters),'mean_difference':delta,
            'ci_low':low,'ci_high':high,'relative_difference':relative,'standardized_paired_effect':standardized,
            'positive_only_successes':wins,'reference_only_successes':losses,
            'task_mcnemar_diagnostic_p':mcnemar_exact(wins,losses) if wins is not None else None,
            'family_cluster_p':p,'test':method,'mc_standard_error':error,
            'interval':'5000-draw category-stratified whole-family bootstrap; task-weighted; >=8 families only'})
    primary={i:r['family_cluster_p'] for i,r in enumerate(result)
             if r['category']=='all' and r['comparison']=='CB' and r['metric']=='operational_task_success'}
    secondary={i:r['family_cluster_p'] for i,r in enumerate(result) if i not in primary}
    adjusted={**holm(primary),**holm(secondary)}
    for i,r in enumerate(result):
        r['holm_p']=adjusted[i];r['multiplicity_family']='two_model_primary_CB' if i in primary else 'exploratory_secondary'
    return result


def enrich(row):
    row=derive(row)
    row['premature_stop']=bool(row['termination_reason']=='completed' and any(
        r['authorized'] and r['available'] and r['requirements_met'] and any(n==0 for n in r['counts'].values())
        for r in row['final_state'].values()))
    return row


def csv_write(path, rows):
    if not rows:
        Path(path).write_text('',encoding='utf-8');return
    keys=list(dict.fromkeys(k for r in rows for k in r))
    with Path(path).open('w',newline='',encoding='utf-8') as f:
        writer=csv.DictWriter(f,keys);writer.writeheader()
        for r in rows: writer.writerow({k:json.dumps(v) if isinstance(v,(dict,list)) else v for k,v in r.items()})


def analyze():
    m=load();raw=all_rows();budget=AuditLedger()
    audit_rows(raw,m,budget);check_reservations(raw,budget)
    coverage=block_coverage(raw,m)
    rows=[enrich(r) for r in raw]
    out=HERE/'analysis';out.mkdir(exist_ok=True)
    outcome_fields=('episode_id','run_id','task_id','category','family','difficulty','execution_batch','block_id','model','condition',
        'repetition','seed','timestamp_utc','termination_reason','verified_task_success','completed_episode_task_success',
        'recovery_accuracy','stopping_accuracy','valid_final_report','model_calls','failed_tool_calls','retries',
        'cached_input_tokens','cache_write_tokens','known_api_cost_usd','api_errors','extension_manifest_sha256')+METRICS
    csv_write(out/'final_results.csv',[{k:r.get(k) for k in dict.fromkeys(outcome_fields)} for r in rows])
    summary=original.summarize(rows,SEED)
    for s in summary:
        group=[r for r in rows if r['model']==s['model'] and r['condition']==s['condition'] and s['category'] in (r['category'],'all') and original.eligible(r)]
        for metric in METRICS:
            vv=[r[metric] for r in group if r.get(metric) is not None]
            s['mean_'+metric]=mean(vv) if vv else None
        s['families']=len({r['family'] for r in group})
        pairs=[(r['task_id'],r['family'],r['category'],int(r['operational_task_success'])) for r in group]
        s['family_cluster_success_ci_low'],s['family_cluster_success_ci_high']=interval(pairs)
        # The inherited task-only interval is diagnostic, not cluster-valid evidence.
        s['task_only_interval_diagnostic']=True
    csv_write(out/'condition_summary.csv',summary)
    comparisons=paired(rows,coverage);csv_write(out/'paired_comparisons.csv',comparisons)
    costs=[]
    for scope in ('all','batch_model','model_condition'):
        groups=defaultdict(list)
        for r in rows:
            key=('all','all','all') if scope=='all' else ((r['execution_batch'],r['model'],'all') if scope=='batch_model' else ('all',r['model'],r['condition']))
            groups[key].append(r)
        for (batch,model,condition),group in groups.items():
            c={'scope':scope,'batch':batch,'model':model,'condition':condition,'episodes':len(group),
                'known_cost_usd':str(sum((money(r['known_api_cost_usd']) for r in group),Decimal(0))),
                'cost_complete':all(r['estimated_api_cost_usd'] is not None for r in group),
                'latency_seconds_mean':mean(r['latency_seconds'] for r in group)}
            for metric in ('input_tokens','output_tokens','cached_input_tokens','cache_write_tokens','reasoning_tokens','total_tokens'):
                c[metric]=sum(r[metric] for r in group) if all(r.get(metric) is not None for r in group) else None
            costs.append(c)
    csv_write(out/'cost_analysis.csv',costs)
    original_csv=HERE.parent/'results/balanced_v1/final_analysis/final_results.csv'
    with original_csv.open(encoding='utf-8',newline='') as f: old=list(csv.DictReader(f))
    replication=[]
    old_paired=HERE.parent/'results/balanced_v1/final_analysis/paired_comparisons.csv'
    with old_paired.open(encoding='utf-8',newline='') as f: prior=list(csv.DictReader(f))
    for r in comparisons:
        if r['category']=='all' and r['comparison']=='CB' and r['metric'] in ('operational_task_success','verified_final_state_correct','final_report_status_correct'):
            oldrow=next((v for v in prior if v['model']==r['model'] and v['category']=='all' and v['comparison']=='CB' and v['metric']==r['metric']),{})
            replication.append({'dataset':'extension_v1',**r,'original_result':oldrow})
    csv_write(out/'original_vs_extension.csv',replication)
    status_path=HERE/'results/live/execution_status.json'
    execution=json.loads(status_path.read_text()) if status_path.exists() else {'status':'NOT_RUN'}
    states=replay_ledger(budget.entries)
    known=sum((money(s['cost']) for s in states.values() if s['state']=='settled' and s['episode_id'].startswith('phase3:extension_v1:')),Decimal(0))
    audit={'status':execution['status'],'episodes':len(rows),'planned':480,'complete_blocks':sum(b['complete'] for b in coverage),
        'planned_blocks':120,'completed_batches':sum(all(b['complete'] for b in coverage if b['batch']==n) for n in range(1,11)),
        'missing_episodes':480-len(rows),'known_extension_cost_usd':str(known),'remaining_phase3_usd':str(Decimal(6)-budget.phase_exposure()),
        'remaining_experiment_usd':str(Decimal(9)-budget.exposure()),'encumbered_unverified_usd':str(budget.encumbered_exposure()),
        'manifest_sha256':m['sha256'],'protected_original_files':len(m['protected_files']),'stop':execution.get('stop'),
        'blocking_reservations':[rid for rid,s in states.items() if s['state']=='pending'],
        'operational_successes':sum(bool(r['operational_task_success']) for r in rows),
        'operational_denominator':sum(original.eligible(r) for r in rows),'blocks':coverage}
    (out/'execution_audit.json').write_text(json.dumps(audit,indent=2),encoding='utf-8')
    write_report(out,m,rows,summary,comparisons,replication,audit)
    print(json.dumps({k:v for k,v in audit.items() if k!='blocks'}),flush=True)
    return audit


def write_report(out,m,rows,summary,comparisons,replication,audit):
    def table(header, data):
        return '| '+' | '.join(header)+' |\n|'+'|'.join('---' for _ in header)+'|\n'+''.join('| '+' | '.join(str(v) for v in r)+' |\n' for r in data)
    primary=[r for r in comparisons if r['category']=='all' and r['comparison']=='CB' and r['metric']=='operational_task_success']
    text=f'''# Competitive prompting extension v1

Status: **{audit['status']}**. Observed {len(rows)}/480 episodes, {audit['complete_blocks']}/120 complete four-condition blocks; {audit['missing_episodes']} unattempted/missing episodes. Extension results are separate from the immutable original experiment.

## Method and predeclaration

Sixty authored deterministic cases: twenty/category, thirty scenario families, two cases/family. Both original models, all four unchanged treatments, low reasoning, original 24-tool/24-model-call and 2048-output-token limits. One repetition; fresh state/conversation every episode. Primary C minus B. Manifest SHA256 `{m['sha256']}`; seed {SEED}. Ten batches, two distinct families/category/batch; immutable block and condition order frozen before extension calls. Stop/admission depends only on budget or infrastructure, never outcomes.

Gold counts are explicit authored data, independently replayed using the original reference checker; no LLM judge. Original scoring/operational denominator retained. Final state, report status and fresh verification are separate diagnostics. Completed incorrect reports, output truncation and agent limits fail. Provider failures are infrastructure; unattempted budget-censored cases are not failures. All attempted episodes remain in the audit; only complete infrastructure-valid ABCD blocks enter paired tests.

## Difficulty and novelty

New business domains combine heterogeneous operation chains, incomplete prerequisite reconciliation, completed downstream operations, independent receipts/attestations, exact-once writes, varied read/validation faults and before/after-commit ambiguity. Stopping includes achieved goals, immutable data and blocked goals with useful permitted work. Domain family and difficulty labels were fixed before extension results. Name-invariant record/dependency/fault fingerprints are distinct from all original cases and from each other. This is a structural novelty check, not evidence of a new simulator mechanism.

Reconciliation with non-prefix completion represents previously recorded downstream work requiring missing audit/prerequisite backfill; tools prohibit repeating it. This changes the difficulty distribution relative to original prefix-completed uniform chains. Oracle traces use only observations and fit frozen limits. Some stopping cases are deliberately easy. The original simulator cannot represent cross-record dependencies, true delayed asynchronous service execution, multi-operation atomicity or within-operation partial writes. Faults model partial workflow completion through unchanged before/after commits. These limitations restrict the claim of generalization; domain families are not proven independent task generators.

## Outcomes by model and condition

'''
    text+=table(['Model','Condition','Successes / evaluable','State correct','Status correct','Mean tools','Mean unnecessary','Violations / episode'],[
        [s['model'],s['condition'],f"{s['operational_successes']}/{s['episodes_evaluable']}",s.get('mean_verified_final_state_correct'),s.get('mean_final_report_status_correct'),s.get('mean_tool_calls'),s.get('mean_unnecessary_tool_calls'),s.get('mean_constraint_violations')] for s in summary if s['category']=='all'])
    text+='\n## Primary CB analysis\n\n'
    text+=table(['Model','Paired tasks','Families','Difference','95% cluster CI','Cluster p','Holm p','C-only / B-only'],[
        [r['model'],r['paired_tasks'],r['families'],r['mean_difference'],f"[{r['ci_low']}, {r['ci_high']}]",r['family_cluster_p'],r['holm_p'],f"{r['positive_only_successes']} / {r['reference_only_successes']}"] for r in primary])
    for r in primary:
        text+=f'\n{r["model"]}: '+('The operational difference is not statistically distinguishable from zero after the primary correction; these data do not establish a benefit or equivalence.' if r['holm_p']>=.05 else 'The operational difference is statistically distinguishable from zero under the prespecified family-cluster test. Its direction, magnitude and physical-state decomposition determine practical value; shared-engine dependence remains a limitation.')+'\n'
    text+='\nExploratory category-specific primary effects:\n\n'
    text+=table(['Model','Category','Tasks','Families','CB operational difference','95% family CI'],[
        [r['model'],r['category'],r['paired_tasks'],r['families'],r['mean_difference'],f"[{r['ci_low']}, {r['ci_high']}]" ]
        for r in comparisons if r['category']!='all' and r['comparison']=='CB' and r['metric']=='operational_task_success'])
    text+='''
Whole families are resampled within category (5000 bootstrap draws). Cluster sign-flip tests flip all paired task differences within a family together. Primary Holm adjustment covers two model operational CB tests; other contrasts/metrics form a separate exploratory family. Task-level McNemar is diagnostic only because siblings share design. Intervals are omitted below eight observed families. Shared simulator assumptions can induce broader dependence than family labels capture. Degenerate intervals at a ceiling do not prove equivalence. Multiplicity correction, finite tasks and one repetition limit precision.

## Correctness, activity and premature stopping

'''
    relevant=[r for r in comparisons if r['category']=='all' and r['comparison']=='CB' and r['metric'] in ('verified_final_state_correct','final_report_status_correct','tool_calls','unnecessary_tool_calls','constraint_violations','incorrect_retries','duplicate_operations','premature_stop','estimated_api_cost_usd','latency_seconds')]
    text+=table(['Model','Metric','C-B','95% family CI','Exploratory Holm p'],[[r['model'],r['metric'],r['mean_difference'],f"[{r['ci_low']}, {r['ci_high']}]",r['holm_p']] for r in relevant])
    failures=[r for r in rows if original.eligible(r) and not r['operational_task_success']]
    text+=f'\nOperational failures: {len(failures)}. Terminations: `{dict(Counter(r["termination_reason"] for r in rows))}`.\n\n'
    lookup={(r['task_id'],r['model'],r['condition']):r for r in rows}
    reductions=[]
    for r in rows:
        if r['condition']!='C':continue
        b=lookup.get((r['task_id'],r['model'],'B'))
        if b and r['tool_calls']<b['tool_calls']:
            reductions.append((r,b))
    text+=f'Of {len(reductions)} observed CB pairs with fewer C tools, {sum(r["premature_stop"] for r,b in reductions)} C episodes stopped with authorized pending work, and {sum(bool(b["verified_final_state_correct"]) and not r["verified_final_state_correct"] for r,b in reductions)} lost final-state correctness versus B. Fewer calls alone are not an efficiency gain.\n\n'
    # Concrete, reproducible trace evidence; do not classify valid reordering as error.
    for r in failures[:8]:
        violations=[(i+1,t['tool'],t['arguments']) for i,t in enumerate(r['action_trace']) if t['violation']]
        pending={rid:[op for op,n in s['counts'].items() if n==0] for rid,s in r['final_state'].items() if s['authorized'] and s['available'] and s['requirements_met'] and any(n==0 for n in s['counts'].values())}
        text+=f'- `{r["episode_id"]}`: state={r["verified_final_state_correct"]}, status={r["final_report_status_correct"]}, termination={r["termination_reason"]}; pending authorized operations `{pending}`; violating trace steps `{violations}`. Final report: `{r["final_text"]}`.\n'
    for model in m['models']:
        observed=[r for r in rows if r['model']==model and original.eligible(r)]
        successes=sum(bool(r['operational_task_success']) for r in observed)
        text+=f'\n{model}: {successes}/{len(observed)} operational successes. '+('A full observed operational ceiling persists; treatment sensitivity is limited.' if observed and successes==len(observed) else 'No full operational ceiling; physical-state/report differences must still be distinguished.')+'\n'
    text+='\n## Original versus extension\n\nOriginal results and conclusions are unchanged. No pooled estimate is presented because difficulty distributions and family definitions differ.\n\n'
    text+=table(['Model','Metric','Original CB','Original 95% interval','Extension CB','Extension 95% family interval'],[
        [r['model'],r['metric'],r['original_result'].get('mean_task_paired_difference'),
            f"[{r['original_result'].get('ci_low')}, {r['original_result'].get('ci_high')}]",r['mean_difference'],
            f"[{r['ci_low']}, {r['ci_high']}]" ] for r in replication])
    text+='\nA matching sign alone is not replication. Assess uncertainty, state/report decomposition and independent family coverage; a null result is valid and is not equivalence. Secondary comparisons DC, BA, CA and DA plus combined rivalry remain exploratory in paired_comparisons.csv. Category-specific effects and counts are retained there and in condition_summary.csv.\n'
    text+=f'''
## Spending and completeness

Known extension usage-priced spending: ${audit['known_extension_cost_usd']}. Remaining Phase 3 allowance: ${audit['remaining_phase3_usd']}; experiment: ${audit['remaining_experiment_usd']}. Unverified encumbrances remain ${audit['encumbered_unverified_usd']}, counted once and not settled charges. Pricing is frozen; reported input/output usage determines cost. Reasoning is part of output tokens, never billed twice. Every request reserves before sending; automatic API retries are disabled. No off-ledger balance or recharge is relied on.

Stop details: `{json.dumps(audit['stop'])}`. Incomplete blocks: `{[b['block_id'] for b in audit['blocks'] if b['recorded'] and not b['complete']]}`. All missing identities and exclusions are in execution_audit.json. If budget stops execution, coverage reflects the fixed order, not outcome selection; incomplete category/model coverage limits inference. Conservative forecasts are estimates, not exact future charges.

## Interpretation

The primary estimates and family-cluster uncertainty above determine whether the extension supports improved operational success. Physical-state and status metrics distinguish execution improvement from reporting effects; activity, latency and cost cannot substitute for correctness. Benefits require reproducible correctness gains with acceptable constraint/recovery/stopping behavior. Treat secondary associations as exploratory and avoid causal claims about mediation by tool count. This finite synthetic benchmark, purposive domains, shared engine and one repetition cannot establish general equivalence or broad production generalization. The extension was not tuned for significance or based on live treatment results.

## Reproduction

From the repository root (analysis makes no API calls):

```powershell
competitive_prompting/.venv/Scripts/python.exe -m competitive_prompting.phase3.extension_v1.analysis
competitive_prompting/.venv/Scripts/python.exe -m pytest competitive_prompting -q
```

Safe continuation, only if original caps admit the next complete block:

```powershell
competitive_prompting/.venv/Scripts/python.exe -m competitive_prompting.phase3.extension_v1.runner --execute
```

Completed episodes are audited and skipped, never rerun. New unknown reservations block execution. No caps or protected original files were changed. No commit, push or PR.
'''
    (out/'final_experiment_report.md').write_text(text,encoding='utf-8')
    comparison='\n'.join(text.split('## Original versus extension')[1].split('## Spending')[0:1])
    (out/'original_vs_extension_report.md').write_text('# Original versus extension\n'+comparison,encoding='utf-8')
    validation=f'''# Validation report

Status: {audit['status']}. {len(rows)} unique recorded episodes, {audit['complete_blocks']} complete ABCD blocks. Planned 480 episodes/120 blocks. Frozen source and manifest hashes match; {audit['protected_original_files']} original files unchanged and original ledger prefix preserved. All recorded request reservations match unique canonical episode interactions. All infrastructure-complete episodes pass the unchanged request/trace/usage/cost/state replay validator and independent scoring. Agent failures remain failures. Incomplete blocks excluded from paired analyses.

Sixty oracle traces and all ten scripted batches passed before paid execution; family-level negative, reset, fault and leakage tests are in tests/test_extension.py. Full-suite output: ../pytest.txt. Post-execution output: ../postflight_pytest.txt.

Manifest SHA256: {m['sha256']}. Missing identities and infrastructure exclusions: execution_audit.json. Ledger is append-only; authorized encumbrances remain fully counted, future unknown requests are not automatically encumbered. API project identity is hash-checked; external account balance/recharge is not inspected or relied on.
'''
    (out/'validation_report.md').write_text(validation,encoding='utf-8')


if __name__=='__main__': analyze()
