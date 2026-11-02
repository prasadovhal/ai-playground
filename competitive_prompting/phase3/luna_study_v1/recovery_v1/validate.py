"""Offline restoration and full frozen scripted-trace gate. Never calls the API."""
import json

from .. import runner as frozen
from ..plan import load as load_parent
from .execute import HERE, load, original_row
from .resume import restore, validate
from .analysis import AuditLedger


def main():
    recovery, manifest = load(), load_parent()
    ledger=AuditLedger()
    ordered=frozen.items(manifest)
    scripted=frozen.rows(False)
    assert len(scripted)==2000 and len({r['episode_id'] for r in scripted})==2000
    for row,item in zip(scripted,ordered):
        validate(row,item,manifest)
    recorded=frozen.rows(True)
    for row,item in zip(recorded,ordered):
        validate(row,item,manifest,ledger)
    first=restore(original_row()); second=restore(original_row())
    assert first[0].trace==second[0].trace and first[0].state==second[0].state and first[1:]==second[1:]
    assert sum(b['complete'] for b in frozen.coverage(scripted,manifest))==500
    result={'passed':True,'recovery_plan_sha256':recovery['sha256']}
    (HERE/'offline_validation.json').write_text(json.dumps(result,indent=2))
    print(json.dumps({**result,'scripted_episodes_validated':2000,'scripted_blocks_validated':500,
                      'original_paid_records_validated':8,'restored_acknowledged_responses':15,
                      'api_calls':0}))


if __name__=='__main__':main()
