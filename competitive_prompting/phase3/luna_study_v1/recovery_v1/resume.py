"""Restore only local state; reuse the original loop with remaining attempt budget."""
from copy import deepcopy
from dataclasses import asdict
import json
from types import FunctionType

from .. import runner as frozen
from ..environment import Environment
from ..plan import LIMITS
from ...results.balanced_v1.final_analysis.recovery_v1.resume import known_tokens, TOKEN_FIELDS
from .accounting import RID, EPISODE, AMOUNT, replay_ledger, money

shared = frozen.shared


def restore(row):
    assert row['episode_id'] == EPISODE and row['termination_reason'] == 'api_error'
    assert row['model_calls'] == len(row['interactions']) == 16
    assert row['interactions'][-1]['reservation_id'] == RID and 'response' not in row['interactions'][-1]
    task = frozen.CASES[row['task_id']].task
    env, history, ids = Environment(task), shared.messages(task, row['condition']), set()
    for interaction in row['interactions']:
        assert interaction['request'] == shared.build_request(task, row['condition'], row['model'], LIMITS, history=history)
        if 'response' not in interaction:
            break
        response = interaction['response']
        assert response['status'] == 'completed'
        history.extend(deepcopy(response['output']))
        for call in response['output']:
            if call.get('type') != 'function_call':
                continue
            assert call['call_id'] not in ids
            ids.add(call['call_id'])
            result = env.call(call['name'], json.loads(call['arguments']))
            env.trace[-1].update(call_id=call['call_id'], response_id=response.get('id'))
            history.append({'type': 'function_call_output', 'call_id': call['call_id'],
                            'output': json.dumps(result, sort_keys=True)})
    assert len(env.trace) == 15 and env.trace == row['action_trace'] and env.state == row['final_state']
    return env, history, ids


def continue_episode(old, transport, budget, plan_sha):
    env, history, ids = restore(old)
    hold = replay_ledger(budget.entries)[RID]
    assert hold['state'] == 'encumbered_unverified' and money(hold['allowance']) == AMOUNT
    prior = {i['reservation_id'] for i in old['interactions']}
    assert not any(s['episode_id'] == EPISODE and rid not in prior
                   for rid, s in replay_ledger(budget.entries).items()), 'Recovery already attempted; never repeat charges'
    namespace = {**shared.run_episode.__globals__, 'Environment': lambda _: env,
                 'messages': lambda *_: deepcopy(history),
                 'set': lambda values=None: set(ids) if values is None else set(values)}
    loop = FunctionType(shared.run_episode.__code__, namespace, 'restored_luna_loop',
                        shared.run_episode.__defaults__, shared.run_episode.__closure__)
    loop.__kwdefaults__ = shared.run_episode.__kwdefaults__
    limits = shared.Limits(LIMITS.tool_calls, LIMITS.model_calls - old['model_calls'],
                           LIMITS.output_tokens, LIMITS.request_bytes)
    row = loop(frozen.CASES[old['task_id']].task, old['condition'], old['model'], old['repetition'], old['seed'],
               transport, limits=limits, effort=old['reasoning_effort'], pricing=old['pricing'], budget=budget,
               dry_run=False, run_id=old['run_id'], episode_id=EPISODE)
    new_calls = row['model_calls']
    row.update(interactions=deepcopy(old['interactions']) + row['interactions'],
               reported_usage=deepcopy(old['reported_usage']) + row['reported_usage'],
               model_calls=old['model_calls'] + new_calls, initial_messages=deepcopy(old['initial_messages']),
               limits=asdict(LIMITS), latency_seconds=old['latency_seconds'] + row['latency_seconds'],
               prior_infrastructure_errors=deepcopy(old['api_errors']), recovered_from_reservation=RID,
               recovery_plan_sha256=plan_sha, restored_acknowledged_requests=15,
               new_requests_after_resume=new_calls, estimated_api_cost_usd=None)
    row['known_api_cost_usd'] = float(money(old['known_api_cost_usd']) + money(row['known_api_cost_usd']))
    row['known_token_usage'] = known_tokens(row['reported_usage'])
    row['unknown_usage_reservations'] = [rid for rid, s in replay_ledger(budget.entries).items()
                                        if s['episode_id'] == EPISODE and s['state'] != 'settled']
    for key in TOKEN_FIELDS:
        row[key] = None
    assert row['model_calls'] <= LIMITS.model_calls and row['tool_calls'] <= LIMITS.tool_calls
    return row


def validate(row, item, manifest, budget=None, original=None):
    namespace = {**frozen.validate.__globals__, 'replay_ledger': replay_ledger,
                 'BASE_VALIDATOR': {**frozen.BASE_VALIDATOR, 'replay_ledger': replay_ledger}}
    validator = FunctionType(frozen.validate.__code__, namespace, argdefs=frozen.validate.__defaults__)
    if row.get('recovered_from_reservation') != RID:
        return validator(row, item, manifest, budget)
    assert original is not None
    restore(original)
    assert row['interactions'][:16] == original['interactions']
    assert row['model_calls'] == len(row['interactions']) <= LIMITS.model_calls
    assert row['new_requests_after_resume'] == row['model_calls'] - 16
    state = replay_ledger(budget.entries)[RID]
    assert state['state'] == 'encumbered_unverified' and money(state['allowance']) == AMOUNT
    assert row['estimated_api_cost_usd'] is None and all(row[k] is None for k in TOKEN_FIELDS)
    assert row['known_token_usage'] == known_tokens(row['reported_usage'])
    if frozen.old_analysis.eligible(row):
        view = deepcopy(row)
        view['interactions'] = view['interactions'][:15] + view['interactions'][16:]
        view['model_calls'] -= 1
        view.update(known_tokens(view['reported_usage']))
        view['estimated_api_cost_usd'] = view['known_api_cost_usd']
        return validator(view, item, manifest, budget)
    return validator(row, item, manifest, budget)
