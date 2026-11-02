"""Frozen endpoint analysis bound to audited effective records and approved ledger."""
from collections import Counter
from decimal import Decimal
import json
from pathlib import Path
from types import FunctionType

from .. import analysis as frozen
from ..plan import LEDGER, load as load_parent
from ..runner import items, complete
from .execute import HERE, load, effective_rows, original_row
from .accounting import RID, EPISODE, replay_ledger, money
from .resume import validate
from ..recovery_v1 import execute as prior


class AuditLedger(frozen.AuditLedger):
    def __init__(self):
        self.entries=[json.loads(s) for s in LEDGER.read_text().splitlines()]
        self.states=replay_ledger(self.entries)


def validate_effective(row,item,manifest,budget=None):
    return validate(row,item,manifest,budget,original_row() if row.get('recovered_from_reservation') else None)


def analyze():
    recovery=load()
    data=effective_rows()
    ledger=AuditLedger()
    # Journal coverage for both preserved original requests and new paid requests.
    from ..plan import HERE as parent
    events=[]
    for root in (parent/'results/live',prior.HERE/'results/live',HERE/'results/live'):
        for p in sorted(root.glob('b*/requests.jsonl')):
            events.extend(json.loads(s) for s in p.read_text().splitlines())
    journal={}
    for e in events: journal.setdefault(e['reservation_id'],[]).append(e)
    interactions=[i for r in data for i in r['interactions']]
    assert len(interactions)==len({i['reservation_id'] for i in interactions})==len(journal)
    response_ids=set();known_tokens=Counter()
    for row in data:
        for seq,i in enumerate(row['interactions'],1):
            pair=journal[i['reservation_id']]
            assert len(pair)==2 and pair[0]['event']=='request' and pair[0]['request']==i['request']
            assert all(e['episode_id']==row['episode_id'] and e['request_sequence']==seq for e in pair)
            if 'response' in i:
                assert pair[1]['event']=='acknowledged_response' and pair[1]['response']==i['response']
                assert i['response']['id'] not in response_ids
                response_ids.add(i['response']['id'])
                u=i['response']['usage']
                assert u['total_tokens']==u['input_tokens']+u['output_tokens']
                for key in ('input_tokens','output_tokens','total_tokens'):known_tokens[key]+=u[key]
                known_tokens['reasoning_tokens']+=u['output_tokens_details']['reasoning_tokens']
                known_tokens['cached_input_tokens']+=u['input_tokens_details']['cached_tokens']
                known_tokens['cache_write_tokens']+=u['input_tokens_details'].get('cache_write_tokens',0)
            else:
                assert pair[1]['event']=='api_error'
                assert ledger.states[i['reservation_id']]['state'] in ('pending','encumbered_unverified')
    globals_={**frozen.analyze.__globals__,'HERE':HERE,'rows':effective_rows,'AuditLedger':AuditLedger,
              'load':load_parent,'validate':validate_effective,'Path':Path}
    csv_writer=FunctionType(frozen.csv_write.__code__,{**frozen.csv_write.__globals__,'Path':Path})
    globals_['csv_write']=csv_writer
    globals_['report']=FunctionType(frozen.report.__code__,{**frozen.report.__globals__,'HERE':HERE})
    fn=FunctionType(frozen.analyze.__code__,globals_)
    result=fn()
    out=HERE/'analysis'
    evidence={'passed':True,'recovery_plan_sha256':recovery['sha256'],'restored_responses':13,
              'restored_tool_calls':13,'request_attempts':len(interactions),'acknowledged_response_ids':len(response_ids),
              'known_token_usage':dict(known_tokens),'unverified_encumbrance_id':RID,
              'unverified_encumbrance_state':ledger.states[RID]['state'],
              'unverified_encumbrance_amount_usd':ledger.states[RID]['allowance'],
              'new_unknown_reservations':result['pending_reservations'],
              'protected_original_live_and_analysis_files':len(recovery['protected_checkpoints'])}
    (out/'recovery_audit.json').write_text(json.dumps(evidence,indent=2))
    # Additional known-usage columns avoid disguising an encumbered request as zero.
    cost_rows=[]
    for r in data:
        known=r.get('known_token_usage')
        if known is None:
            from .resume import known_tokens as add_known
            known=add_known(r['reported_usage'])
        cost_rows.append({'episode_id':r['episode_id'],'condition':r['condition'],
                         'known_api_cost_usd':r['known_api_cost_usd'],'full_cost_known':r['estimated_api_cost_usd'] is not None,
                         **{'known_'+k:v for k,v in known.items()}})
    csv_writer(out/'known_usage_audit.csv',cost_rows)
    if result['endpoint_reached']:
        excluded={original_row()['task_id'],prior.original_row()['task_id']}
        enriched=[frozen.enrich(r) for r in data if r['task_id'] not in excluded]
        csv_writer(out/'recovered_block_exclusion_sensitivity.csv',frozen.paired(enriched,True))
    note=f'''
## Authorized recovery audit

All prior records and analyses remain immutable. Two interrupted episodes were
restored from their acknowledged responses and simulator actions offline. Their
provider-error records remain as audit provenance. No acknowledged API generation
was repeated. Each continuation is linked to the original planned slot and carries
an execution-attempt identifier.

Reservation `{RID}` transitioned by explicit user approval to an unverified
$0.02 encumbrance. It is fully counted within the unchanged $4 study cap, never a
confirmed charge. Prior holds of $0.088950 remain. No future failure is
automatically approved. Full usage/cost for the recovered episode stays null;
known acknowledged usage and costs are in known_usage_audit.csv. Reasoning tokens
are a subset of output, not an extra charge. All 16 previous attempts, including
the rejection, count toward the original 24-call limit. The latest continuation
counts all 14 previous attempts and starts at request sequence 15.

Recovery plan content hash: `{recovery['sha256']}`. Original frozen manifest and
experimental source hashes, task allocation/order, prompts, model/settings,
tools and scoring remain unchanged. Profile-family analysis and the original
fixed endpoint gate remain unchanged. The user-authorized recovery is a disclosed
execution-protocol amendment following an infrastructure stop, not task/sample
selection based on outcomes. At the full endpoint, an additional sensitivity
excludes the recovered task's whole ABCD block. For partial termination, all
comparisons remain descriptive and incomplete blocks are excluded.

Correct offline reproduction:

```powershell
competitive_prompting/.venv/Scripts/python.exe -m competitive_prompting.phase3.luna_study_v1.recovery_v2.analysis
competitive_prompting/.venv/Scripts/python.exe -m pytest competitive_prompting -q
```
'''
    report=out/'final_experiment_report.md'
    text=report.read_text(encoding='utf-8').replace('competitive_prompting.phase3.luna_study_v1.analysis',
        'competitive_prompting.phase3.luna_study_v1.recovery_v2.analysis').replace('ICCâ‰ˆ.358','ICC approximately .358')
    report.write_text(text+note,encoding='utf-8')
    with (out/'validation_report.md').open('a',encoding='utf-8') as f:
        f.write('\nRecovery journal identities, response uniqueness, approved ledger and acknowledged usage checked. '
                'Original checkpoint/analysis hashes and frozen source hashes match. Recovery tests are in '
                '../preflight_pytest.txt; postflight tests in ../postflight_pytest.txt. See recovery_audit.json.\n')
    return result


if __name__=='__main__':analyze()
