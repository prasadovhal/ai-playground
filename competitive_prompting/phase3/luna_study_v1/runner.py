"""Frozen Luna execution, default offline; shared Responses loop/settings retained."""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from types import FunctionType
from decimal import Decimal

from ... import runner as shared,analysis as old_analysis
from ..balanced.runner import validate_record as old_validate
from ..extension_v1.runner import credentials
from .tasks import by_id,VERSION
from .environment import Environment
from .oracle import OracleResponses
from .core import reference
from .plan import HERE,LEDGER,MODEL,MANIFEST,load,prepare,LIMITS
from .budget import Budget,RunLock,BudgetError,replay_ledger,money,NAMESPACE,STUDY_CAP

CASES=by_id()
run_episode=FunctionType(shared.run_episode.__code__,{**shared.run_episode.__globals__,'Environment':Environment},argdefs=shared.run_episode.__defaults__)
run_episode.__kwdefaults__=dict(shared.run_episode.__kwdefaults__)
BASE_VALIDATOR={**old_validate.__globals__,'BY_ID':CASES,'Environment':Environment,'replay_ledger':replay_ledger}


def complete(r):
    return r.get('independent_audit_passed',False) and r['termination_reason'] not in shared.INFRASTRUCTURE_STOPS and old_analysis.eligible(r)


def validate(row,item,m,budget=None):
    if any(row[k]!=item[k] for k in ('episode_id','task_id','model','condition','seed','repetition')):raise ValueError('Identity mismatch')
    if row['family']!=CASES[row['task_id']].family or row['manifest_sha256']!=m['sha256']:raise ValueError('Family/manifest mismatch')
    case=CASES[row['task_id']];t=case.task
    gold,_,status=reference(t.nodes,(t.initial_mask,t.initial_resources,t.initial_value))
    business=row['final_state']
    expected_counts={n.name:int(gold[0]>>i&1) for i,n in enumerate(t.nodes)}
    correct=(business['counts']==expected_counts and business['resources']==list(gold[1]) and
             business['register']==gold[2] and not business['partial'])
    if correct!=row['state_correct'] or status!=t.expected_status:raise ValueError('Independent gold mismatch')
    if old_analysis.eligible(row):
        # One complete strict ledger replay per episode, then use that immutable
        # snapshot for all per-call reconciliation checks in the unchanged validator.
        states=replay_ledger(budget.entries) if budget else {}
        g={**BASE_VALIDATOR,'replay_ledger':lambda entries:states}
        validator=FunctionType(old_validate.__code__,g,argdefs=old_validate.__defaults__)
        validator(row,item,m,budget)
    else:
        env=Environment(t)
        for interaction in row['interactions']:
            response=interaction.get('response')
            if not response or response.get('status')!='completed':break
            for call in response.get('output',[]):
                if call.get('type')!='function_call':continue
                if len(env.trace)>=LIMITS.tool_calls:break
                try:args=json.loads(call.get('arguments',''))
                except (ValueError,TypeError):args=call.get('arguments')
                env.call(call.get('name'),args)
                env.trace[-1].update(call_id=call['call_id'],response_id=response.get('id'))
        if env.trace!=row['action_trace'] or env.state!=row['final_state']:raise ValueError('Incomplete episode prefix corrupt')


def items(m):return [e for b in m['batches'] for block in b['blocks'] for e in block['episodes']]
def directory(n,execute):return HERE/'results'/('live' if execute else 'offline')/f'b{n:03d}'
def rows(execute=True):
    result=[]
    for n in range(1,101):
        p=directory(n,execute)/'episodes.jsonl'
        if p.exists():result.extend(json.loads(s) for s in p.read_text(encoding='utf-8').splitlines())
    return result


def coverage(data,m):
    ids={r['episode_id']:r for r in data}
    if len(ids)!=len(data):raise ValueError('Duplicate episode ID')
    result=[]
    for batch in m['batches']:
        for block in batch['blocks']:
            present=[ids[e['episode_id']] for e in block['episodes'] if e['episode_id'] in ids]
            result.append({'block_id':block['id'],'batch':batch['batch'],'task_id':block['task_id'],'family':block['family'],
                'category':block['category'],'recorded':len(present),'complete':len(present)==4 and all(complete(r) for r in present),
                'missing':[e['condition'] for e in block['episodes'] if e['episode_id'] not in ids],
                'infrastructure':[r['condition'] for r in present if not complete(r)]})
    return result


class Journal:
    def __init__(self,live,budget,path,episode):self.live=live;self.budget=budget;self.path=path;self.episode=episode;self.sequence=0
    def append(self,value):
        with self.path.open('a',encoding='utf-8') as f:
            f.write(json.dumps(value,ensure_ascii=False)+'\n');f.flush();os.fsync(f.fileno())
    def create(self,**request):
        self.sequence+=1;reservation=self.budget.entries[-1]
        if reservation['event']!='reserve' or reservation['episode_id']!=self.episode:raise ValueError('Unreserved API request')
        common={'episode_id':self.episode,'request_sequence':self.sequence,'reservation_id':reservation['id']}
        self.append({**common,'event':'request','request':request})
        try:response=self.live.create(**request)
        except Exception as exc:
            self.append({**common,'event':'api_error','error':shared.api_error_details(exc,self.episode,self.sequence)})
            raise
        self.append({**common,'event':'acknowledged_response','response':response})
        return response


def run(m,execute=False,client_factory=shared.LiveResponses):
    if execute:
        credentials()
        if hashlib.sha256(os.environ['OPENAI_PROJECT_ID'].encode()).hexdigest()!=m['project_id_sha256']:raise ValueError('Project mismatch')
        gate=json.loads((HERE/'offline_validation.json').read_text())
        log=(HERE/'pytest.txt').read_text(encoding='utf-8-sig')
        if not gate['passed'] or gate['manifest_sha256']!=m['sha256'] or 'passed' not in log or ' failed' in log:raise ValueError('Offline gates not passed')
    data=rows(execute);ordered=items(m);status='COMPLETE';reason=None;live=None
    out=HERE/'results'/('live' if execute else 'offline');out.mkdir(parents=True,exist_ok=True)
    def work(budget=None):
        nonlocal live,status,reason,data
        if [r['episode_id'] for r in data]!=[e['episode_id'] for e in ordered[:len(data)]]:raise ValueError('Persisted prefix mismatch')
        if any(not complete(r) for r in data):raise ValueError('Prior incomplete episode blocks automatic retries')
        for r,e in zip(data,ordered):validate(r,e,m,budget)
        if budget:
            actual={rid for rid,s in replay_ledger(budget.entries).items() if s['episode_id'].startswith(NAMESPACE)}
            recorded=[i['reservation_id'] for r in data for i in r['interactions']]
            if len(recorded)!=len(set(recorded)) or actual!=set(recorded):raise ValueError('Orphan/duplicate charged request; never replay paid acknowledgments')
        done={r['episode_id'] for r in data}
        for batch in m['batches']:
            load()
            d=directory(batch['batch'],execute);d.mkdir(parents=True,exist_ok=True)
            for block in batch['blocks']:
                missing=[e for e in block['episodes'] if e['episode_id'] not in done]
                if not missing:continue
                if execute:
                    try:budget.require_available(block['admission_allowance_usd'])
                    except BudgetError as exc:
                        status='PARTIAL_BUDGET';reason={'block_id':block['id'],'required':block['admission_allowance_usd'],
                            'study_remaining':str(STUDY_CAP-budget.study_exposure()),'message':str(exc)}
                        shared.write_result(d/'admissions.jsonl',{'admitted':False,**reason});return
                    shared.write_result(d/'admissions.jsonl',{'block_id':block['id'],'admitted':True,'required':block['admission_allowance_usd'],
                        'study_exposure':str(budget.study_exposure())})
                    if live is None:
                        live=client_factory(execute=True);access=live.check_access([MODEL])
                        if access[MODEL]['returned_id']!=MODEL:raise ValueError('Model ID substitution prohibited')
                        (out/'access.json').write_text(json.dumps({'project_sha256':m['project_id_sha256'],'models':access}),encoding='utf-8')
                for e in missing:
                    case=CASES[e['task_id']]
                    transport=Journal(live,budget,d/'requests.jsonl',e['episode_id']) if execute else OracleResponses(case)
                    row=run_episode(case.task,e['condition'],MODEL,0,e['seed'],transport,limits=LIMITS,effort='low',
                        pricing=m['pricing']['models'][MODEL],budget=budget,dry_run=not execute,episode_id=e['episode_id'],run_id=VERSION)
                    row.update(family=case.family,mechanism=case.mechanism,execution_batch=batch['batch'],block_id=block['id'],
                        manifest_sha256=m['sha256'],benchmark_manifest_sha256=m['sha256'],source_sha256=m['source_sha256'],independent_audit_passed=True,
                        uninspected_write_retries=sum(t.get('uninspected_retry',False) for t in row['action_trace']))
                    try:validate(row,e,m,budget)
                    except Exception as exc:
                        row.update(original_termination_reason=row['termination_reason'],termination_reason='evaluator_error',
                            verified_task_success=None,independent_audit_passed=False)
                        row['api_errors'].append({'type':'IndependentAuditError','message':str(exc)})
                    shared.write_result(d/'episodes.jsonl',row);data.append(row);done.add(e['episode_id'])
                    print(f'Batch {batch["batch"]}: {len(data)}/2000 {e["task_id"]} {e["condition"]} {row["termination_reason"]}',flush=True)
                    if not complete(row):
                        status='PARTIAL_BUDGET' if row['termination_reason']=='budget_limit' else 'PARTIAL_INFRASTRUCTURE'
                        reason={'episode_id':row['episode_id'],'reason':row['termination_reason']};return
            if not all(b['complete'] for b in coverage(data,m) if b['batch']==batch['batch']):raise ValueError('Batch incomplete')
            check={'passed':True,'episodes':20,'blocks':5,'manifest_sha256':m['sha256'],'dry_run':not execute}
            (d/'validation.json').write_text(json.dumps(check,indent=2),encoding='utf-8')
            print(f'BATCH {batch["batch"]} VALIDATED'+(f'; study remaining ${STUDY_CAP-budget.study_exposure()}' if execute else '; offline'),flush=True)
    try:
        if execute:
            with RunLock(LEDGER):work(Budget(LEDGER,m['approval']))
        else:work()
    except Exception as exc:
        status='FAILED_VALIDATION' if isinstance(exc,(ValueError,AssertionError,KeyError,TypeError)) else 'PARTIAL_INFRASTRUCTURE'
        reason=shared.api_error_details(exc,None,0)
    finally:
        if live:live.close()
    c=coverage(data,m)
    result={'status':status,'episodes':len(data),'planned':2000,'blocks':sum(b['complete'] for b in c),'planned_blocks':500,
        'reason':reason,'manifest_sha256':m['sha256'],'dry_run':not execute}
    (out/'execution_status.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    if not execute:
        (HERE/'offline_validation.json').write_text(json.dumps({'passed':status=='COMPLETE' and len(data)==2000,
            'episodes':len(data),'manifest_sha256':m['sha256']},indent=2),encoding='utf-8')
    print(json.dumps(result),flush=True);return result


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--prepare',action='store_true');p.add_argument('--execute',action='store_true')
    a=p.parse_args()
    if a.prepare:
        if a.execute:p.error('Freeze is offline')
        print(prepare()['sha256']);return
    result=run(load(),a.execute)
    if result['status'] not in ('COMPLETE','PARTIAL_BUDGET'):raise SystemExit(1)


if __name__=='__main__':main()
