"""Freeze all 500 programs, gold states, schedule, controls and analysis before calls."""
from dataclasses import asdict
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import random
from statistics import mean

from ... import runner as shared
from ...prompts import CONDITIONS,SYSTEM,FINAL_FORMAT,messages
from ...simulator import TOOLS
from ..preparation import LIMITS
from ..extension_v1.plan import load as old_manifest
from ..extension_v1.analysis import AuditLedger
from ..luna_planning_v1.prepare import digest,cost_forecast
from ..extension_v1.runner import all_rows
from ..results.balanced_v1.final_analysis.recovery_v1.execute import effective_rows
from .tasks import cases,canonical_graph,VERSION
from .oracle import oracle
from .budget import GLOBAL_CAP,PHASE_CAP,STUDY_CAP,NAMESPACE

HERE=Path(__file__).resolve().parent
MANIFEST=HERE/'manifest_v1.json'
LEDGER=shared.ROOT/'results/spend.jsonl'
MODEL='gpt-6-luna'
SEED=20261030


def source_files():
    return {p.name:digest(p) for p in sorted(HERE.glob('*.py'))}


def protected_files():
    result={}
    for directory,dirs,names in os.walk(shared.ROOT):
        dirs[:]=[d for d in dirs if d not in ('.venv','__pycache__','.pytest_cache','luna_study_v1')]
        for name in names:
            p=Path(directory)/name
            if p==LEDGER or p.suffix in ('.pyc','.lock'):continue
            result[str(p.relative_to(shared.ROOT)).replace('\\','/')]=digest(p)
    return result


def verify_protected(m):
    for name,sha in m['protected_files'].items():
        if digest(shared.ROOT/name)!=sha:raise ValueError('Historical protected file changed: '+name)
    with LEDGER.open('rb') as f:data=f.read(m['ledger_prefix']['bytes'])
    if hashlib.sha256(data).hexdigest()!=m['ledger_prefix']['sha256']:raise ValueError('Historical ledger prefix changed')


def oracle_requests(case,condition):
    s=oracle(case);history=messages(case.task,condition);requests=[]
    for i,a in enumerate(s['actions']):
        requests.append(shared.build_request(case.task,condition,MODEL,LIMITS,history=history))
        history.extend([{'type':'function_call','call_id':f'offline_{i}','name':a['tool'],'arguments':json.dumps(a['arguments'])},
            {'type':'function_call_output','call_id':f'offline_{i}','output':json.dumps(s['trace'][i]['response'],sort_keys=True)}])
    requests.append(shared.build_request(case.task,condition,MODEL,LIMITS,history=history))
    return requests


def prepare():
    prior=old_manifest();approval=json.loads((HERE/'approval.json').read_text())
    prices=json.loads((shared.ROOT/'pricing.json').read_text())
    calibration,_=cost_forecast(effective_rows()[0],all_rows())
    # Actual request costs and sizes, not estimated final token counts.
    measured=[]
    for r in effective_rows()[0]+all_rows():
        if r['model']!=MODEL:continue
        for i in r['interactions']:
            if not i.get('reported_cost_usd'):continue
            u=i['response']['usage'];size=len(json.dumps(i['request'],ensure_ascii=False).encode())
            measured.append((Decimal(i['reported_cost_usd'])/size,u['input_tokens']/size,u['output_tokens']))
    ratio=mean(x[0] for x in measured)
    input_bound=max(x[1] for x in measured)
    output_bound=sorted(x[2] for x in measured)[int(.95*(len(measured)-1))]+256
    rng=random.Random(SEED);families=sorted({c.family for c in cases()});rng.shuffle(families)
    entries={};batches=[];scripts=[]
    for c in cases():
        s=oracle(c);scripts.append(s)
        if len(s['actions'])+1>LIMITS.model_calls or len(s['actions'])>LIMITS.tool_calls:raise ValueError('Oracle exceeds fixed limits')
        payload=json.loads(json.dumps(asdict(c.task)))
        entries[c.task.id]={'specification':{'task':payload},'sha256':shared.json_hash(payload),
            'family':c.family,'category':c.task.category,'graph_sha256':c.graph_hash,'mechanism':c.mechanism,
            'oracle_actions':s['actions'],'gold_sha256':shared.json_hash(s['final_state'])}
    for n,family in enumerate(families,1):
        group=[c for c in cases() if c.family==family];rng.shuffle(group);blocks=[]
        for c in group:
            cond=list(CONDITIONS);rng.shuffle(cond)
            requests=max((oracle_requests(c,k) for k in CONDITIONS),key=lambda rr:sum(len(json.dumps(x,ensure_ascii=False).encode()) for x in rr))
            sizes=[len(json.dumps(x,ensure_ascii=False).encode()) for x in requests]
            if max(sizes)>LIMITS.request_bytes:raise ValueError('Oracle exceeds request-size limit')
            nominal=sum(sizes)*ratio
            price=prices['models'][MODEL]
            no_cache=(Decimal(str(sum(sizes)*input_bound))*Decimal(str(price['cache_write']))+
                Decimal(len(requests)*output_bound)*Decimal(str(price['output'])))/Decimal(1000000)*Decimal('1.5')
            blocks.append({'id':f'{VERSION}:b{n:03d}:{c.task.id}','task_id':c.task.id,'family':family,
                'category':c.task.category,'oracle_model_calls':len(requests),'max_oracle_request_bytes':max(sizes),
                'observed_forecast_usd':str(nominal*4),'conservative_forecast_usd':str(no_cache*4),
                'admission_allowance_usd':str(no_cache*4+Decimal('.02')),
                'episodes':[{'episode_id':f'{NAMESPACE}{c.task.id}:0:{k}','task_id':c.task.id,'model':MODEL,
                    'condition':k,'repetition':0,'seed':SEED} for k in cond]})
        batches.append({'batch':n,'family':family,'blocks':blocks})
    ledger=AuditLedger();prefix=LEDGER.read_bytes()
    m={'version':VERSION,'model':MODEL,'seed':SEED,'tasks':entries,'batches':batches,'episodes':2000,'task_count':500,
        'conditions':{k:{'text':v,'sha256':hashlib.sha256(v.encode()).hexdigest()} for k,v in CONDITIONS.items()},
        'settings':{'limits':asdict(LIMITS),'reasoning_effort':'low','request':shared.REQUEST_SETTINGS,
            'system':SYSTEM,'final_format':FINAL_FORMAT,'tools':TOOLS},'pricing':prices,
        'source_files':source_files(),'source_sha256':shared.json_hash(source_files()),
        'approval':approval,'approval_sha256':digest(HERE/'approval.json'),'project_id_sha256':prior['project_id_sha256'],
        'prior_manifest_sha256':prior['sha256'],'protected_files':protected_files(),
        'ledger_prefix':{'bytes':len(prefix),'sha256':hashlib.sha256(prefix).hexdigest()},
        'starting_exposure':{'global':str(ledger.exposure()),'phase3':str(ledger.phase_exposure()),
            'encumbered_unverified':str(ledger.encumbered_exposure())},
        'caps':{'study':str(STUDY_CAP),'phase3':str(PHASE_CAP),'global':str(GLOBAL_CAP)},
        'randomization':'Seed 20261030 shuffles 100 structural-profile families, five tasks/family, then ABCD per task; one model, one repetition, fresh state/conversation. Family membership fixed from graph structure, never model outcomes.',
        'novelty':{'graphs':500,'nonisomorphic_graphs':500,'families':100,'family_basis':'Same unlabeled graph degree/source/sink/depth profile, five DIFFERENT topologies per family.',
            'old_template_exclusion':'Every new graph has multiple source nodes and an indegree>=2 join; previous environments allowed only independent linear chains.',
            'qualification':'Procedurally generated distinct constraint programs, not 500 independent hand-authored production workflows. No repeated dependency topology, even after renaming/permuting nodes. Shared interpreter, selection grammar and four mechanism classes limit broader independence/generalization. Family independence is a conditional analysis assumption, not a certified fact.'},
        'primary':{'contrast':'CB','outcome':'operational_task_success','null':'E[C-B]=0','alpha':.05,'sidedness':'two-sided',
            'endpoint':'All 500 four-condition task blocks completed and audited. No interim p-values or outcome-driven task/sample selection.',
            'test':'Exact whole-family sign flip of integer paired task sums; conditional independent-family/joint-sign-symmetry assumptions.',
            'interval':'10000-draw category-stratified family bootstrap, task-weighted; seed 20261030.',
            'power':'Planning analysis retained in phase3/luna_planning_v1. Five tasks/family, q=7/54 and rho~.358 imply ~49% power for 5pp and ~59% for 5.56pp at N500. This is exploratory, not an 80-90% small-effect design.'},
        'secondary':{'contrasts':['DC','BA','CA','DA','combined_rivalry_B'],
            'outcomes':['verified_final_state_correct','final_report_status_correct','constraint_violations','incorrect_retries','duplicate_operations','correct_stopping','tool_calls','unnecessary_tool_calls','tokens','cost','latency'],
            'multiplicity':'Holm across all predeclared secondary model/category/contrast/outcome tests; one unadjusted primary.',
            'sensitivity':'Whole-mechanism sign-flip sensitivity (four broad classes), descriptive category summaries. Shared mechanisms may invalidate broader independence; do not claim population generalization from profile-only intervals.'},
        'stopping':'Only infrastructure, integrity or budget. Before EACH complete ABCD block require conservative workload plus peak request allowance; every request reserves/settles. No automatic retries/new encumbrances. Unknown costs stop. No cap increases or task substitution based on results. At budget/infrastructure termination, audit and descriptive partial report only; no confirmatory p/CI because fixed endpoint not reached.',
        'evaluation':'Expected business counts/resources/register computed by independent exhaustive bitset search; all legal macro action orders have one terminal state. Validate public-observation oracle through tool engine, fault/reset/unsafe tests. No LLM judge. Operational success requires correct gold state, schema/status, fresh verification and zero violations; final-state correctness is separate. Original four schemas/API/settings retained, new environment version changes task semantics and cannot be silently pooled with previous studies.',
        'cost_evidence':calibration,'forecast':{'observed_cache_usd':str(sum(Decimal(b['observed_forecast_usd']) for a in batches for b in a['blocks'])),
            'no_cache_conservative_usd':str(sum(Decimal(b['conservative_forecast_usd']) for a in batches for b in a['blocks']))},
        'release':'Offline validation required; explicit user authorization already recorded. No results inspected to choose these tasks.'}
    m['sha256']=shared.json_hash(m)
    with MANIFEST.open('x',encoding='utf-8') as f:json.dump(m,f,indent=2)
    with (HERE/'oracle_traces.jsonl').open('x',encoding='utf-8') as f:
        for s in scripts:f.write(json.dumps(s)+'\n')
    return m


def load():
    m=json.loads(MANIFEST.read_text(encoding='utf-8'))
    if shared.json_hash({k:v for k,v in m.items() if k!='sha256'})!=m['sha256'] or m['source_files']!=source_files():
        raise ValueError('Frozen Luna source/manifest hash mismatch')
    if m['approval_sha256']!=digest(HERE/'approval.json'):raise ValueError('Authorization changed')
    verify_protected(m)
    return m
