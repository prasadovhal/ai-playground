"""Audit checkpoints, then continue the exact frozen schedule within existing caps."""
import argparse
import hashlib
import json
import os
from pathlib import Path

from .. import runner as frozen
from ..plan import HERE as PARENT, LEDGER, MODEL, load as load_parent
from ..budget import NAMESPACE, STUDY_CAP
from .accounting import Budget, BudgetError, RunLock, RID, EPISODE, approve, evidence_hash, replay_ledger
from .resume import restore, continue_episode, validate

HERE = Path(__file__).resolve().parent
PLAN = HERE / 'recovery_plan_v1.json'
shared = frozen.shared


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def original_row():
    old = frozen.rows(True)
    if len(old) != 8 or old[-1]['episode_id'] != EPISODE:
        raise ValueError('Original paid checkpoint changed')
    return old[-1]


def plan_base():
    m = load_parent()
    env, history, _ = restore(original_row())
    protected = {str(p.relative_to(PARENT)): digest(p)
                 for root in (PARENT/'results/live', PARENT/'analysis', PARENT/'reporting_v1')
                 for p in sorted(root.rglob('*')) if p.is_file() and '__pycache__' not in p.parts}
    return {'version': 'luna-audited-recovery-v1', 'parent_manifest_sha256': m['sha256'],
            'sources': {p.name: digest(p) for p in sorted(HERE.glob('*.py'))},
            'approval_sha256': evidence_hash(), 'protected_checkpoints': protected,
            'restored_acknowledged_responses': 15, 'restored_tool_calls': len(env.trace),
            'restored_state_sha256': shared.json_hash(env.state), 'restored_history_sha256': shared.json_hash(history),
            'remaining_model_calls': 8, 'remaining_tool_calls': 9,
            'reservation_id': RID, 'hold_usd': '0.02', 'study_cap_usd': '4.00',
            'headroom_at_approval_usd': '3.975109305',
            'policy': 'Specific explicit hold only. No prior charge/request replay, no release, no future auto-encumbrance. All 16 prior attempts count. Exact frozen request construction, remaining limits, scoring and task/condition order unchanged. New unknown charge stops.',
            'analysis': 'Preserve infrastructure history and report recovered-block sensitivity if full fixed endpoint reached. Unknown cost/token totals remain null, known usage separate. No interim p-values. Partial audit descriptive only, per original preregistration.'}


def prepare():
    m = plan_base()
    prefix = LEDGER.read_bytes()
    m['ledger_prefix'] = {'bytes': len(prefix), 'sha256': hashlib.sha256(prefix).hexdigest()}
    m['sha256'] = shared.json_hash(m)
    with PLAN.open('x', encoding='utf-8') as f:
        json.dump(m, f, indent=2)
    return m


def load():
    m = json.loads(PLAN.read_text())
    if {k:v for k,v in m.items() if k not in ('sha256','ledger_prefix')} != plan_base():
        raise ValueError('Recovery source/approval/checkpoint hash mismatch')
    if shared.json_hash({k:v for k,v in m.items() if k != 'sha256'}) != m['sha256']:
        raise ValueError('Recovery plan content hash mismatch')
    with LEDGER.open('rb') as f:
        prefix = f.read(m['ledger_prefix']['bytes'])
    if hashlib.sha256(prefix).hexdigest() != m['ledger_prefix']['sha256']:
        raise ValueError('Ledger prefix changed')
    return m


def directory(n):
    return HERE / 'results/live' / f'b{n:03d}'


def persisted():
    return [json.loads(s) for n in range(1,101) if (p:=directory(n)/'episodes.jsonl').exists()
            for s in p.read_text().splitlines()]


def effective_rows():
    newer = persisted()
    return frozen.rows(True)[:7] + (newer if newer else [original_row()])


def audit_prefix(data, manifest, recovery, budget):
    ordered = frozen.items(manifest)
    if [r['episode_id'] for r in data] != [e['episode_id'] for e in ordered[:len(data)]]:
        raise ValueError('Persisted recovery prefix is missing, duplicated or reordered')
    old = original_row()
    for row, item in zip(data, ordered):
        if not frozen.complete(row):
            raise ValueError('Previously incomplete recovery blocks retries')
        validate(row, item, manifest, budget, old if row.get('recovered_from_reservation') else None)
        if row.get('recovery_plan_sha256') not in (None, recovery['sha256']):
            raise ValueError('Recovery plan assignment changed')
    ids = [i['reservation_id'] for r in data for i in r['interactions']]
    if not any(r['episode_id'] == EPISODE for r in data):
        ids.extend(i['reservation_id'] for i in old['interactions'])
    actual = {k for k,s in replay_ledger(budget.entries).items() if s['episode_id'].startswith(NAMESPACE)}
    if len(ids) != len(set(ids)) or actual != set(ids):
        raise ValueError('Orphan or duplicate paid request; stop without replay')


def run(client_factory=shared.LiveResponses):
    recovery, m = load(), load_parent()
    gate = json.loads((HERE/'offline_validation.json').read_text())
    log = (HERE/'preflight_pytest.txt').read_text(encoding='utf-8-sig')
    if gate != {'passed':True, 'recovery_plan_sha256':recovery['sha256']} or 'passed' not in log or ' failed' in log:
        raise ValueError('Recovery offline gates not passed')
    frozen.credentials()
    if hashlib.sha256(os.environ['OPENAI_PROJECT_ID'].encode()).hexdigest() != m['project_id_sha256']:
        raise ValueError('Project changed')
    data = frozen.rows(True)[:7] + persisted()
    out = HERE/'results/live'; out.mkdir(parents=True, exist_ok=True)
    live, status, reason = None, 'COMPLETE', None
    try:
        with RunLock(LEDGER):
            budget = Budget(LEDGER,m['approval'])
            audit_prefix(data,m,recovery,budget)
            for batch in m['batches']:
                load()
                d = directory(batch['batch']); d.mkdir(parents=True,exist_ok=True)
                for block in batch['blocks']:
                    done = {r['episode_id'] for r in data}
                    missing = [e for e in block['episodes'] if e['episode_id'] not in done]
                    if not missing: continue
                    try:
                        budget.require_available(block['admission_allowance_usd'])
                    except BudgetError as exc:
                        status,reason = 'PARTIAL_BUDGET', {'block_id':block['id'],'message':str(exc),
                            'required':block['admission_allowance_usd'],'study_remaining':str(STUDY_CAP-budget.study_exposure())}
                        shared.write_result(d/'admissions.jsonl',{'admitted':False,**reason})
                        break
                    shared.write_result(d/'admissions.jsonl',{'block_id':block['id'],'admitted':True,
                        'required':block['admission_allowance_usd'],'study_exposure':str(budget.study_exposure())})
                    if live is None:
                        live = client_factory(execute=True)
                        access = live.check_access([MODEL])
                        if access[MODEL]['returned_id'] != MODEL: raise ValueError('Model substitution prohibited')
                        (out/'access.json').write_text(json.dumps({'project_sha256':m['project_id_sha256'],'models':access}))
                    for item in missing:
                        case = frozen.CASES[item['task_id']]
                        transport = frozen.Journal(live,budget,d/'requests.jsonl',item['episode_id'])
                        if item['episode_id'] == EPISODE:
                            transport.sequence = 16
                            row = continue_episode(original_row(),transport,budget,recovery['sha256'])
                        else:
                            row = frozen.run_episode(case.task,item['condition'],MODEL,0,item['seed'],transport,
                                limits=frozen.LIMITS,effort='low',pricing=m['pricing']['models'][MODEL],budget=budget,
                                dry_run=False,episode_id=item['episode_id'],run_id=frozen.VERSION)
                        row.update(family=case.family,mechanism=case.mechanism,execution_batch=batch['batch'],
                            block_id=block['id'],manifest_sha256=m['sha256'],benchmark_manifest_sha256=m['sha256'],
                            source_sha256=m['source_sha256'],recovery_plan_sha256=recovery['sha256'],independent_audit_passed=True,
                            uninspected_write_retries=sum(t.get('uninspected_retry',False) for t in row['action_trace']))
                        try:
                            validate(row,item,m,budget,original_row() if item['episode_id']==EPISODE else None)
                        except Exception as exc:
                            row.update(original_termination_reason=row['termination_reason'],termination_reason='evaluator_error',
                                verified_task_success=None,independent_audit_passed=False)
                            row['api_errors'].append({'type':'RecoveryAuditError','message':str(exc)})
                        shared.write_result(d/'episodes.jsonl',row); data.append(row)
                        print(f'Batch {batch["batch"]}: {len(data)}/2000 {item["task_id"]} {item["condition"]} {row["termination_reason"]}',flush=True)
                        if not frozen.complete(row):
                            status = 'PARTIAL_BUDGET' if row['termination_reason']=='budget_limit' else 'PARTIAL_INFRASTRUCTURE'
                            reason = {'episode_id':row['episode_id'],'reason':row['termination_reason']}
                            break
                    if status != 'COMPLETE': break
                if status != 'COMPLETE': break
                cov = [b for b in frozen.coverage(data,m) if b['batch']==batch['batch']]
                if not all(b['complete'] for b in cov): raise ValueError('Incomplete four-condition batch')
                (d/'validation.json').write_text(json.dumps({'passed':True,'episodes':20,'blocks':5,
                    'manifest_sha256':m['sha256'],'recovery_plan_sha256':recovery['sha256']}))
                print(f'BATCH {batch["batch"]} VALIDATED; study remaining ${STUDY_CAP-budget.study_exposure()}',flush=True)
    except Exception as exc:
        status = 'FAILED_VALIDATION' if isinstance(exc,(ValueError,AssertionError,KeyError,TypeError)) else 'PARTIAL_INFRASTRUCTURE'
        reason = shared.api_error_details(exc,None,0)
    finally:
        if live: live.close()
    result = {'status':status,'episodes':len(data),'planned':2000,
              'blocks':sum(b['complete'] for b in frozen.coverage(data,m)),'planned_blocks':500,
              'reason':reason,'manifest_sha256':m['sha256'],'recovery_plan_sha256':recovery['sha256'],'dry_run':False}
    (out/'execution_status.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result),flush=True)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--prepare',action='store_true'); p.add_argument('--approve-encumbrance',action='store_true')
    p.add_argument('--execute',action='store_true'); a = p.parse_args()
    if a.prepare:
        if a.execute or a.approve_encumbrance: p.error('Freeze recovery offline first')
        print(prepare()['sha256']); return
    recovery = load()
    if a.approve_encumbrance:
        changed = approve(LEDGER,approved=True)
        b = Budget(LEDGER,load_parent()['approval'])
        print(json.dumps({'transition_appended':changed,'remaining_study_usd':str(STUDY_CAP-b.study_exposure()),
                          'reported_spend_usd':str(b.reported_spend()),'encumbered_unverified_usd':str(b.encumbered_exposure())}))
    if a.execute:
        result = run()
        if result['status'] not in ('COMPLETE','PARTIAL_BUDGET'): raise SystemExit(1)


if __name__ == '__main__': main()
