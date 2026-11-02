from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal
import json
import random

import pytest

from competitive_prompting import runner as shared
from competitive_prompting.phase3.luna_study_v1.tasks import cases,canonical_graph
from competitive_prompting.phase3.luna_study_v1.core import reference
from competitive_prompting.phase3.luna_study_v1.environment import Environment
from competitive_prompting.phase3.luna_study_v1.oracle import oracle,OracleResponses
from competitive_prompting.phase3.luna_study_v1.budget import Budget,BudgetError,NAMESPACE,replay_ledger
from competitive_prompting.phase3.luna_study_v1.runner import run_episode,Journal,complete,validate
from competitive_prompting.phase3.luna_study_v1.analysis import paired,enrich
from competitive_prompting.phase3.luna_study_v1.plan import HERE,LIMITS,MODEL

CASES=cases()
APPROVAL=json.loads((HERE/'approval.json').read_text())
PRICE={'input':.1,'cached_input':.01,'cache_write':.125,'cache_write_12h':.25,'output':.5}


@pytest.mark.parametrize('case',CASES,ids=lambda c:c.task.id)
def test_independent_oracle_and_limits(case):
    s=oracle(case);t=case.task
    assert s['evaluation']['verified_task_success']
    assert len(s['actions'])<24
    gold,path,status=reference(t.nodes,(t.initial_mask,t.initial_resources,t.initial_value))
    assert gold==(t.expected_mask,t.expected_resources,t.expected_value) and status==t.expected_status
    assert s['evaluation']['constraint_violations']==0
    assert not any(e.get('uninspected_retry') for e in s['trace'])


@pytest.mark.parametrize('case',CASES,ids=lambda c:c.task.id)
def test_unsafe_action_and_wrong_status_fail(case):
    s=oracle(case);env=Environment(case.task)
    for a in s['actions']:env.call(a['tool'],a['arguments'])
    op=next(n.name for n in case.task.nodes if env.state['counts'][n.name]>0)
    env.call('apply_operation',{'record_id':'batch','operation':op})
    env.call('verify_completion',{})
    assert not env.evaluate(s['final_text'],'completed')['verified_task_success']
    clean=Environment(case.task)
    for a in s['actions']:clean.call(a['tool'],a['arguments'])
    wrong=json.dumps({'status':'blocked' if case.task.expected_status=='completed' else 'completed','reason':'wrong'})
    assert not clean.evaluate(wrong,'completed')['verified_task_success']


@pytest.mark.parametrize('case',CASES,ids=lambda c:c.task.id)
def test_reset_determinism_and_no_ground_truth_leak(case):
    a,b=Environment(case.task),Environment(case.task)
    for action in oracle(case)['actions']:
        response=a.call(action['tool'],action['arguments'])
        assert response==b.call(action['tool'],action['arguments'])
        text=json.dumps(response)
        assert not any(word in text for word in ('expected_mask','expected_status','hidden_failure','failures','family','graph_hash'))
    assert a.trace==b.trace
    fresh=Environment(case.task)
    assert not fresh.trace and not fresh.attempts and fresh.checked is None
    fresh.state['resources'][0]=99
    assert a.state==b.state and a.state['resources'][0]!=99


def test_no_template_reuse_under_graph_isomorphism():
    assert len(CASES)==500 and len({c.graph_hash for c in CASES})==500
    assert len({c.family for c in CASES})==100
    rng=random.Random(7)
    for c in CASES:
        edges=tuple(n.requires for n in c.task.nodes)
        assert canonical_graph(edges)==c.graph_hash
        perm=list(range(7));rng.shuffle(perm);inverse={old:new for new,old in enumerate(perm)}
        transformed=tuple(tuple(inverse[p] for p in edges[old]) for old in perm)
        assert canonical_graph(transformed)==c.graph_hash
        assert max(map(len,edges))>=2 and sum(not e for e in edges)>=2
    assert sum(any(p>i for i,n in enumerate(c.task.nodes) for p in n.requires) for c in CASES)>450


def test_each_new_recovery_mechanism_is_exercised():
    seen={}
    for c in CASES:
        for e in oracle(c)['trace']:
            if e['hidden_failure']:seen.setdefault(e['hidden_failure'],e)
    assert set(seen)>={'before','after','partial','conflict'}
    assert seen['before']['before']==seen['before']['after']
    assert seen['after']['before']!=seen['after']['after']
    assert seen['partial']['after']['partial'] and seen['partial']['after']['counts'][seen['partial']['arguments']['operation']]==0
    assert seen['conflict']['after']['revision']==seen['conflict']['before']['revision']+1


def test_approved_caps_are_hard_and_cumulative(tmp_path):
    p=tmp_path/'ledger.jsonl'
    with pytest.raises(BudgetError):Budget(p,{**APPROVAL,'approved':False})
    b=Budget(p,APPROVAL)
    with pytest.raises(BudgetError):b.reserve('.02','phase3:heldout:old')
    rid=b.reserve('3.99',NAMESPACE+'first');b.settle(rid,'3.99')
    assert b.study_exposure()==Decimal('3.99')
    with pytest.raises(BudgetError):b.require_available('.02')
    again=Budget(p,APPROVAL)
    with pytest.raises(BudgetError):again.require_available('.02')
    assert replay_ledger(again.entries)==again._states


def test_unresolved_charge_blocks_and_no_auto_encumbrance(tmp_path):
    p=tmp_path/'ledger.jsonl';b=Budget(p,APPROVAL)
    b.reserve('.02',NAMESPACE+'unknown')
    assert b.study_exposure()==Decimal('.02')
    with pytest.raises(BudgetError):b.require_available('.02')
    with pytest.raises(BudgetError):Budget(p,APPROVAL)
    assert not b.encumbered()


class FakePaid:
    def __init__(self,case,fail=False):self.oracle=OracleResponses(case);self.fail=fail;self.n=0
    def create(self,**request):
        self.n+=1
        if self.fail and self.n==2:raise TimeoutError('offline synthetic timeout')
        r=self.oracle.create(**request)
        r['usage']={'input_tokens':10,'output_tokens':1,'total_tokens':11,
            'input_tokens_details':{'cached_tokens':0,'cache_write_tokens':0},'output_tokens_details':{'reasoning_tokens':0}}
        return r


def test_paid_loop_offline_journal_and_ledger_reconciliation(tmp_path):
    c=CASES[0];b=Budget(tmp_path/'ledger',APPROVAL);eid=NAMESPACE+'test'
    client=FakePaid(c);j=Journal(client,b,tmp_path/'requests.jsonl',eid)
    r=run_episode(c.task,'B',MODEL,0,20261030,j,pricing=PRICE,budget=b,dry_run=False,episode_id=eid)
    assert r['verified_task_success'] and r['reasoning_tokens']==0
    assert r['total_tokens']==r['input_tokens']+r['output_tokens']
    assert b.reported_spend()==Decimal(str(r['known_api_cost_usd']))
    assert not b.pending()
    journal=[json.loads(s) for s in (tmp_path/'requests.jsonl').read_text().splitlines()]
    assert len(journal)==r['model_calls']*2
    assert [e['event'] for e in journal][:2]==['request','acknowledged_response']
    assert len({e['reservation_id'] for e in journal})==r['model_calls']


def test_unknown_api_failure_keeps_full_exposure(tmp_path):
    c=CASES[0];b=Budget(tmp_path/'ledger',APPROVAL);eid=NAMESPACE+'error'
    j=Journal(FakePaid(c,True),b,tmp_path/'requests.jsonl',eid)
    r=run_episode(c.task,'C',MODEL,0,20261030,j,pricing=PRICE,budget=b,dry_run=False,episode_id=eid)
    assert r['termination_reason']=='api_error' and r['verified_task_success'] is None
    assert r['estimated_api_cost_usd'] is None and r['input_tokens'] is None
    assert len(b.pending())==1 and b.study_exposure()==b.reported_spend()+Decimal('.02')
    with pytest.raises(BudgetError):b.reserve('.02',NAMESPACE+'next')


def test_no_interim_inference_and_family_exact_endpoint():
    data=[]
    for i in range(40):
        for c in 'ABCD':
            data.append({'task_id':str(i),'family':str(i//5),'category':'execution','condition':c,
                'mechanism':'dependency_DAG','independent_audit_passed':True,'termination_reason':'completed',
                'verified_task_success':c=='C','valid_final_report':True,'operational_task_success':c=='C','model_calls':1})
    partial=paired(data,False)
    assert all(r['p_value'] is None and r['ci_low'] is None for r in partial)
    final=paired(data,True)
    r=next(r for r in final if r['category']=='all' and r['contrast']=='CB' and r['metric']=='operational_task_success')
    assert r['families']==8 and r['paired_tasks']==40 and r['p_value']==2/256
