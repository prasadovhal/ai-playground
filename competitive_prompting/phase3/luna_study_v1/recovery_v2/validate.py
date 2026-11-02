"""Validate the full paid prefix and accounting on an isolated ledger copy."""
import json
import tempfile
from pathlib import Path
from decimal import Decimal

from .. import runner as frozen
from ..plan import LEDGER, load as load_parent
from .accounting import Budget, RID, approve, replay_ledger
from .execute import HERE, load, persisted, audit_prefix, original_row
from .resume import restore


def main():
    recovery, manifest = load(), load_parent()
    data=frozen.rows(True)[:7]+persisted()
    assert len(data)==1572 and all(frozen.complete(r) for r in data)
    coverage=frozen.coverage(data,manifest)
    assert len(coverage)==500 and sum(b['complete'] for b in coverage)==393
    assert len(coverage)-sum(b['complete'] for b in coverage)==107
    assert 2000-len(data)==428 and not frozen.complete(original_row())
    a,h,ids=restore(original_row());b,h2,ids2=restore(original_row())
    assert a.state==b.state and a.trace==b.trace and h==h2 and ids==ids2 and len(ids)==13
    before=LEDGER.read_bytes()
    with tempfile.TemporaryDirectory() as directory:
        p=Path(directory)/'spend.jsonl';p.write_bytes(before)
        assert approve(p,approved=True) and not approve(p,approved=True)
        budget=Budget(p,manifest['approval'])
        assert replay_ledger(budget.entries)[RID]['state']=='encumbered_unverified'
        assert budget.study_exposure()==Decimal('1.164197410')
        assert Decimal('4')-budget.study_exposure()==Decimal('2.835802590')
        assert not budget.pending()
        audit_prefix(data,manifest,recovery,budget)
    assert LEDGER.read_bytes()==before
    gate={'passed':True,'recovery_plan_sha256':recovery['sha256']}
    (HERE/'offline_validation.json').write_text(json.dumps(gate,indent=2))
    print(json.dumps({**gate,'complete_episodes':len(data),'complete_blocks':393,'unfinished_blocks':107,
                      'valid_episodes_needed':428,'restored_responses':13,'paid_calls':0}))


if __name__=='__main__':main()
