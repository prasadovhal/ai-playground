"""Explicitly authorized cap version; historical accounting policy is unchanged."""
from decimal import Decimal
import json
from pathlib import Path
import os

from ..results.balanced_v1.final_analysis.recovery_v1.accounting import (
    private,replay_ledger,BudgetError,RunLock,money)

HERE=Path(__file__).resolve().parent
NAMESPACE='phase3:luna-dag-v1:'
STUDY_CAP=Decimal('4.00')
PHASE_CAP=Decimal('9.363684130')
GLOBAL_CAP=Decimal('9.518315430')


class Budget(private.Budget):
    def __init__(self,path,approval):
        if (approval.get('approved') is not True or approval.get('new_study_cap_usd')!='4.00'
                or money(approval['phase3_cap_usd'])!=PHASE_CAP or money(approval['experiment_cap_usd'])!=GLOBAL_CAP
                or not approval.get('user_authorization')):
            raise BudgetError('Explicit cap-version authorization required')
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.stage='main';self.total_cap=GLOBAL_CAP;self.pilot_cap=Decimal('.75')
        self.entries=[json.loads(s) for s in self.path.read_text(encoding='utf-8').splitlines()] if self.path.exists() else []
        self._states=replay_ledger(self.entries)
        self.start_exposure=self.exposure();self.run_ceiling=GLOBAL_CAP
        if self.pending():raise BudgetError('Unknown prior charge blocks execution; no automatic encumbrance')
        self.require_available(0)

    def subset_exposure(self,prefix):
        return sum((money(s['cost'] if s['state']=='settled' else s['allowance'])
            for s in self._states.values() if s['episode_id'].startswith(prefix)),Decimal(0))

    def exposure(self,stage=None):
        return sum((money(s['cost'] if s['state']=='settled' else s['allowance']) for s in self._states.values()
                    if stage is None or s['stage']==stage),Decimal(0))

    def pending(self):return {k:v for k,v in self._states.items() if v['state']=='pending'}
    def encumbered(self):return {k:v for k,v in self._states.items() if v['state']=='encumbered_unverified'}

    def _append(self,entry):
        # Full original decoder validation on EVERY append; cached reads merely
        # avoid repeatedly decoding the same immutable prefix within one request.
        states=replay_ledger([*self.entries,entry])
        with self.path.open('a',encoding='utf-8') as f:
            f.write(json.dumps(entry,allow_nan=False)+'\n');f.flush();os.fsync(f.fileno())
        self.entries.append(entry);self._states=states

    def phase_exposure(self):return self.subset_exposure('phase3:')
    def study_exposure(self):return self.subset_exposure(NAMESPACE)

    def require_available(self,allowance):
        # All settled costs plus active/unknown holds, once each; never sum old
        # reconciled allowances a second time. Original pending/global checks reused.
        super().require_available(allowance)
        a=money(allowance)
        if self.phase_exposure()+a>PHASE_CAP:raise BudgetError('Authorized cumulative Phase 3 cap would be exceeded')
        if self.study_exposure()+a>STUDY_CAP:raise BudgetError('New-study $4 exposure cap would be exceeded')

    def reserve(self,allowance,episode_id):
        if not episode_id.startswith(NAMESPACE):raise BudgetError('This cap version authorizes only new Luna study IDs')
        return super().reserve(allowance,episode_id)

    def settle(self,reservation,cost,**kwargs):
        if not replay_ledger(self.entries)[reservation]['episode_id'].startswith(NAMESPACE):
            raise BudgetError('Historical reservations cannot be reconciled by this study')
        return super().settle(reservation,cost,**kwargs)
