from copy import deepcopy
from decimal import Decimal
import json

import pytest

from competitive_prompting.phase3.luna_study_v1 import runner as frozen
from competitive_prompting.phase3.luna_study_v1.plan import LEDGER, load
from competitive_prompting.phase3.luna_study_v1.recovery_v1.accounting import (
    Budget, BudgetError, RID, EPISODE, approve, replay_ledger)
from competitive_prompting.phase3.luna_study_v1.recovery_v1.execute import original_row, audit_prefix
from competitive_prompting.phase3.luna_study_v1.recovery_v1.resume import restore, continue_episode, validate


@pytest.fixture
def ledger(tmp_path):
    old = original_row()
    ids = {i['reservation_id'] for i in old['interactions']}
    entries = [json.loads(s) for s in LEDGER.read_text().splitlines()]
    # Real recorded fixture, isolated; the production ledger is never mutated.
    entries = [e for e in entries if e['id'] in ids and e['event'] in ('reserve','settle')]
    p = tmp_path/'spend.jsonl'
    p.write_text(''.join(json.dumps(e)+'\n' for e in entries))
    return p


def test_explicit_hold_is_idempotent_and_not_a_settlement(ledger):
    with pytest.raises(BudgetError): approve(ledger)
    before = ledger.read_bytes()
    assert approve(ledger,approved=True)
    assert not approve(ledger,approved=True)
    assert ledger.read_bytes().startswith(before)
    b = Budget(ledger,load()['approval'])
    assert not b.pending() and replay_ledger(b.entries)[RID]['state']=='encumbered_unverified'
    assert b.study_exposure()==b.reported_spend()+Decimal('.02')
    assert b.reported_spend()==Decimal(str(original_row()['known_api_cost_usd']))
    assert not [e for e in b.entries if e['event']=='settle' and e['id']==RID]
    with pytest.raises(BudgetError): b.settle(RID,0)
    assert b.encumbered_exposure()==Decimal('.02')


def test_additional_unknown_reservation_still_blocks(ledger):
    approve(ledger,approved=True); b=Budget(ledger,load()['approval'])
    b.reserve('.02',frozen.NAMESPACE+'additional-unknown')
    with pytest.raises(BudgetError): b.require_available('.02')
    with pytest.raises(BudgetError): Budget(ledger,load()['approval'])


def test_full_four_dollar_exposure_still_enforced(ledger):
    approve(ledger,approved=True); b=Budget(ledger,load()['approval'])
    allowance=Decimal('4')-b.study_exposure()
    r=b.reserve(allowance,frozen.NAMESPACE+'to-cap'); b.settle(r,allowance)
    assert b.study_exposure()==Decimal(4)
    with pytest.raises(BudgetError): b.reserve('.000000001',frozen.NAMESPACE+'excess')
    entries=deepcopy(b.entries)
    entry=next(e for e in entries if e['event']=='encumbered_unverified' and e['id']==RID)
    with pytest.raises(BudgetError): replay_ledger(entries+[entry])


def test_offline_restore_is_identical_and_keeps_agent_errors():
    old=original_row(); a,h,ids=restore(old); b,h2,ids2=restore(old)
    assert a.state==b.state==old['final_state'] and h==h2 and ids==ids2 and len(ids)==15
    assert a.trace==old['action_trace'] and a.state['counts']['op4']==2
    task=frozen.CASES[old['task_id']].task
    assert frozen.shared.build_request(task,'C',old['model'],frozen.LIMITS,history=h)==old['interactions'][-1]['request']


class FinalResponse:
    def __init__(self,old): self.calls=[]; self.request=old['interactions'][-1]['request']
    def create(self,**request):
        assert request==self.request  # No prompt edits or acknowledged prefix API requests.
        self.calls.append(request)
        return {'id':'offline-recovered-final','status':'completed','usage':{
            'input_tokens':10,'output_tokens':10,'total_tokens':20,
            'input_tokens_details':{'cached_tokens':0,'cache_write_tokens':0},
            'output_tokens_details':{'reasoning_tokens':2}},
            'output':[{'type':'message','role':'assistant','content':[{'type':'output_text',
                'text':json.dumps({'status':'blocked','reason':'Prior duplicate prevents completion.'})}]}]}


def test_real_checkpoint_continuation_is_one_new_request_and_auditable(ledger,tmp_path):
    approve(ledger,approved=True); m=load(); b=Budget(ledger,m['approval']); old=original_row()
    client=FinalResponse(old); journal=frozen.Journal(client,b,tmp_path/'requests',EPISODE)
    journal.sequence=16
    row=continue_episode(old,journal,b,'test-plan')
    for key in ('family','mechanism','execution_batch','block_id','manifest_sha256','benchmark_manifest_sha256',
                'source_sha256','independent_audit_passed','uninspected_write_retries'):
        row[key]=old[key]
    assert len(client.calls)==1 and row['model_calls']==17 and row['tool_calls']==15
    assert row['interactions'][:16]==old['interactions'] and row['prior_infrastructure_errors']==old['api_errors']
    assert row['termination_reason']=='completed' and row['verified_task_success'] is False
    assert row['estimated_api_cost_usd'] is None and row['total_tokens'] is None
    assert row['known_token_usage']['total_tokens']==row['known_token_usage']['input_tokens']+row['known_token_usage']['output_tokens']
    assert replay_ledger(b.entries)[RID]['state']=='encumbered_unverified'
    item=next(i for i in frozen.items(m) if i['episode_id']==EPISODE)
    validate(row,item,m,b,old)
    before=ledger.read_bytes()
    with pytest.raises(AssertionError,match='already attempted'): continue_episode(old,journal,b,'test-plan')
    assert ledger.read_bytes()==before and len(client.calls)==1
    events=[json.loads(s) for s in (tmp_path/'requests').read_text().splitlines()]
    assert [e['request_sequence'] for e in events]==[17,17]


def test_second_unknown_failure_is_retained_and_never_approved(ledger,tmp_path):
    class Failure:
        def create(self,**request): raise TimeoutError('offline new unknown charge')
    approve(ledger,approved=True); m=load(); b=Budget(ledger,m['approval']); old=original_row()
    j=frozen.Journal(Failure(),b,tmp_path/'requests',EPISODE); j.sequence=16
    row=continue_episode(old,j,b,'test-plan')
    assert row['termination_reason']=='api_error' and len(b.pending())==1
    assert b.study_exposure()==b.reported_spend()+Decimal('.04')
    with pytest.raises(BudgetError): b.require_available('.02')


def test_orphan_and_duplicate_prefix_requests_block(ledger):
    entries=[json.loads(s) for s in LEDGER.read_text().splitlines()]
    ids={i['reservation_id'] for r in frozen.rows(True) for i in r['interactions']}
    ledger.write_text(''.join(json.dumps(e)+'\n' for e in entries
                             if e['id'] in ids and e['event'] in ('reserve','settle')))
    approve(ledger,approved=True); m=load(); b=Budget(ledger,m['approval'])
    audit_prefix(frozen.rows(True)[:7],m,{'sha256':'test'},b)
    b.reserve('.02',frozen.NAMESPACE+'orphan')
    with pytest.raises(ValueError,match='Orphan'): audit_prefix(frozen.rows(True)[:7],m,{'sha256':'test'},b)
    duplicate=[frozen.rows(True)[0]]*2
    with pytest.raises(ValueError,match='prefix'): audit_prefix(duplicate,m,{'sha256':'test'},b)


def test_partial_analysis_end_to_end_offline(tmp_path,monkeypatch):
    from competitive_prompting.phase3.luna_study_v1.recovery_v1 import analysis
    source=LEDGER.read_bytes()
    entries=[json.loads(s) for s in source.decode().splitlines()]
    original_ids={i['reservation_id'] for r in frozen.rows(True) for i in r['interactions']}
    later_ids={e['id'] for e in entries if e['event']=='reserve' and
               e['episode_id'].startswith(frozen.NAMESPACE) and e['id'] not in original_ids}
    ledger=tmp_path/'copy.jsonl'
    ledger.write_text(''.join(json.dumps(e)+'\n' for e in entries if e['id'] not in later_ids))
    approve(ledger,approved=True)
    class ReadOnly(analysis.AuditLedger):
        def __init__(self):
            self.entries=[json.loads(s) for s in ledger.read_text().splitlines()]
            self.states=replay_ledger(self.entries)
    monkeypatch.setattr(analysis,'AuditLedger',ReadOnly)
    monkeypatch.setattr(analysis,'HERE',tmp_path)
    monkeypatch.setattr(analysis,'effective_rows',lambda:frozen.rows(True))
    monkeypatch.setattr(analysis,'load',lambda:{'sha256':'offline-fixture','protected_checkpoints':{}})
    out=tmp_path/'results/live'; out.mkdir(parents=True)
    from competitive_prompting.phase3.luna_study_v1.plan import HERE as parent
    (out/'execution_status.json').write_bytes((parent/'results/live/execution_status.json').read_bytes())
    result=analysis.analyze()
    assert not result['endpoint_reached'] and result['complete_blocks']==1
    assert (tmp_path/'analysis/final_results.csv').exists()
    assert (tmp_path/'analysis/known_usage_audit.csv').exists()
    assert 'no primary inferential' in (tmp_path/'analysis/final_experiment_report.md').read_text()
    audit=json.loads((tmp_path/'analysis/recovery_audit.json').read_text())
    assert audit['request_attempts']==91 and audit['acknowledged_response_ids']==90
    assert LEDGER.read_bytes()==source
