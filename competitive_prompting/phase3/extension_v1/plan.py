"""Offline freeze, provenance, novelty checks and prespecified balanced order."""
from collections import Counter
from dataclasses import asdict
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import random
from statistics import mean

from ... import runner as shared
from ...prompts import CONDITIONS, SYSTEM, FINAL_FORMAT, messages
from ...simulator import TOOLS
from ...tasks import TASKS
from ..tasks import ALL as OLD_CASES
from ..evaluation import oracle
from ..preparation import LIMITS, MODELS, oracle_requests
from ..balanced.plan import load_plan as old_plan, request_peak
from ..results.balanced_v1.final_analysis.recovery_v1.execute import effective_rows
from ..results.balanced_v1.final_analysis.recovery_v1.accounting import RecoveryBudget
from .tasks import EXAMPLES, META, VERSION

HERE = Path(__file__).resolve().parent
MANIFEST = HERE/'manifest_v1.json'
LEDGER = shared.ROOT/'results/spend.jsonl'
SEED = 20261029


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sources():
    return {p.name:digest(p) for p in sorted(HERE.glob('*.py'))}


def structure(task):
    """Name-invariant record/dependency/fault fingerprint, including shared steps."""
    ids = {r.id:i for i,r in enumerate(task.records)}
    vocabulary = {}
    records = []
    for r in task.records:
        steps = []
        for s in r.steps:
            if s not in vocabulary: vocabulary[s] = len(vocabulary)
            steps.append(vocabulary[s])
        records.append((steps, r.counts or (0,)*len(r.steps), r.authorized, r.available, r.requirements_met))
    failures = [(f.tool,ids.get(f.record,-1), task.records[ids[f.record]].steps.index(f.operation)
                 if f.operation else -1,f.attempt,f.kind) for f in task.failures]
    return shared.json_hash([records,failures,task.expected_status])


def validate_tasks():
    assert len(EXAMPLES)==60 and len(META)==60
    assert Counter(e.case.task.category for e in EXAMPLES)==dict(execution=20,recovery=20,stopping=20)
    assert len({e.family for e in EXAMPLES})==30
    assert set(Counter(e.family for e in EXAMPLES).values())=={2}
    old = {structure(t) for t in TASKS} | {structure(c.task) for c in OLD_CASES}
    fingerprints = [structure(e.case.task) for e in EXAMPLES]
    assert not old.intersection(fingerprints), 'Structural duplicate of original task'
    assert len(set(fingerprints))==60, 'Extension structural duplicate'
    scripts = {}
    for e in EXAMPLES:
        script = oracle(e.case)
        assert len(script['actions'])<=LIMITS.tool_calls and len(script['actions'])+1<=LIMITS.model_calls
        scripts[e.case.task.id] = script
    return scripts


def protected_files():
    result = {}
    for p in shared.ROOT.rglob('*'):
        rel = p.relative_to(shared.ROOT)
        if (not p.is_file() or any(x in rel.parts for x in ('.venv','__pycache__','.pytest_cache','extension_v1'))
                or p==LEDGER or p.suffix=='.lock' or p.suffix=='.pyc'):
            continue
        result[str(rel).replace('\\','/')] = digest(p)
    return result


def verify_protected(manifest):
    for name, sha in manifest['protected_files'].items():
        if digest(shared.ROOT/name)!=sha: raise ValueError('Original protected file changed: '+name)
    prefix = manifest['ledger_prefix']
    with LEDGER.open('rb') as f: data = f.read(prefix['bytes'])
    if len(data)!=prefix['bytes'] or hashlib.sha256(data).hexdigest()!=prefix['sha256']:
        raise ValueError('Original ledger prefix changed')


def calibrations():
    rows,_ = effective_rows()
    groups = {}
    for row in rows:
        for i in row['interactions']:
            if not i.get('response',{}).get('usage'): continue
            u=i['response']['usage']
            size=len(json.dumps(i['request'],ensure_ascii=False).encode())
            groups.setdefault((row['model'],row['category']),[]).append((
                u['input_tokens']/size,u['output_tokens'],Decimal(i['reported_cost_usd'])/size,
                i['request']['max_output_tokens']))
    return groups


def freeze():
    scripts=validate_tasks()
    parent=old_plan()
    groups=calibrations()
    pricing=json.loads((shared.ROOT/'pricing.json').read_text())
    rng=random.Random(SEED)
    # Each batch uses two DIFFERENT families/category. Family siblings occur in
    # separate halves; shuffle membership once before outcomes, never adapt it.
    category_lists={}
    for category in ('execution','recovery','stopping'):
        families=sorted({e.family for e in EXAMPLES if e.case.task.category==category})
        rng.shuffle(families)
        first=[];second=[]
        for family in families:
            members=[e for e in EXAMPLES if e.family==family]
            rng.shuffle(members);first.append(members[0]);second.append(members[1])
        category_lists[category]=first+second
    batches=[]
    for n in range(10):
        selected=[e for cat in category_lists for e in category_lists[cat][2*n:2*n+2]]
        blocks=[(e,model) for e in selected for model in MODELS]
        rng.shuffle(blocks)
        planned=[]
        for e,model in blocks:
            conditions=list(CONDITIONS);rng.shuffle(conditions)
            requests=max((oracle_requests(e.case,c,model) for c in CONDITIONS),
                key=lambda rr:sum(len(json.dumps(r,ensure_ascii=False).encode()) for r in rr))
            bytes_sum=sum(len(json.dumps(r,ensure_ascii=False).encode()) for r in requests)
            measured=groups[(model,e.case.task.category)]
            price=pricing['models'][model]
            nominal=Decimal(str(bytes_sum))*mean(v[2] for v in measured)
            # Maximum observed input/byte ratio, no cache discount, cache-write
            # price, 95th-percentile output +256 tokens/request, 50% contingency.
            output=sorted(v[1] for v in measured)[int(.95*(len(measured)-1))]+256
            conservative=(Decimal(str(bytes_sum*max(v[0] for v in measured)))*
                max(Decimal(str(price['input'])),Decimal(str(price['cache_write'])))+
                Decimal(len(requests)*output)*Decimal(str(price['output'])))/Decimal(1000000)*Decimal('1.5')
            peak=request_peak(price)
            planned.append({'id':f'{VERSION}:batch{n+1}:block{len(planned)+1}',
                'task_id':e.case.task.id,'model':model,'family':e.family,
                'observed_forecast_usd':str(nominal*4),'conservative_forecast_usd':str(conservative*4),
                'admission_allowance_usd':str(conservative*4+peak),
                'episodes':[{'episode_id':f'phase3:extension_v1:{model}:{e.case.task.id}:0:{c}',
                    'task_id':e.case.task.id,'model':model,'condition':c,'seed':SEED,'repetition':0} for c in conditions]})
        batches.append({'batch':n+1,'task_ids':[e.case.task.id for e in selected],'blocks':planned})
    ledger=LEDGER.read_bytes()
    budget=RecoveryBudget(LEDGER)
    manifest={'version':VERSION,'seed':SEED,'models':list(MODELS),'episodes':480,'batches':batches,
        'settings':{'limits':asdict(LIMITS),'reasoning_effort':{m:'low' for m in MODELS},
                    'request':shared.REQUEST_SETTINGS,'system':SYSTEM,'final_format':FINAL_FORMAT,'tools':TOOLS},
        'conditions':{c:{'text':s,'sha256':hashlib.sha256(s.encode()).hexdigest()} for c,s in CONDITIONS.items()},
        'pricing':pricing,'project_id_sha256':parent['project_id_sha256'],
        'tasks':{e.case.task.id:{'specification':json.loads(json.dumps(asdict(e.case))),
            'sha256':shared.json_hash(json.loads(json.dumps(asdict(e.case)))),
            'family':e.family,'difficulty':e.difficulty,'design':e.design,
            'structure_sha256':structure(e.case.task),'oracle_actions':scripts[e.case.task.id]['actions']}
            for e in EXAMPLES},
        'source_files':sources(),'source_sha256':shared.json_hash(sources()),'original_source_sha256':parent['parent_source_sha256'],
        'original_plan_sha256':parent['sha256'],'original_orchestration_sha256':parent['orchestration_source_sha256'],
        'protected_files':protected_files(),
        'ledger_prefix':{'bytes':len(ledger),'sha256':hashlib.sha256(ledger).hexdigest()},
        'starting_budget':{'known_spend_usd':str(budget.reported_spend()),
            'encumbered_usd':str(budget.encumbered_exposure()),'phase3_headroom_usd':str(Decimal(6)-budget.phase_exposure()),
            'experiment_headroom_usd':str(Decimal(9)-budget.exposure())},
        'caps_usd':{'phase3':'6.00','experiment':'9.00','pilot':'0.75'},
        'randomization':'Seed 20261029. Shuffle ten families/category, separate siblings across halves, shuffle sibling assignment. Two families/category/batch. Shuffle twelve model/task blocks/batch, then conditions per block.',
        'analysis_plan':{'primary':'C-B operational success, separate model effects; Holm across two model primary cluster sign-flip tests.',
            'secondary':['D-C','B-A','C-A','D-A','combined rivalry-B (exploratory)'],
            'denominators':'All attempted agent episodes; completed-incorrect, invalid reports, output truncation and limits fail. Infrastructure separate. Unattempted due to budget censored. Complete ABCD blocks only for paired contrasts.',
            'cluster':'Task-weighted effects. Stratified family-cluster bootstrap within category, 5000 draws seed 20261029, intervals only >=8 families. Whole-family sign flips (exact <=16 nonzero families, 10000 Monte Carlo otherwise); paired task McNemar diagnostic. Shared simulator limits independence even between named domain families.',
            'multiplicity':'Holm two-model CB operational primary family; remaining metrics/contrasts separate exploratory Holm family. Category effects exploratory.',
            'effect_sizes':'Absolute paired mean, relative difference vs reference, standardized paired difference; discordant binary pairs.',
            'replication':'Extension first, original results separate. Compare direction and uncertainty; no automatic pooling. Do not interpret null as equivalence.',
            'activity':'Check CB tool reductions jointly with state correctness and remaining authorized zero-count operations; trace case audit. Report Sol ceilings.',
            'selection':'Execute immutable order; only infrastructure/accounting and budget govern continuation. No rebalancing based on outcomes.'},
        'admission_rule':'Before every ABCD block require four conservative oracle-workload episode forecasts plus one maximum request reservation. Estimate is not a guarantee; requests always reserve/reconcile independently. Stop on any unknown usage/error; no automatic encumbrances.',
        'novelty_limit':'New domains and name-invariant task structures checked against original42 cases. Same linear per-record simulator cannot test cross-record dependencies or true asynchronous/partial multi-operation transactions. Non-prefix states represent audit backfill after independently recorded downstream work; transient before/after writes model partial workflow outcomes.'}
    manifest['sha256']=shared.json_hash(manifest)
    with MANIFEST.open('x',encoding='utf-8') as f: json.dump(manifest,f,indent=2)
    with (HERE/'oracle_traces.jsonl').open('x',encoding='utf-8') as f:
        for script in scripts.values(): f.write(json.dumps(script)+'\n')
    return manifest


def load(path=MANIFEST, *, protected=True):
    m=json.loads(Path(path).read_text(encoding='utf-8'))
    payload={k:v for k,v in m.items() if k!='sha256'}
    if shared.json_hash(payload)!=m['sha256'] or m['source_files']!=sources(): raise ValueError('Frozen extension hash mismatch')
    if old_plan()['sha256']!=m['original_plan_sha256']: raise ValueError('Original source/plan changed')
    for e in EXAMPLES:
        if m['tasks'][e.case.task.id]['sha256']!=shared.json_hash(json.loads(json.dumps(asdict(e.case)))):
            raise ValueError('Task changed')
    if protected: verify_protected(m)
    return m
