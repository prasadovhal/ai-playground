"""Execute one predeclared 24-episode batch. Offline unless --execute is explicit."""
import argparse
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path

from ... import runner as shared, analysis as original
from ...budget import BudgetError, RunLock, money, replay_ledger, usage_cost, call_allowance
from ...prompts import messages
from ...simulator import Environment
from ..evaluation import OracleResponses
from ..preparation import ROOT, MODELS, LIMITS, load_manifest
from ..runner import Phase3Budget, audit_row, infrastructure_complete
from ..tasks import BY_ID
from .plan import PLAN, MANIFEST, VERSION, load_plan, prepare
from .analysis import write_reports


def require(value, message):
    if not value:
        raise ValueError(message)


def validate_record(row, item, parent, budget=None):
    """Replay complete infrastructure records, including legitimate agent-limit failures."""
    require(all(row[k]==item[k] for k in ('episode_id','task_id','model','condition','seed','repetition')), 'Episode identity mismatch')
    require(row['source_sha256']==parent['source_sha256'] and row['benchmark_manifest_sha256']==parent['sha256'], 'Parent source/manifest changed')
    require(row['limits']==parent['settings']['limits'] and row['reasoning_effort']=='low', 'Frozen settings changed')
    require(row['pricing']==parent['pricing']['models'][row['model']], 'Frozen pricing changed')
    case = BY_ID[row['task_id']]
    require(row['task_spec']==parent['tasks'][case.task.id]['specification']['task'], 'Task changed')
    history, env, ids, cost = messages(case.task,row['condition']), Environment(case.task), set(), Decimal(0)
    require(row['initial_messages']==history, 'Initial prompts changed')
    fields = {k:0 for k in ('input_tokens','output_tokens','total_tokens','cached_input_tokens','cache_write_tokens','reasoning_tokens')}
    final = ''
    require(len(row['interactions'])==row['model_calls'], 'Model interaction count mismatch')
    for n,interaction in enumerate(row['interactions']):
        require(interaction['request']==shared.build_request(case.task,row['condition'],row['model'],LIMITS,history=history), 'Request replay mismatch')
        response = interaction['response']
        require(response.get('service_tier') in (None,'default'), 'Processing tier changed')
        usage = response.get('usage')
        if row['dry_run']:
            require(usage is None and interaction['reservation_id'] is None, 'Fabricated offline usage/reservation')
        else:
            measured = usage_cost(usage,row['pricing'])
            require(measured is not None and measured==money(interaction['reported_cost_usd']), 'Reported usage/cost mismatch')
            require(usage['total_tokens']==usage['input_tokens']+usage['output_tokens'], 'Token total mismatch')
            details = usage.get('input_tokens_details') or {}
            require((details.get('cache_write_tokens_details') or {}).get('ttl_12h_tokens',0)==0, 'Unexpected 12h cache writes')
            values = {'input_tokens':usage['input_tokens'],'output_tokens':usage['output_tokens'],'total_tokens':usage['total_tokens'],
                'cached_input_tokens':details.get('cached_tokens'),'cache_write_tokens':details.get('cache_write_tokens'),
                'reasoning_tokens':(usage.get('output_tokens_details') or {}).get('reasoning_tokens')}
            require(values['reasoning_tokens'] is None or 0<=values['reasoning_tokens']<=usage['output_tokens'], 'Invalid reasoning usage')
            for key,value in values.items():
                fields[key] = fields[key]+value if fields[key] is not None and value is not None else None
            require(budget is not None, 'Live audit requires original ledger')
            entry = replay_ledger(budget.entries)[interaction['reservation_id']]
            require(entry['state']=='settled' and entry['episode_id']==row['episode_id'] and entry['stage']=='main', 'Reservation identity/state mismatch')
            require(money(entry['cost'])==measured and money(entry['allowance'])==money(interaction['allowance_usd'])==
                    call_allowance(interaction['request'],row['pricing']) and measured<=money(entry['allowance']), 'Reservation reconciliation mismatch')
            cost += measured
        if response.get('status')!='completed':
            require(n==len(row['interactions'])-1 and original.output_truncated(row), 'Unexplained incomplete response')
            break
        history.extend(response.get('output',[]))
        calls = [c for c in response.get('output',[]) if c.get('type')=='function_call']
        if not calls:
            require(n==len(row['interactions'])-1, 'Requests after final answer')
            final = ''.join(c.get('text','') for o in response.get('output',[]) if o.get('type')=='message'
                            for c in o.get('content',[]) if c.get('type')=='output_text')
        for call in calls:
            require(isinstance(call.get('call_id'),str) and call['call_id'] not in ids, 'Missing/duplicate call ID')
            ids.add(call['call_id'])
            if len(env.trace)>=LIMITS.tool_calls:
                require(row['termination_reason']=='tool_limit', 'Undispatched tool call')
                break
            try:
                args = json.loads(call.get('arguments',''))
            except (ValueError,TypeError):
                args = call.get('arguments')
            result = env.call(call.get('name'),args)
            env.trace[-1].update(call_id=call['call_id'],response_id=response.get('id'))
            history.append({'type':'function_call_output','call_id':call['call_id'],'output':json.dumps(result,sort_keys=True)})
    require(env.trace==row['action_trace'] and env.state==row['final_state'] and final==row['final_text'], 'Corrupted trace/state/final report')
    require(row['reported_usage']==[i['response'].get('usage') for i in row['interactions']], 'Raw usage mismatch')
    for key,value in fields.items():
        require(row[key]==(None if row['dry_run'] else value), 'Aggregated usage mismatch: '+key)
    require(money(row['known_api_cost_usd'])==cost and money(row['estimated_api_cost_usd'])==cost, 'Episode cost mismatch')
    for key,value in env.evaluate(final,row['termination_reason']).items():
        require(row[key]==value, 'Frozen evaluator metric mismatch: '+key)


def admit(budget, block):
    budget.require_available(money(block['admission_allowance_usd']))


def run_batch(plan, number, output, *, execute=False):
    batch = next((b for b in plan['batches'] if b['batch']==number),None)
    require(batch is not None,'Unknown batch')
    parent = load_manifest(MANIFEST)
    if execute:
        require(bool(os.environ.get('OPENAI_API_KEY')) and bool(os.environ.get('OPENAI_PROJECT_ID')), 'Export OPENAI_API_KEY and OPENAI_PROJECT_ID securely')
        require(hashlib.sha256(os.environ['OPENAI_PROJECT_ID'].encode()).hexdigest()==plan['project_id_sha256'], 'Project differs from neutral development')
    directory = Path(output)/f'{VERSION}-batch{number}-{"live" if execute else "offline"}'
    directory.mkdir(parents=True,exist_ok=False)
    (directory/'execution_plan.json').write_text(json.dumps(plan,indent=2),encoding='utf-8')
    (directory/'parent_manifest.json').write_bytes(MANIFEST.read_bytes())
    rows, reason = [], 'completed'
    live = None

    def episodes(budget=None):
        nonlocal live,reason
        for block in batch['blocks']:
            decision = {'block_id':block['id'],'admission_allowance_usd':block['admission_allowance_usd'],
                'dry_run':not execute,'plan_sha256':plan['sha256']}
            if budget is not None:
                decision.update(global_exposure_usd=str(budget.exposure()),phase3_exposure_usd=str(budget.phase_exposure()))
                try:
                    admit(budget,block)
                except BudgetError as exc:
                    decision.update(admitted=False,reason=str(exc))
                    shared.write_result(directory/'admissions.jsonl',decision)
                    reason='block_budget_not_admitted'
                    return
            decision['admitted']=True
            shared.write_result(directory/'admissions.jsonl',decision)
            if execute and live is None:
                live = shared.LiveResponses(execute=True)
                access = {'project_id':os.environ['OPENAI_PROJECT_ID']}
                try:
                    access['models']=live.check_access(MODELS)
                except Exception as exc:
                    access['error']=shared.api_error_details(exc,None,0)
                (directory/'access.json').write_text(json.dumps(access),encoding='utf-8')
                require('error' not in access and all(access['models'][m]['returned_id']==m for m in MODELS),'Model access failed; no substitutions')
            for item in block['episodes']:
                case=BY_ID[item['task_id']]
                row=shared.run_episode(case.task,item['condition'],item['model'],item['repetition'],item['seed'],
                    live if execute else OracleResponses(case),limits=LIMITS,effort='low',dry_run=not execute,budget=budget,
                    pricing=parent['pricing']['models'][item['model']],episode_id=item['episode_id'],run_id=f'{VERSION}-batch{number}')
                row.update(benchmark=parent['benchmark'],collection='heldout',stage=item['stage'],execution_batch=number,
                    parent_order=item['order'],block_id=block['id'],benchmark_manifest_sha256=parent['sha256'],
                    source_sha256=parent['source_sha256'],execution_plan_sha256=plan['sha256'],
                    orchestration_source_sha256=plan['orchestration_source_sha256'])
                audit_row(case,row)
                if infrastructure_complete(row):
                    try:
                        validate_record(row,item,parent,budget)
                    except (ValueError,KeyError,TypeError) as exc:
                        row.update(original_termination_reason=row['termination_reason'],termination_reason='evaluator_error',
                                   verified_task_success=None,independent_audit_passed=False)
                        row['api_errors'].append({'type':'ReplayAuditError','message':str(exc)})
                shared.write_result(directory/'episodes.jsonl',row)
                rows.append(row)
                print(f'{len(rows)}/24 {item["model"]} {item["task_id"]} {item["condition"]} {row["termination_reason"]}',flush=True)
                if not infrastructure_complete(row):
                    reason=row['termination_reason']
                    return

    def live_batches():
        ledger=shared.ROOT/'results/spend.jsonl'
        with RunLock(ledger):
            budget=Phase3Budget(ledger,'heldout')
            used={e['episode_id'] for e in budget.entries if e['event']=='reserve'}
            require(not used.intersection(e['episode_id'] for b in batch['blocks'] for e in b['episodes']), 'Batch already attempted; no selective reruns')
            for previous in plan['batches'][:number-1]:
                prior=directory.parent/f'{VERSION}-batch{previous["batch"]}-live'
                require(json.loads((prior/'validation.json').read_text(encoding='utf-8'))['passed'],'Previous batch incomplete')
                require(json.loads((prior/'execution_plan.json').read_text(encoding='utf-8'))==plan,'Execution plan changed')
                require(json.loads((prior/'access.json').read_text(encoding='utf-8'))['project_id']==os.environ['OPENAI_PROJECT_ID'],'Project changed')
                earlier=[json.loads(line) for line in (prior/'episodes.jsonl').read_text(encoding='utf-8').splitlines()]
                items=[e for b in previous['blocks'] for e in b['episodes']]
                require([r['episode_id'] for r in earlier]==[e['episode_id'] for e in items],'Previous batch missing/duplicate/reordered episodes')
                for row,item in zip(earlier,items):
                    require(infrastructure_complete(row),'Previous infrastructure failure')
                    validate_record(row,item,parent,budget)
            episodes(budget)

    try:
        live_batches() if execute else episodes()
    except Exception as exc:
        reason='infrastructure_error'
        (directory/'error.json').write_text(json.dumps(shared.api_error_details(exc,None,0)),encoding='utf-8')
    finally:
        if live is not None:
            live.close()
    try:
        coverage=write_reports(rows,plan,directory/'analysis')
    except (ValueError,KeyError,TypeError) as exc:
        reason='analysis_error'
        coverage={'blocks':[]}
        (directory/'analysis_error.json').write_text(json.dumps({'type':type(exc).__name__,'message':str(exc)}),encoding='utf-8')
    passed=reason=='completed' and len(rows)==24 and all(infrastructure_complete(r) for r in rows)
    validation={'passed':passed,'dry_run':not execute,'planned':24,'recorded':len(rows),'stop_reason':reason,
        'complete_four_condition_blocks':sum(b['paired_complete'] for b in coverage['blocks'] if b['batch']==number),
        'execution_plan_sha256':plan['sha256'],'note':'Success/failure outcomes never gate continuation; infrastructure and budget do. Missing coverage is retained.'}
    (directory/'validation.json').write_text(json.dumps(validation,indent=2)+'\n',encoding='utf-8')
    return directory


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare',action='store_true')
    parser.add_argument('--plan',type=Path,default=PLAN)
    parser.add_argument('--batch',type=int,default=1)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--output-dir',type=Path,default=ROOT/'results/balanced_v1')
    args=parser.parse_args(argv)
    if args.prepare:
        if args.execute:
            parser.error('Preparation is offline only')
        print(prepare()['sha256'])
    else:
        path=run_batch(load_plan(args.plan),args.batch,args.output_dir,execute=args.execute)
        print(path)
        if not json.loads((path/'validation.json').read_text(encoding='utf-8'))['passed']:
            raise SystemExit(1)


if __name__=='__main__':
    main()
