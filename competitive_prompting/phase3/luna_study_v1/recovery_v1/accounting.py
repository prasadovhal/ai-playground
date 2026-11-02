"""One specifically authorized additional hold; all prior transitions unchanged."""
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
from types import FunctionType

from .. import budget as frozen
from .... import budget as original

RID = '091f3198f854487a967146c812c33571'
EPISODE = 'phase3:luna-dag-v1:luna1_execution_f061_v2:0:C'
REQUEST_ID = 'req_0508192ec9ef4f4c83f0b0dc101a48eb'
AMOUNT = Decimal('.02')
HERE = Path(__file__).resolve().parent
money, BudgetError, RunLock = frozen.money, frozen.BudgetError, frozen.RunLock


def replay_ledger(entries):
    filtered = []
    approved = False
    for entry in entries:
        if entry.get('event') == 'encumbered_unverified' and entry.get('id') == RID:
            state = frozen.replay_ledger(filtered).get(RID)
            if (approved or not state or state['state'] != 'pending' or state['stage'] != 'main'
                    or state['episode_id'] != EPISODE or entry.get('episode_id') != EPISODE
                    or entry.get('stage') != 'main' or money(state['allowance']) != AMOUNT
                    or money(entry['allowance']) != AMOUNT or entry.get('request_id') != REQUEST_ID
                    or entry.get('approval') != 'explicit_user_flag' or not entry.get('approved_at_utc')
                    or entry.get('approval_evidence_sha256') != evidence_hash()):
                raise BudgetError('Invalid/repeated authorized Luna encumbrance')
            approved = True
            continue
        if approved and entry.get('event') == 'settle' and entry.get('id') == RID:
            if not isinstance(entry.get('external_evidence'), str) or not entry['external_evidence'].strip():
                raise BudgetError('External billing evidence required; unknown charge cannot be invented')
        filtered.append(entry)
    states = frozen.replay_ledger(filtered)
    if approved and states[RID]['state'] == 'pending':
        states[RID] = {**states[RID], 'state': 'encumbered_unverified', 'request_id': REQUEST_ID}
    return states


def evidence_hash():
    return hashlib.sha256((HERE / 'approval.json').read_bytes()).hexdigest()


def rebound(fn):
    clone = FunctionType(fn.__code__, {**fn.__globals__, 'replay_ledger': replay_ledger},
                         fn.__name__, fn.__defaults__, fn.__closure__)
    clone.__kwdefaults__ = fn.__kwdefaults__
    return clone


class Budget(frozen.Budget):
    # Rebind only decoder-dependent methods. The inherited caps, request
    # reservation, pending guard and approved namespace remain byte-identical.
    __init__ = rebound(frozen.Budget.__init__)
    _append = rebound(frozen.Budget._append)
    reported_spend = rebound(original.Budget.reported_spend)

    def settle(self, reservation, cost, **kwargs):
        if not self._states[reservation]['episode_id'].startswith(frozen.NAMESPACE):
            raise BudgetError('Cannot settle historical study reservations')
        return rebound(original.Budget.settle)(self, reservation, cost, **kwargs)


def approve(path, *, approved=False):
    if not approved:
        raise BudgetError('Explicit approval flag is required')
    authorization = json.loads((HERE / 'approval.json').read_text())
    if (authorization.get('approved') is not True or authorization['reservation_id'] != RID
            or authorization['amount_usd'] != '0.02' or authorization['study_cap_usd'] != '4.00'):
        raise BudgetError('Approval scope mismatch')
    path = Path(path)
    with RunLock(path):
        entries = [json.loads(s) for s in path.read_text().splitlines()]
        states = replay_ledger(entries)
        state = states.get(RID)
        if state and state['state'] == 'encumbered_unverified':
            return False
        if not state or state['state'] != 'pending':
            raise BudgetError('Specific pending reservation is missing or already settled')
        entry = {'event': 'encumbered_unverified', 'id': RID, 'stage': 'main',
                 'episode_id': EPISODE, 'allowance': str(AMOUNT), 'request_id': REQUEST_ID,
                 'approval': 'explicit_user_flag', 'approval_evidence_sha256': evidence_hash(),
                 'approved_at_utc': datetime.now(timezone.utc).isoformat()}
        after = replay_ledger([*entries, entry])
        def exposure(ss):
            return sum((money(s['cost'] if s['state'] == 'settled' else s['allowance'])
                        for s in ss.values()), Decimal(0))
        if exposure(states) != exposure(after):
            raise BudgetError('Approval must not release or double-count exposure')
        with path.open('a', encoding='utf-8') as f:
            f.write(json.dumps(entry) + '\n'); f.flush(); os.fsync(f.fileno())
    return True
