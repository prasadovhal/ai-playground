"""Sequential extension supervisor; unchanged paid loop, scorer and ledger caps."""
import argparse
from dataclasses import asdict
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
from types import FunctionType

from ... import runner as shared
from ...budget import usage_cost
from ..preparation import LIMITS, MODELS
from ..evaluation import OracleResponses
from ..runner import audit_row, infrastructure_complete
from ..balanced.runner import validate_record as original_validate
from ..results.balanced_v1.final_analysis.recovery_v1.accounting import (
    RecoveryBudget, RunLock, BudgetError, replay_ledger, private, money)
from .tasks import BY_ID, META, VERSION
from .plan import HERE, MANIFEST, LEDGER, load, freeze


class ExtensionBudget(RecoveryBudget):
    def reserve(self, allowance, episode_id):
        if not episode_id.startswith('phase3:extension_v1:'):
            raise BudgetError('Extension reservation namespace required')
        return private.Budget.reserve(self, allowance, episode_id)


# Bind the unchanged validator to the NEW task table and the already-authorized
# ledger decoder. No global monkeypatch, experimental source edit or scorer fork.
validate_record = FunctionType(original_validate.__code__,
    {**original_validate.__globals__, 'BY_ID':BY_ID, 'replay_ledger':replay_ledger},
    argdefs=original_validate.__defaults__)


def directory(batch, execute):
    return HERE/'results'/('live' if execute else 'offline')/f'batch{batch:02d}'


def read_rows(d):
    p=d/'episodes.jsonl'
    return [json.loads(s) for s in p.read_text(encoding='utf-8').splitlines()] if p.exists() else []


def items(manifest):
    return [e for b in manifest['batches'] for block in b['blocks'] for e in block['episodes']]


def all_rows(execute=True):
    return [r for n in range(1,11) for r in read_rows(directory(n,execute))]


def audit_rows(rows, manifest, budget=None):
    expected={e['episode_id']:e for e in items(manifest)}
    seen=set()
    for r in rows:
        if r['episode_id'] in seen: raise ValueError('Duplicate episode ID')
        seen.add(r['episode_id'])
        e=expected[r['episode_id']]
        if r['family']!=META[r['task_id']].family or r['extension_manifest_sha256']!=manifest['sha256']:
            raise ValueError('Extension provenance mismatch')
        if infrastructure_complete(r): validate_record(r,e,manifest,budget)
        else:
            # Retain exact prefix and independently check state even on API failure.
            audit_row(BY_ID[r['task_id']],r)
    return seen


def check_reservations(rows, budget):
    actual={rid for rid,s in replay_ledger(budget.entries).items()
            if s['episode_id'].startswith('phase3:extension_v1:')}
    recorded=[i['reservation_id'] for r in rows for i in r['interactions'] if i.get('reservation_id')]
    if len(recorded)!=len(set(recorded)) or actual!=set(recorded):
        raise ValueError('Orphan/duplicate reservation; no acknowledged request may be repeated')


def block_coverage(rows, manifest):
    lookup={r['episode_id']:r for r in rows}
    result=[]
    for batch in manifest['batches']:
        for block in batch['blocks']:
            present=[lookup[e['episode_id']] for e in block['episodes'] if e['episode_id'] in lookup]
            result.append({'batch':batch['batch'],'block_id':block['id'],'task_id':block['task_id'],
                'model':block['model'],'family':block['family'],'recorded':len(present),
                'complete':len(present)==4 and all(infrastructure_complete(r) for r in present),
                'missing_conditions':[e['condition'] for e in block['episodes'] if e['episode_id'] not in lookup],
                'infrastructure_conditions':[r['condition'] for r in present if not infrastructure_complete(r)]})
    return result


def credentials():
    path=shared.ROOT.parent/'.env'
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            k,sep,v=line.removeprefix('export ').partition('=')
            if sep and k.strip() in ('OPENAI_API_KEY','OPENAI_PROJECT_ID'):
                os.environ[k.strip()]=v.strip().strip('"').strip("'")
    if not all(os.environ.get(k) for k in ('OPENAI_API_KEY','OPENAI_PROJECT_ID')):
        raise ValueError('Missing intended project credentials')


def run_all(manifest, *, execute=False, client_factory=shared.LiveResponses):
    if execute:
        credentials()
        if hashlib.sha256(os.environ['OPENAI_PROJECT_ID'].encode()).hexdigest()!=manifest['project_id_sha256']:
            raise ValueError('Intended OpenAI project mismatch')
        gate=json.loads((HERE/'offline_validation.json').read_text(encoding='utf-8'))
        log=(HERE/'pytest.txt').read_text(encoding='utf-8')
        if gate['manifest_sha256']!=manifest['sha256'] or not gate['passed'] or 'passed' not in log or ' failed' in log:
            raise ValueError('Full offline suite and all scripted batches must pass')
    status='COMPLETE';stop=None;live=None

    def work(budget=None):
        nonlocal status,stop,live
        prior=all_rows(execute)
        audit_rows(prior,manifest,budget)
        if execute: check_reservations(prior,budget)
        if any(not infrastructure_complete(r) for r in prior):
            raise ValueError('Existing infrastructure failure; cannot selectively rerun')
        ordered=items(manifest)
        if [r['episode_id'] for r in prior]!=[e['episode_id'] for e in ordered[:len(prior)]]:
            raise ValueError('Persisted rows are not the frozen execution prefix')
        for batch in manifest['batches']:
            # Recheck frozen sources and original files before every batch.
            load()
            d=directory(batch['batch'],execute);d.mkdir(parents=True,exist_ok=True)
            for block in batch['blocks']:
                existing={r['episode_id'] for r in prior}
                missing=[e for e in block['episodes'] if e['episode_id'] not in existing]
                if not missing: continue
                if execute:
                    required=money(block['admission_allowance_usd'])
                    try: budget.require_available(required)
                    except BudgetError as exc:
                        available=min(Decimal(6)-budget.phase_exposure(),Decimal(9)-budget.exposure())
                        status='PARTIAL_BUDGET'
                        stop={'block_id':block['id'],'required_usd':str(required),'available_usd':str(available),
                            'shortfall_to_admit_next_block_usd':str(max(Decimal(0),required-available)),
                            'message':str(exc),'unattempted_conditions':[e['condition'] for e in missing]}
                        shared.write_result(d/'admissions.jsonl',{'admitted':False,**stop})
                        return
                    shared.write_result(d/'admissions.jsonl',{'block_id':block['id'],'admitted':True,
                        'allowance_usd':str(required),'phase3_exposure_usd':str(budget.phase_exposure())})
                    if live is None:
                        live=client_factory(execute=True)
                        access=live.check_access(MODELS)
                        if any(access[m]['returned_id']!=m for m in MODELS): raise ValueError('Model access mismatch')
                        (HERE/'results/live/access.json').write_text(json.dumps({'project_sha256':manifest['project_id_sha256'],
                            'models':access}),encoding='utf-8')
                for item in missing:
                    case=BY_ID[item['task_id']]
                    row=shared.run_episode(case.task,item['condition'],item['model'],0,item['seed'],
                        live if execute else OracleResponses(case),limits=LIMITS,effort='low',
                        pricing=manifest['pricing']['models'][item['model']],budget=budget,dry_run=not execute,
                        episode_id=item['episode_id'],run_id=VERSION)
                    row.update(family=META[item['task_id']].family,difficulty=META[item['task_id']].difficulty,
                        execution_batch=batch['batch'],block_id=block['id'],extension_manifest_sha256=manifest['sha256'],
                        benchmark_manifest_sha256=manifest['sha256'],source_sha256=manifest['source_sha256'])
                    audit_row(case,row)
                    if infrastructure_complete(row):
                        try: validate_record(row,item,manifest,budget)
                        except Exception as exc:
                            row.update(original_termination_reason=row['termination_reason'],termination_reason='evaluator_error',
                                verified_task_success=None,independent_audit_passed=False)
                            row['api_errors'].append({'type':'ReplayAuditError','message':str(exc)})
                    shared.write_result(d/'episodes.jsonl',row)
                    prior.append(row)
                    print(f'Batch {batch["batch"]}: {len(prior)}/480 {item["model"]} {item["task_id"]} {item["condition"]} {row["termination_reason"]}',flush=True)
                    if not infrastructure_complete(row):
                        status='PARTIAL_INFRASTRUCTURE';stop={'episode_id':row['episode_id'],'reason':row['termination_reason']}
                        return
            current=read_rows(d)
            audit_rows(current,manifest,budget)
            if execute: check_reservations(prior,budget)
            coverage=[b for b in block_coverage(prior,manifest) if b['batch']==batch['batch']]
            if len(current)!=48 or not all(b['complete'] for b in coverage): raise ValueError('Batch integrity failure')
            (d/'validation.json').write_text(json.dumps({'passed':True,'episodes':48,'complete_blocks':12,
                'manifest_sha256':manifest['sha256'],'dry_run':not execute},indent=2),encoding='utf-8')
            print(f'BATCH {batch["batch"]} VALIDATED; '+(f'Phase3 headroom ${Decimal(6)-budget.phase_exposure()}' if execute else 'offline'),flush=True)
    try:
        if execute:
            with RunLock(LEDGER): work(ExtensionBudget(LEDGER))
        else: work()
    except Exception as exc:
        status='FAILED_VALIDATION' if isinstance(exc,(ValueError,AssertionError,KeyError,TypeError)) else 'PARTIAL_INFRASTRUCTURE'
        stop=shared.api_error_details(exc,None,0)
    finally:
        if live: live.close()
    rows=all_rows(execute)
    out=HERE/'results'/('live' if execute else 'offline');out.mkdir(parents=True,exist_ok=True)
    coverage=block_coverage(rows,manifest)
    result={'status':status,'recorded':len(rows),'planned':480,'complete_blocks':sum(b['complete'] for b in coverage),
        'stop':stop,'manifest_sha256':manifest['sha256'],'dry_run':not execute}
    (out/'execution_status.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    if not execute:
        (HERE/'offline_validation.json').write_text(json.dumps({'passed':status=='COMPLETE' and len(rows)==480,
            'manifest_sha256':manifest['sha256'],'episodes':len(rows),'blocks':result['complete_blocks']},indent=2),encoding='utf-8')
    print(json.dumps(result),flush=True)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare',action='store_true')
    parser.add_argument('--execute',action='store_true')
    args=parser.parse_args()
    if args.prepare:
        if args.execute: parser.error('Freeze is offline')
        print(freeze()['sha256']);return
    result=run_all(load(),execute=args.execute)
    if result['status'] not in ('COMPLETE','PARTIAL_BUDGET'): raise SystemExit(1)


if __name__=='__main__': main()
