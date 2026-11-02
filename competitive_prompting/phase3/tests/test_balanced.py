from copy import deepcopy
from decimal import Decimal
import hashlib
import json

import pytest

from competitive_prompting import runner as shared, analysis as original
from competitive_prompting.budget import BudgetError, RunLock, call_allowance, money
from competitive_prompting.phase3.preparation import ROOT, LIMITS, load_manifest, oracle_requests
from competitive_prompting.phase3.evaluation import OracleResponses
from competitive_prompting.phase3.tasks import BY_ID
from competitive_prompting.phase3.runner import Phase3Budget, audit_row, infrastructure_complete
from competitive_prompting.phase3.balanced import plan as planning, runner, analysis
from competitive_prompting.tests.test_experiment import historical_ledger


@pytest.fixture(scope='module')
def plan():
    return planning.build_plan()


@pytest.fixture(scope='module')
def parent():
    return load_manifest(planning.MANIFEST)


def row_for(item, plan, transport=None):
    case=BY_ID[item['task_id']]
    parent_prices=json.loads(planning.MANIFEST.read_text(encoding='utf-8'))['pricing']['models']
    row=shared.run_episode(case.task,item['condition'],item['model'],item['repetition'],item['seed'],
        transport or OracleResponses(case),limits=LIMITS,pricing=parent_prices[item['model']],episode_id=item['episode_id'])
    row.update(benchmark_manifest_sha256=plan['parent_manifest_sha256'],source_sha256=plan['parent_source_sha256'],
               execution_plan_sha256=plan['sha256'],orchestration_source_sha256=plan['orchestration_source_sha256'],collection='heldout')
    return audit_row(case,row)


def test_eight_batches_exact_parent_identities_order_balance(plan,parent):
    assert plan['episodes']==192 and len(plan['batches'])==8
    flattened=[]
    for batch in plan['batches']:
        assert len(batch['task_ids'])==3 and len(batch['blocks'])==6 and batch['episodes']==24
        assert {BY_ID[t].task.category for t in batch['task_ids']}=={'execution','recovery','stopping'}
        for block in batch['blocks']:
            assert len(block['episodes'])==4 and {e['condition'] for e in block['episodes']}==set('ABCD')
            original_block=[p for p in parent['plan'] if (p['task_id'],p['model'])==(block['task_id'],block['model'])]
            assert block['episodes']==original_block
            flattened+=block['episodes']
    assert {p['episode_id'] for p in flattened}=={p['episode_id'] for p in parent['plan']}
    assert len({p['episode_id'] for p in flattened})==192
    assert plan['parent_manifest_sha256']==parent['sha256']
    assert plan['parent_source_sha256']==parent['source_sha256']


def test_plan_write_once_hash_and_tamper_rejection(plan,tmp_path,monkeypatch):
    path=tmp_path/'execution.json'
    monkeypatch.setattr(planning,'PLAN',path)
    assert planning.prepare()==plan
    with pytest.raises(FileExistsError):
        planning.prepare()
    assert planning.load_plan(path)==plan
    bad=deepcopy(plan)
    bad['batches'][0]['blocks'][0]['episodes'][0]['condition']='X'
    path.write_text(json.dumps(bad),encoding='utf-8')
    with pytest.raises(ValueError,match='hash mismatch'):
        planning.load_plan(path)
    path.write_text(json.dumps(plan),encoding='utf-8')
    monkeypatch.setattr(planning,'orchestration_hash',lambda:'changed')
    with pytest.raises(ValueError,match='hash mismatch'):
        planning.load_plan(path)


def test_block_admission_does_not_reserve_twice_and_cumulative_cap_blocks(plan,tmp_path):
    ledger=historical_ledger(tmp_path,approved=True)
    budget=Phase3Budget(ledger,'heldout')
    block=plan['batches'][0]['blocks'][0]
    before=ledger.read_bytes()
    runner.admit(budget,block)
    assert ledger.read_bytes()==before
    assert budget.encumbered_exposure()==Decimal('.048950')
    cost=Decimal('6')-money(block['admission_allowance_usd'])+Decimal('.000001')
    rid=budget.reserve(cost,'phase3:heldout:earlier-version:test')
    budget.settle(rid,cost)
    before=ledger.read_bytes()
    with pytest.raises(BudgetError):
        runner.admit(budget,block)
    assert ledger.read_bytes()==before
    assert Decimal('6')-budget.phase_exposure()>Decimal('.02')  # A single cheap call could fit, a block cannot.


def test_global_cap_and_unknown_request_still_block(plan,tmp_path):
    ledger=historical_ledger(tmp_path,approved=True)
    budget=Phase3Budget(ledger,'heldout')
    # Prior non-Phase3 spend also counts against the global cap.
    from competitive_prompting.budget import Budget
    prior=Budget(ledger,stage='main')
    rid=prior.reserve(8.8,'prior-non-phase3')
    prior.settle(rid,8.8)
    budget=Phase3Budget(ledger,'heldout')
    block=next(b for b in plan['batches'][0]['blocks'] if b['model']=='gpt-6.1-sol')
    with pytest.raises(BudgetError):
        runner.admit(budget,block)
    budget.reserve('.02','phase3:heldout:unknown')
    with pytest.raises(BudgetError,match='Unsettled'):
        Phase3Budget(ledger,'heldout')


def test_peak_reservation_bounds_oracle_requests(plan,parent):
    for batch in plan['batches']:
        for block in batch['blocks']:
            assert money(block['admission_allowance_usd'])==money(block['conservative_forecast_usd'])+money(block['peak_request_reservation_usd'])
            for request in oracle_requests(BY_ID[block['task_id']],'C',block['model']):
                assert call_allowance(request,parent['pricing']['models'][block['model']])<=money(block['peak_request_reservation_usd'])


@pytest.fixture
def live_temp(plan,tmp_path,monkeypatch):
    ledger=historical_ledger(tmp_path,approved=True)
    real=Phase3Budget
    monkeypatch.setattr(runner,'Phase3Budget',lambda _,c:real(ledger,c))
    monkeypatch.setattr(runner,'RunLock',lambda _:RunLock(ledger))
    project=json.loads((planning.DEV_FILES[0].parent/'access.json').read_text(encoding='utf-8'))['project_id']
    monkeypatch.setenv('OPENAI_API_KEY','offline-placeholder')
    monkeypatch.setenv('OPENAI_PROJECT_ID',project)
    return ledger


def test_unadmitted_block_starts_no_client_or_episode(plan,tmp_path,live_temp,monkeypatch):
    budget=Phase3Budget(live_temp,'heldout')
    rid=budget.reserve('5.99','phase3:heldout:earlier')
    budget.settle(rid,'5.99')
    class Forbidden:
        def __init__(self,**_): pytest.fail('No client before block admission')
    monkeypatch.setattr(shared,'LiveResponses',Forbidden)
    before=live_temp.read_bytes()
    path=runner.run_batch(plan,1,tmp_path,execute=True)
    result=json.loads((path/'validation.json').read_text())
    assert not result['passed'] and result['recorded']==0 and result['stop_reason']=='block_budget_not_admitted'
    assert live_temp.read_bytes()==before
    assert json.loads((path/'admissions.jsonl').read_text())['admitted'] is False


def test_new_unknown_api_charge_stops_and_remains_pending(plan,tmp_path,live_temp,monkeypatch):
    class Broken:
        calls=0
        def __init__(self,**_): pass
        def check_access(self,models): return {m:{'requested_model':m,'returned_id':m} for m in models}
        def create(self,**_):
            Broken.calls+=1
            raise RuntimeError('mock network interruption')
        def close(self): pass
    monkeypatch.setattr(shared,'LiveResponses',Broken)
    path=runner.run_batch(plan,1,tmp_path,execute=True)
    assert Broken.calls==1
    validation=json.loads((path/'validation.json').read_text())
    assert not validation['passed'] and validation['recorded']==1 and validation['stop_reason']=='api_error'
    with pytest.raises(BudgetError,match='Unsettled'):
        Phase3Budget(live_temp,'heldout')
    coverage=json.loads((path/'analysis/coverage.json').read_text())
    assert coverage['complete_blocks']==0 and coverage['missing']==191


def test_default_batch_and_continuation_do_not_depend_on_success(plan,tmp_path,monkeypatch):
    class WrongReport(OracleResponses):
        def create(self,**request):
            response=super().create(**request)
            if response['output'][0]['type']=='message':
                text=json.loads(self.script['final_text'])
                text['status']='blocked' if text['status']=='completed' else 'completed'
                response['output'][0]['content'][0]['text']=json.dumps(text)
            return response
    monkeypatch.setattr(runner,'OracleResponses',WrongReport)
    class Forbidden:
        def __init__(self,**_): pytest.fail('Offline default constructed live client')
    monkeypatch.setattr(shared,'LiveResponses',Forbidden)
    path=runner.run_batch(plan,1,tmp_path)
    rows=[json.loads(l) for l in (path/'episodes.jsonl').read_text().splitlines()]
    assert len(rows)==24 and all(not r['verified_task_success'] for r in rows)
    assert json.loads((path/'validation.json').read_text())['passed']
    assert json.loads((path/'analysis/coverage.json').read_text())['complete_blocks']==6


@pytest.mark.parametrize('kind',['wrong_status','invalid_report','missing_verification'])
def test_state_and_report_metrics_are_separate(plan,kind):
    item=next(e for b in plan['batches'][0]['blocks'] if BY_ID[b['task_id']].task.category=='execution' for e in b['episodes'])
    row=row_for(item,plan)
    if kind=='wrong_status': row['final_text']='{"status":"blocked","reason":"wrong"}'
    if kind=='invalid_report': row['final_text']='not json'
    if kind=='missing_verification': row['action_trace']=row['action_trace'][:-1]
    result=analysis.derive(row)  # Derivation does not rewrite saved scoring fields.
    assert result['verified_final_state_correct']
    if kind=='missing_verification':
        assert result['final_report_status_correct'] and not result['verification_current']
        assert result['report_consistent_with_verification'] is None
    else:
        assert not result['final_report_status_correct'] and not result['report_consistent_with_verification']


def test_blocked_expected_status_is_correct(plan):
    item=next(e for b in plan['batches'][0]['blocks'] if BY_ID[b['task_id']].task.category=='stopping' for e in b['episodes'])
    row=row_for(item,plan)
    result=analysis.derive(row)
    assert result['verified_final_state_correct'] and result['final_report_status_correct']
    assert result['report_consistent_with_verification'] and result['operational_task_success']


def test_incomplete_four_condition_blocks_are_never_paired(plan):
    block=plan['batches'][0]['blocks'][0]
    rows=[row_for(e,plan) for e in block['episodes']]
    status,complete=analysis.coverage(rows,plan)
    assert status['complete_blocks']==1 and status['missing']==188
    primary=next(r for r in analysis.paired(complete) if r['category']=='all' and r['comparison']=='primary_C_minus_B' and r['metric']=='verified_final_state_correct')
    assert primary['paired_tasks']==1 and primary['mean_task_paired_difference']==0
    assert primary['ci_low'] is None
    fewer=[r for r in rows if r['condition']!='D']
    status,complete=analysis.coverage(fewer,plan)
    assert status['complete_blocks']==0 and not analysis.paired(complete)  # B and C alone do not qualify.
    with pytest.raises(ValueError,match='Duplicate'):
        analysis.coverage(rows+[rows[0]],plan)
    rows[0]['termination_reason']='api_error'
    assert analysis.coverage(rows,plan)[0]['complete_blocks']==0


def test_truncation_and_agent_limits_remain_operational_failures(plan,parent):
    item=plan['batches'][0]['blocks'][0]['episodes'][0]
    class Truncated:
        def create(self,**_): return {'status':'incomplete','output':[],'usage':None,'incomplete_details':{'reason':'max_output_tokens'}}
    row=row_for(item,plan,Truncated())
    assert original.eligible(row) and not original.analysis_metrics(row)['operational_task_success']
    assert infrastructure_complete(row)
    runner.validate_record(row,item,parent)
    class Reads:
        def __init__(self): self.i=0
        def create(self,**_):
            self.i+=1
            return {'id':str(self.i),'status':'completed','usage':None,'output':[
                {'type':'function_call','call_id':str(self.i),'name':'read_records','arguments':'{}'}]}
    row=row_for(item,plan,Reads())
    assert row['termination_reason']=='model_limit' and original.eligible(row) and infrastructure_complete(row)
    runner.validate_record(row,item,parent)


def test_corrupt_request_or_usage_is_rejected(plan,parent):
    item=plan['batches'][0]['blocks'][0]['episodes'][0]
    row=row_for(item,plan)
    runner.validate_record(row,item,parent)
    bad=deepcopy(row)
    bad['interactions'][0]['request']['max_output_tokens']=999
    with pytest.raises(ValueError,match='Request replay'):
        runner.validate_record(bad,item,parent)
    bad=deepcopy(row)
    bad['output_tokens']=5
    with pytest.raises(ValueError,match='Aggregated usage'):
        runner.validate_record(bad,item,parent)


def test_corrupt_trace_stops_with_raw_record_and_validation(plan,tmp_path,monkeypatch):
    real=shared.run_episode
    def corrupted(*args,**kwargs):
        row=real(*args,**kwargs)
        first=next(iter(row['final_state'].values()))
        first['counts'][first['steps'][0]]=99
        return row
    monkeypatch.setattr(shared,'run_episode',corrupted)
    path=runner.run_batch(plan,1,tmp_path)
    validation=json.loads((path/'validation.json').read_text())
    assert not validation['passed'] and validation['recorded']==1
    assert (path/'episodes.jsonl').exists() and (path/'analysis_error.json').exists()


def test_incomplete_prior_batch_blocks_before_client(plan,tmp_path,live_temp,monkeypatch):
    import os
    prior=tmp_path/f'{planning.VERSION}-batch1-live'
    prior.mkdir()
    (prior/'validation.json').write_text(json.dumps({'passed':True}))
    (prior/'execution_plan.json').write_text(json.dumps(plan))
    (prior/'access.json').write_text(json.dumps({'project_id':os.environ['OPENAI_PROJECT_ID']}))
    (prior/'episodes.jsonl').write_text('')
    class Forbidden:
        def __init__(self,**_): pytest.fail('Prior coverage gate must run before client')
    monkeypatch.setattr(shared,'LiveResponses',Forbidden)
    path=runner.run_batch(plan,2,tmp_path,execute=True)
    result=json.loads((path/'validation.json').read_text())
    assert not result['passed'] and result['recorded']==0
    assert result['stop_reason']=='infrastructure_error'


def test_mock_live_batch_reconciles_all_requests_and_old_encumbrance(plan,tmp_path,live_temp,monkeypatch):
    items=[e for b in plan['batches'][0]['blocks'] for e in b['episodes']]
    class MockLive:
        def __init__(self,**_):
            self.index=0
            self.scripts=[OracleResponses(BY_ID[e['task_id']]) for e in items]
        def check_access(self,models): return {m:{'requested_model':m,'returned_id':m} for m in models}
        def create(self,**request):
            result=self.scripts[self.index].create(**request)
            if result['output'][0]['type']=='message': self.index+=1
            result['usage']={'input_tokens':10,'output_tokens':5,'total_tokens':15,
                'input_tokens_details':{'cached_tokens':0,'cache_write_tokens':0},
                'output_tokens_details':{'reasoning_tokens':2}}
            return result
        def close(self): pass
    monkeypatch.setattr(shared,'LiveResponses',MockLive)
    path=runner.run_batch(plan,1,tmp_path,execute=True)
    validation=json.loads((path/'validation.json').read_text())
    assert validation['passed'] and validation['complete_four_condition_blocks']==6
    budget=Phase3Budget(live_temp,'heldout')
    assert not budget.pending() and budget.encumbered_exposure()==Decimal('.048950')
    rows=[json.loads(l) for l in (path/'episodes.jsonl').read_text(encoding='utf-8').splitlines()]
    assert len(rows)==24 and sum(r['known_api_cost_usd'] for r in rows)==pytest.approx(float(budget.phase_exposure()))
    assert all(r['total_tokens']==r['input_tokens']+r['output_tokens'] for r in rows)
    assert all(r['reasoning_tokens']<=r['output_tokens'] for r in rows)


def test_target_status_match_does_not_hide_wrong_final_state(plan):
    item=next(e for b in plan['batches'][0]['blocks'] if BY_ID[b['task_id']].task.category=='execution' for e in b['episodes'])
    class Early:
        def create(self,**_): return {'status':'completed','usage':None,'output':[{'type':'message','content':[
            {'type':'output_text','text':'{"status":"completed","reason":"premature"}'}]}]}
    row=row_for(item,plan,Early())
    result=analysis.derive(row)
    assert result['final_report_status_correct'] and not result['verified_final_state_correct']
    assert not result['operational_task_success'] and result['report_consistent_with_verification'] is None


def test_recorded_development_status_failure_is_not_hidden():
    rows=[json.loads(line) for p in planning.DEV_FILES for line in p.read_text(encoding='utf-8').splitlines()]
    failed=next(r for r in rows if r['model']=='gpt-6-luna' and r['task_id']=='p3_dev_recovery_invoice_reconcile')
    result=analysis.derive(failed)
    assert result['verified_final_state_correct'] and result['verification_current']
    assert not result['final_report_status_correct'] and not result['report_consistent_with_verification']
    assert result['operational_task_success'] is False


@pytest.mark.parametrize('change',['plan','source','settings','mode'])
def test_analysis_refuses_incompatible_records(plan,change):
    items=plan['batches'][0]['blocks'][0]['episodes']
    rows=[row_for(e,plan) for e in items]
    if change=='plan': rows[0]['execution_plan_sha256']='other'
    if change=='source': rows[0]['orchestration_source_sha256']='other'
    if change=='settings': rows[0]['limits']['model_calls']=999
    if change=='mode': rows[0]['dry_run']=False
    with pytest.raises(ValueError):
        analysis.coverage(rows,plan)
