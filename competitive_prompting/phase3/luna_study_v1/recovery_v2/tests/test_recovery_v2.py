import json
from decimal import Decimal
import pytest

from competitive_prompting.phase3.luna_study_v1 import runner as frozen
from competitive_prompting.phase3.luna_study_v1.plan import LEDGER, load
from competitive_prompting.phase3.luna_study_v1.recovery_v2.accounting import Budget, BudgetError, RID, EPISODE, approve, replay_ledger
from competitive_prompting.phase3.luna_study_v1.recovery_v2.execute import original_row
from competitive_prompting.phase3.luna_study_v1.recovery_v2.resume import restore, continue_episode, validate


@pytest.fixture
def ledger(tmp_path):
    ids={i['reservation_id'] for i in original_row()['interactions']}
    entries=[json.loads(s) for s in LEDGER.read_text().splitlines()]
    p=tmp_path/'ledger.jsonl'
    p.write_text(''.join(json.dumps(e)+'\n' for e in entries if e['id'] in ids and e['event'] in ('reserve','settle')))
    return p


def test_exact_encumbrance_and_future_pending_block(ledger):
    before=ledger.read_bytes()
    with pytest.raises(BudgetError): approve(ledger)
    assert approve(ledger,approved=True)
    assert not approve(ledger,approved=True)
    assert ledger.read_bytes().startswith(before)
    b=Budget(ledger,load()['approval'])
    assert replay_ledger(b.entries)[RID]['state']=='encumbered_unverified'
    assert b.study_exposure()==b.reported_spend()+Decimal('.02')
    with pytest.raises(BudgetError): b.settle(RID,0)
    b.reserve('.02',frozen.NAMESPACE+'other')
    with pytest.raises(BudgetError): b.require_available('.02')
    with pytest.raises(BudgetError): Budget(ledger,load()['approval'])


def test_restore_real_checkpoint_and_mock_continuation(ledger,tmp_path):
    old=original_row(); a,h,ids=restore(old); b,h2,ids2=restore(old)
    assert a.state==b.state==old['final_state'] and a.trace==old['action_trace'] and h==h2 and ids==ids2
    assert len(ids)==13 and len(a.trace)==13
    approve(ledger,approved=True); manifest=load(); budget=Budget(ledger,manifest['approval'])
    class Response:
        def __init__(self): self.calls=[]
        def create(self,**request):
            assert request==old['interactions'][-1]['request']
            self.calls.append(request)
            return {'id':'offline-v2-final','status':'completed','usage':{'input_tokens':10,'output_tokens':10,'total_tokens':20,
                'input_tokens_details':{'cached_tokens':0,'cache_write_tokens':0},'output_tokens_details':{'reasoning_tokens':2}},
                'output':[{'type':'message','role':'assistant','content':[{'type':'output_text',
                'text':json.dumps({'status':'blocked','reason':'Offline validation.'})}]}]}
    client=Response(); journal=frozen.Journal(client,budget,tmp_path/'journal',EPISODE); journal.sequence=14
    row=continue_episode(old,journal,budget,'test-plan')
    for k in ('family','mechanism','execution_batch','block_id','manifest_sha256','benchmark_manifest_sha256',
              'source_sha256','independent_audit_passed','uninspected_write_retries'):row[k]=old[k]
    assert row['execution_attempt_id']==EPISODE+':recovery-v2' and row['planned_episode_id']==EPISODE
    assert row['interactions'][:14]==old['interactions'] and row['model_calls']==15 and len(client.calls)==1
    assert row['estimated_api_cost_usd'] is None and row['total_tokens'] is None
    item=next(e for e in frozen.items(manifest) if e['episode_id']==EPISODE)
    validate(row,item,manifest,budget,old)
    with pytest.raises(AssertionError,match='already attempted'):continue_episode(old,journal,budget,'test-plan')
    events=[json.loads(s) for s in (tmp_path/'journal').read_text().splitlines()]
    assert [e['request_sequence'] for e in events]==[15,15]


def test_new_unknown_failure_stops(ledger,tmp_path):
    class Failure:
        def create(self,**request):raise TimeoutError('offline synthetic failure')
    approve(ledger,approved=True); m=load(); b=Budget(ledger,m['approval'])
    j=frozen.Journal(Failure(),b,tmp_path/'journal',EPISODE);j.sequence=14
    row=continue_episode(original_row(),j,b,'test-plan')
    assert row['termination_reason']=='api_error' and len(b.pending())==1
    assert b.study_exposure()==b.reported_spend()+Decimal('.04')
    with pytest.raises(BudgetError):b.require_available('.02')
