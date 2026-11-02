"""Endpoint-gated, family-aware analysis. No API calls or interim hypothesis tests."""
from collections import defaultdict,Counter
import csv
from decimal import Decimal
import json
import random
from statistics import mean

from ... import analysis as original
from ..extension_v1.analysis import AuditLedger
from ..results.balanced_v1.final_analysis.statistics_helpers import signflip,holm,effect
from ..luna_planning_v1.power import family_signflip
from .plan import HERE,load,SEED
from .runner import rows,items,coverage,validate,complete,CASES
from .budget import replay_ledger,NAMESPACE,STUDY_CAP,PHASE_CAP,GLOBAL_CAP,money

METRICS=('operational_task_success','verified_final_state_correct','final_report_status_correct','correct_stopping',
    'constraint_violations','incorrect_retries','duplicate_operations','uninspected_write_retries','tool_calls',
    'unnecessary_tool_calls','retries','input_tokens','cached_input_tokens','output_tokens','reasoning_tokens',
    'total_tokens','estimated_api_cost_usd','latency_seconds')
BINARY={'operational_task_success','verified_final_state_correct','final_report_status_correct','correct_stopping'}
CONTRASTS={'CB':{'C':1,'B':-1},'DC':{'D':1,'C':-1},'BA':{'B':1,'A':-1},'CA':{'C':1,'A':-1},
    'DA':{'D':1,'A':-1},'combined_rivalry_B':{'C':.5,'D':.5,'B':-1}}


def csv_write(path,data):
    if not data:Path(path).write_text('',encoding='utf-8');return
    keys=list(dict.fromkeys(k for r in data for k in r))
    with Path(path).open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,keys);w.writeheader()
        for r in data:w.writerow({k:json.dumps(v) if isinstance(v,(dict,list)) else v for k,v in r.items()})


def enrich(r):
    r=original.analysis_metrics(r);task=CASES[r['task_id']].task
    try:report=json.loads(r['final_text'])
    except (ValueError,TypeError):report={}
    return {**r,'verified_final_state_correct':r['state_correct'],
        'final_report_status_correct':bool(r['valid_final_report'] and report.get('status')==task.expected_status)}


def interval(values):
    strata=defaultdict(lambda:defaultdict(list))
    for family,category,value in values:strata[category][family].append(float(value))
    if sum(len(g) for g in strata.values())<8:return None,None
    rng=random.Random(SEED);draws=[]
    for _ in range(10000):
        chosen=[]
        for groups in strata.values():
            for f in rng.choices(list(groups),k=len(groups)):chosen.extend(groups[f])
        draws.append(mean(chosen))
    draws.sort();return draws[250],draws[9750]


def paired(data,endpoint):
    bytask=defaultdict(dict)
    for r in data:bytask[r['task_id']][r['condition']]=r
    groups=defaultdict(list)
    for task,cond in bytask.items():
        if set(cond)!=set('ABCD') or any(not complete(r) for r in cond.values()):continue
        first=cond['A']
        for contrast,weights in CONTRASTS.items():
            for metric in METRICS:
                if any(cond[c].get(metric) is None for c in weights):continue
                delta=sum(cond[c][metric]*w for c,w in weights.items())
                baseline=sum(cond[c][metric]*-w for c,w in weights.items() if w<0)
                win=loss=None
                if metric in BINARY and contrast!='combined_rivalry_B':
                    a=cond[next(c for c,w in weights.items() if w>0)][metric]
                    b=cond[next(c for c,w in weights.items() if w<0)][metric]
                    win=int(a and not b);loss=int(b and not a)
                for cat in (first['category'],'all'):
                    groups[(cat,contrast,metric)].append((first['family'],first['category'],delta,baseline,win,loss,first['mechanism']))
    result=[]
    for (category,contrast,metric),v in sorted(groups.items()):
        values=[x[2] for x in v];families=defaultdict(list)
        for x in v:families[x[0]].append(x[2])
        p=None;method='Descriptive only: fixed endpoint not reached';se=None;low=high=None
        broad_p=None
        if endpoint:
            low,high=interval([(x[0],x[1],x[2]) for x in v])
            if metric in BINARY:
                scale=2 if contrast=='combined_rivalry_B' else 1
                p=family_signflip([int(round(sum(x)*scale)) for x in families.values()]);method='Exact whole-profile-family sign flip';se=0
            else:p,method,se=signflip([sum(x) for x in families.values()],SEED)
            if category=='all' and contrast=='CB' and metric=='operational_task_success':
                broad=defaultdict(float)
                for x in v:broad[x[6]]+=x[2]
                broad_p=family_signflip([int(round(x)) for x in broad.values()])
        delta,relative,standard=effect(values,mean(x[3] for x in v))
        result.append({'category':category,'contrast':contrast,'metric':metric,'paired_tasks':len(v),'families':len(families),
            'difference':delta,'relative_difference':relative,'standardized_paired_effect':standard,
            'ci_low':low,'ci_high':high,'p_value':p,'test':method,'mc_standard_error':se,
            'treatment_only_successes':sum(x[4] for x in v) if v[0][4] is not None else None,
            'reference_only_successes':sum(x[5] for x in v) if v[0][5] is not None else None,
            'broad_mechanism_sensitivity_p':broad_p,'endpoint_reached':endpoint})
    if endpoint:
        secondary={i:r['p_value'] for i,r in enumerate(result) if not (r['category']=='all' and r['contrast']=='CB' and r['metric']=='operational_task_success')}
        adjusted=holm(secondary)
        for i,r in enumerate(result):r['adjusted_p']=adjusted[i] if i in adjusted else r['p_value']
    else:
        for r in result:r['adjusted_p']=None
    return result


def analyze():
    m=load();raw=rows();ledger=AuditLedger();planned={x['episode_id']:x for x in items(m)}
    if len({r['episode_id'] for r in raw})!=len(raw):raise ValueError('Duplicate episodes')
    for r in raw:validate(r,planned[r['episode_id']],m,ledger)
    actual={rid for rid,s in ledger.states.items() if s['episode_id'].startswith(NAMESPACE)}
    ids=[i['reservation_id'] for r in raw for i in r['interactions']]
    if len(ids)!=len(set(ids)) or actual!=set(ids):raise ValueError('Reservation/episode audit mismatch')
    cov=coverage(raw,m);endpoint=len(raw)==2000 and all(x['complete'] for x in cov)
    data=[enrich(r) for r in raw]
    out=HERE/'analysis';out.mkdir(exist_ok=True)
    fields=('episode_id','task_id','family','mechanism','category','model','condition','repetition','seed','execution_batch',
        'block_id','timestamp_utc','termination_reason','model_calls','valid_final_report','completed_episode_task_success',
        'known_api_cost_usd','api_errors','final_text','manifest_sha256')+METRICS
    csv_write(out/'final_results.csv',[{k:r.get(k) for k in dict.fromkeys(fields)} for r in data])
    summary=original.summarize(data,SEED)
    for s in summary:
        for k in list(s):
            if 'ci_low' in k or 'ci_high' in k:s[k]=None
        group=[r for r in data if r['condition']==s['condition'] and s['category'] in ('all',r['category']) and original.eligible(r)]
        for metric in METRICS:
            v=[r[metric] for r in group if r.get(metric) is not None]
            s['mean_'+metric]=mean(v) if v else None
        s['family_count']=len({r['family'] for r in group})
    csv_write(out/'condition_summary.csv',summary)
    contrasts=paired(data,endpoint);csv_write(out/'paired_comparisons.csv',contrasts)
    costs=[]
    for key in ['condition','execution_batch']:
        groups=defaultdict(list)
        for r in data:groups[r[key]].append(r)
        for value,group in groups.items():
            costs.append({'scope':key,'value':value,'episodes':len(group),
                'known_spend_usd':str(sum((money(r['known_api_cost_usd']) for r in group),Decimal(0))),
                'cost_complete':all(r['estimated_api_cost_usd'] is not None for r in group),
                'input_tokens':sum(r['input_tokens'] for r in group) if all(r['input_tokens'] is not None for r in group) else None,
                'cached_input_tokens':sum(r['cached_input_tokens'] for r in group) if all(r['cached_input_tokens'] is not None for r in group) else None,
                'output_tokens':sum(r['output_tokens'] for r in group) if all(r['output_tokens'] is not None for r in group) else None,
                'reasoning_tokens':sum(r['reasoning_tokens'] for r in group) if all(r['reasoning_tokens'] is not None for r in group) else None,
                'mean_latency_seconds':mean(r['latency_seconds'] for r in group)})
    csv_write(out/'cost_analysis.csv',costs)
    execution=json.loads((HERE/'results/live/execution_status.json').read_text())
    study_states={k:s for k,s in ledger.states.items() if s['episode_id'].startswith(NAMESPACE)}
    exposure=sum((money(s['cost'] if s['state']=='settled' else s['allowance']) for s in study_states.values()),Decimal(0))
    known=sum((money(s['cost']) for s in study_states.values() if s['state']=='settled'),Decimal(0))
    audit={'status':execution['status'],'episodes':len(data),'planned':2000,'complete_blocks':sum(x['complete'] for x in cov),
        'planned_blocks':500,'missing':2000-len(data),'endpoint_reached':endpoint,'known_new_cost_usd':str(known),
        'new_study_exposure_usd':str(exposure),'remaining_study_exposure_usd':str(STUDY_CAP-exposure),
        'remaining_phase3_usd':str(PHASE_CAP-ledger.phase_exposure()),'remaining_global_usd':str(GLOBAL_CAP-ledger.exposure()),
        'unverified_encumbrances_usd':str(ledger.encumbered_exposure()),'pending_reservations':[k for k,s in ledger.states.items() if s['state']=='pending'],
        'protected_files':len(m['protected_files']),'manifest_sha256':m['sha256'],'blocks':cov,'stop':execution['reason']}
    (out/'execution_audit.json').write_text(json.dumps(audit,indent=2),encoding='utf-8')
    report(m,data,summary,contrasts,audit)
    print(json.dumps({k:v for k,v in audit.items() if k!='blocks'}));return audit


def report(m,data,summary,contrasts,audit):
    out=HERE/'analysis'
    def table(headers,rr):return '| '+' | '.join(headers)+' |\n|'+'|'.join('---' for _ in headers)+'|\n'+''.join('| '+' | '.join(str(v) for v in r)+' |\n' for r in rr)
    txt=f'''# Luna constraint-program study v1

Status **{audit['status']}**. Recorded {audit['episodes']}/2000 episodes, {audit['complete_blocks']}/500 complete ABCD blocks. Missing {audit['missing']} episodes. Fixed endpoint reached: {audit['endpoint_reached']}.

## Preregistered design

500 distinct non-isomorphic seven-operation dependency programs, 100 structural-profile groups of five. Fixed seed 20261030, one Luna model, one repetition, all four exact original framings, C-B operational success primary (two-sided .05). Same system instructions, four function schemas, reasoning low, 24 tool/model calls and 2048 output tokens. Fresh state/conversation. No interim p-values, task substitution or sample-size change. Categories: 170 execution, 165 recovery, 165 stopping.

The new version adds explicit fork/join constraints, resource capacity, register guards, partial-effect resume, revision conflict and per-operation restrictions. Operation IDs are permuted to avoid leaking topological order. Independent exhaustive bitset search verifies a unique terminal business state under all legal macro action orders; a public-observation oracle validates actual tool/failure behavior. No LLM judge. Gold/fault schedules are not returned to models.

Novelty is exact dependency-topology uniqueness under node renaming/permutation, not a claim of 500 independent production workflows. Profiles group graph features; common interpreter/selection grammar and four mechanism classes can induce broader dependence. This is a synthetic constraint-program benchmark, not a random production task sample. It must not be silently pooled with previous linear-record experiments.

## Outcomes

'''
    txt+=table(['Condition','Success/evaluable','Final-state accuracy','Report accuracy','Mean tools','Mean unnecessary','Violations/episode'],[
        [s['condition'],f"{s['operational_successes']}/{s['episodes_evaluable']}",s['mean_verified_final_state_correct'],
            s['mean_final_report_status_correct'],s['mean_tool_calls'],s['mean_unnecessary_tool_calls'],s['mean_constraint_violations']]
        for s in summary if s['category']=='all'])
    txt+='\n## Primary result and stopping rule\n\n'
    primary=next((r for r in contrasts if r['category']=='all' and r['contrast']=='CB' and r['metric']=='operational_task_success'),None)
    if primary:
        txt+=f"Paired descriptive C-B difference: {primary['difference']}; complete paired tasks: {primary['paired_tasks']}; profile families: {primary['families']}; C-only successes: {primary['treatment_only_successes']}, B-only: {primary['reference_only_successes']}.\n\n"
    if audit['endpoint_reached']:
        txt+=f"95% category-stratified family bootstrap interval: [{primary['ci_low']}, {primary['ci_high']}]. Exact whole-family sign-flip p={primary['p_value']}. Broad four-mechanism sensitivity p={primary['broad_mechanism_sensitivity_p']}. These inference statements depend on the stated family independence/sign-symmetry assumptions.\n\n"
    else:
        txt+='The preregistered fixed endpoint was not reached. Per protocol, no primary inferential confidence interval, p-value or significance claim is calculated. All paired values are descriptive partial results; budget/infra censoring may be informative. Do not substitute the partial sample for the planned study or top up based on these outcomes.\n\n'
    txt+='Operational success includes state, correct final status, fresh verification and no constraint violation. Final-state correctness is separately reported; reporting-only gains cannot establish improved environment execution. Agent limits/truncation/incorrect reports fail; infrastructure is separate; unattempted budget censoring is not a failure. Valid alternative orders are allowed. Secondary Holm-adjusted results, if endpoint reached, are exploratory. A null or zero difference is not equivalence.\n\n'
    txt+='## Cost and audit\n\n'
    txt+=f"Known new usage-priced cost ${audit['known_new_cost_usd']}; new exposure ${audit['new_study_exposure_usd']}; remaining $4 study allowance ${audit['remaining_study_exposure_usd']}. Remaining approved Phase 3 allowance ${audit['remaining_phase3_usd']}; overall ${audit['remaining_global_usd']}. Historical encumbrances remain fully counted and are not confirmed charges. Pending IDs: `{audit['pending_reservations']}`.\n\n"
    txt+=f"Approved cap version: Phase 3 $9.363684130, overall $9.518315430, separate new-study $4.00. Approval is recorded in approval.json from the user's $4 instruction and explicit 'continue' response to the exact cap request. No prior holds were released. The persistent ledger is append-only, requests reserve before sending, SDK retries are disabled and unknown usage stops execution. Acknowledged responses are additionally journaled before returning to the agent loop. Old charged episodes are never rerun. Stop: `{audit['stop']}`.\n\n"
    txt+='Tokens/cost use reported API usage and frozen configured rates. Reasoning tokens are already part of output tokens, never billed twice. The observed-cache/task-aware forecasts are estimates, not guaranteed actual charges. Account balance and external usage are not verified; no recharge is relied on. See cost_analysis.csv and frozen manifest forecast.\n\n'
    txt+='## Limitations and reproduction\n\n'
    txt+='Planning showed only about 49% power for a five-point operational effect at N500 under q=7/54, five tasks/family and ICC≈.358. The study is exploratory for modest effects. Different tasks may change discordance, correlation, cost or ceiling behavior. The previous final-state B/C discordance was zero; that does not establish zero future variance. The new benchmark addresses structural variety but does not prove independent real-world scenario diversity. One repetition, shared engine, profile-group assumptions, alias drift, API latency/cache differences and prompt-length differences limit interpretation. Task-selection rules used only topology/feasibility and predated all new live results.\n\n'
    txt+=f"Manifest SHA256 `{m['sha256']}`. Protected prior files: {audit['protected_files']}. Source hash `{m['source_sha256']}`.\n\n"
    txt+='''```powershell
competitive_prompting/.venv/Scripts/python.exe -m competitive_prompting.phase3.luna_study_v1.analysis
competitive_prompting/.venv/Scripts/python.exe -m pytest competitive_prompting -q
```

No commits, pushes or PRs. Original studies and their conclusions remain unchanged.
'''
    (out/'final_experiment_report.md').write_text(txt,encoding='utf-8')
    (out/'validation_report.md').write_text(f"# Validation\n\n{audit['status']}; {audit['episodes']} unique episodes; {audit['complete_blocks']} complete blocks. Strict request/state/token/cost replay and independent gold validation passed for all recorded infrastructure-complete episodes. Prior {audit['protected_files']} file hashes and ledger prefix remain intact. Fixed endpoint: {audit['endpoint_reached']}. Incomplete blocks/exclusions and pending holds are preserved in execution_audit.json.\n\nOffline source/manifest/oracle/fault/negative/isomorphism/budget tests and 2000 scripted episodes precede paid calls; pytest.txt and postflight_pytest.txt hold full test results.\n",encoding='utf-8')


if __name__=='__main__':analyze()
