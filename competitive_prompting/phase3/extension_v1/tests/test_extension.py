"""Family-wide offline checks; no live transport or credentials required."""
from collections import Counter
from dataclasses import asdict
from decimal import Decimal
import json
from pathlib import Path

import pytest

from competitive_prompting import runner as shared
from competitive_prompting.simulator import Environment
from competitive_prompting.phase3.evaluation import oracle, evaluate, OracleResponses
from competitive_prompting.phase3.preparation import LIMITS
from competitive_prompting.phase3.extension_v1.tasks import EXAMPLES, BY_ID
from competitive_prompting.phase3.extension_v1.plan import validate_tasks, load, MANIFEST
from competitive_prompting.phase3.extension_v1.runner import ExtensionBudget, BudgetError, validate_record, block_coverage, check_reservations
from competitive_prompting.phase3.extension_v1.analysis import interval, paired, enrich


@pytest.mark.parametrize('example',EXAMPLES,ids=lambda e:e.case.task.id)
def test_oracle_and_independent_gold(example):
    script=oracle(example.case)
    assert script['evaluation']['verified_task_success']
    assert not script['evaluation']['uninspected_write_retries']
    assert len(script['actions'])+1<=LIMITS.model_calls
    assert len(script['actions'])<=LIMITS.tool_calls
    expected={rid:dict(zip(script['final_state'][rid]['steps'],counts)) for rid,counts in example.case.expected}
    assert {rid:r['counts'] for rid,r in script['final_state'].items()}==expected


@pytest.mark.parametrize('example',EXAMPLES,ids=lambda e:e.case.task.id)
def test_unsafe_action_and_incorrect_report_fail(example):
    script=oracle(example.case);env=Environment(example.case.task)
    for a in script['actions']: env.call(a['tool'],a['arguments'])
    record=next((r for r in example.case.task.records if r.authorized and r.available and r.requirements_met),example.case.task.records[0])
    env.call('apply_operation',{'record_id':record.id,'operation':record.steps[0]})
    env.call('verify_completion',{})
    assert not env.evaluate(script['final_text'],'completed')['verified_task_success']
    assert not evaluate(example.case,env.trace,env.state,script['final_text'],'completed')['verified_task_success']
    wrong=json.dumps({'status':'blocked' if example.case.task.expected_status=='completed' else 'completed','reason':'wrong'})
    assert not evaluate(example.case,script['trace'],script['final_state'],wrong,'completed')['verified_task_success']


@pytest.mark.parametrize('example',EXAMPLES,ids=lambda e:e.case.task.id)
def test_reset_fault_determinism_and_no_leakage(example):
    left=Environment(example.case.task);right=Environment(example.case.task)
    for a in oracle(example.case)['actions']:
        first=left.call(a['tool'],a['arguments'])
        assert right.call(a['tool'],a['arguments'])==first
        serialized=json.dumps(first)
        for key in ('hidden_failure','expected_status','fault_schedule','oracle_actions'):
            assert key not in serialized
    assert left.trace==right.trace
    fresh=Environment(example.case.task)
    assert not fresh.trace and not fresh.attempts
    assert fresh.state is not left.state
    fresh.state[next(iter(fresh.state))]['authorized']=False
    assert right.state==left.state
    request=shared.build_request(example.case.task,'A','gpt-6-luna',LIMITS)
    assert 'expected_status' not in json.dumps(request) and 'failures' not in json.dumps(request)


def test_inventory_structural_novelty():
    assert len(validate_tasks())==60
    assert Counter(e.case.task.category for e in EXAMPLES)==dict(execution=20,recovery=20,stopping=20)
    assert len({e.family for e in EXAMPLES})==30


def test_frozen_allocation_when_available():
    if not MANIFEST.exists(): pytest.skip('Freeze follows source validation')
    m=load()
    blocks=[block for batch in m['batches'] for block in batch['blocks']]
    episodes=[e for block in blocks for e in block['episodes']]
    assert len(episodes)==480 and len({e['episode_id'] for e in episodes})==480
    assert len(blocks)==120
    for batch in m['batches']:
        assert Counter(BY_ID[t].task.category for t in batch['task_ids'])==dict(execution=2,recovery=2,stopping=2)
        assert len({m['tasks'][t]['family'] for t in batch['task_ids']})==6
    for block in blocks: assert {e['condition'] for e in block['episodes']}==set('ABCD')
    assert all(v==8 for v in Counter(e['task_id'] for e in episodes).values())


def test_budget_caps_and_unknown_stop(tmp_path):
    b=ExtensionBudget(tmp_path/'ledger.jsonl')
    with pytest.raises(BudgetError): b.reserve('0.02','phase3:heldout:wrong')
    rid=b.reserve('5.99','phase3:extension_v1:one')
    b.settle(rid,'5.99')
    with pytest.raises(BudgetError): b.require_available('.02')
    other=ExtensionBudget(tmp_path/'other.jsonl')
    other.reserve('.02','phase3:extension_v1:timeout')
    with pytest.raises(BudgetError): other.require_available('.02')
    with pytest.raises(BudgetError): ExtensionBudget(tmp_path/'other.jsonl')
    with pytest.raises(ValueError): check_reservations([],other)


def test_fault_after_commit_and_before_are_observably_ambiguous():
    for kind in ('before','after'):
        e=next(e for e in EXAMPLES if any(f.kind==kind and f.tool=='apply_operation' for f in e.case.task.failures))
        script=oracle(e.case)
        event=next(t for t in script['trace'] if t['hidden_failure']==kind and t['tool']=='apply_operation')
        assert event['response']['error']=='timeout'
        assert (event['before']!=event['after'])==(kind=='after')


def test_family_bootstrap_and_pairing():
    assert interval([('t','f','execution',1)])==(None,None)
    pairs=[(str(i),str(i//2),'execution',1) for i in range(16)]
    assert interval(pairs,samples=30)==(1.0,1.0)
    rows=[];coverage=[]
    for i in range(16):
        coverage.append({'block_id':str(i),'complete':True})
        for c in 'ABCD':
            rows.append({'block_id':str(i),'task_id':str(i),'family':str(i//2),'model':'m','category':'execution',
                'condition':c,'operational_task_success':c=='C'})
    result=paired(rows,coverage)
    r=next(r for r in result if r['category']=='all' and r['comparison']=='CB' and r['metric']=='operational_task_success')
    assert r['paired_tasks']==16 and r['families']==8 and r['mean_difference']==1
    assert r['family_cluster_p']==2/256
    assert r['positive_only_successes']==16 and r['reference_only_successes']==0
    coverage[0]['complete']=False
    result=paired(rows,coverage)
    assert next(r for r in result if r['category']=='all' and r['comparison']=='CB' and r['metric']=='operational_task_success')['paired_tasks']==15


def test_scripted_serialization_and_stopping():
    case=EXAMPLES[0].case
    row=shared.run_episode(case.task,'B','gpt-6-luna',0,20261029,OracleResponses(case),
        episode_id='offline',pricing={'input':.1})
    assert row['verified_task_success'] and row['tool_calls']>0
    assert json.loads(json.dumps(row))==row
    enriched=enrich(row)
    assert enriched['verified_final_state_correct'] and enriched['final_report_status_correct'] and not enriched['premature_stop']
    row['termination_reason']='model_limit';row['verified_task_success']=False
    assert not enrich(row)['operational_task_success']
    row['termination_reason']='api_error';row['verified_task_success']=None
    assert enrich(row)['operational_task_success'] is None
