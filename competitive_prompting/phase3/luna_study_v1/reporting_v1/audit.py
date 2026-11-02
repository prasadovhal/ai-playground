"""Read-only postflight journal reconciliation; never submits or retries requests."""
from collections import Counter
from decimal import Decimal
import hashlib
import json

from ..plan import HERE, LEDGER, load
from ..runner import rows, items, validate
from ..budget import NAMESPACE
from ...extension_v1.analysis import AuditLedger


def audit():
    manifest = load()  # Frozen sources, all prior files, old ledger byte prefix.
    ledger = AuditLedger()
    records = rows()
    planned = {e['episode_id']: e for e in items(manifest)}
    events = []
    for path in sorted((HERE / 'results/live').glob('b*/requests.jsonl')):
        events.extend(json.loads(s) for s in path.read_text().splitlines())
    by_reservation = {}
    for event in events:
        by_reservation.setdefault(event['reservation_id'], []).append(event)
    interactions = [i for r in records for i in r['interactions']]
    assert len(interactions) == len(by_reservation)
    assert len({i['reservation_id'] for i in interactions}) == len(interactions)
    own = {k: s for k, s in ledger.states.items() if s['episode_id'].startswith(NAMESPACE)}
    assert set(own) == set(by_reservation)
    response_ids = set()
    totals = Counter()
    known = Decimal(0)
    for record in records:
        validate(record, planned[record['episode_id']], manifest, ledger)
        for seq, interaction in enumerate(record['interactions'], 1):
            rid = interaction['reservation_id']
            pair = by_reservation[rid]
            assert len(pair) == 2 and pair[0]['event'] == 'request'
            assert pair[0]['request'] == interaction['request']
            assert all(e['episode_id'] == record['episode_id'] and
                       e['request_sequence'] == seq for e in pair)
            assert own[rid]['episode_id'] == record['episode_id']
            response = interaction.get('response')
            if response is None:
                assert pair[1]['event'] == 'api_error' and own[rid]['state'] == 'pending'
                totals['requests_with_unknown_usage'] += 1
                continue
            assert pair[1]['event'] == 'acknowledged_response'
            assert pair[1]['response'] == response and own[rid]['state'] == 'settled'
            assert response['id'] not in response_ids
            response_ids.add(response['id'])
            usage = response['usage']
            assert usage['total_tokens'] == usage['input_tokens'] + usage['output_tokens']
            assert 0 <= usage['output_tokens_details']['reasoning_tokens'] <= usage['output_tokens']
            totals['known_input_tokens'] += usage['input_tokens']
            totals['known_output_tokens'] += usage['output_tokens']
            totals['known_reasoning_tokens'] += usage['output_tokens_details']['reasoning_tokens']
            totals['known_cached_input_tokens'] += usage['input_tokens_details']['cached_tokens']
            totals['known_cache_write_tokens'] += usage['input_tokens_details'].get('cache_write_tokens', 0)
            known += Decimal(own[rid]['cost'])
    result = {
        'passed': True, 'paid_requests_attempted': len(interactions),
        'acknowledged_responses': len(response_ids), 'journal_events': len(events),
        **totals, 'confirmed_usage_priced_new_cost_usd': str(known),
        'ledger_entries': len(ledger.entries),
        'ledger_sha256': hashlib.sha256(LEDGER.read_bytes()).hexdigest(),
        'historical_prefix_and_protected_files_intact': True,
        'protected_files': len(manifest['protected_files']),
        'manifest_content_sha256': manifest['sha256'],
        'manifest_file_sha256': hashlib.sha256((HERE / 'manifest_v1.json').read_bytes()).hexdigest(),
        'reporting_sources_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                     for p in sorted((HERE / 'reporting_v1').glob('*.py'))},
        'pending': {k: s for k, s in own.items() if s['state'] == 'pending'},
        'no_paid_calls_during_postflight': True,
    }
    path = HERE / 'analysis/postflight_audit.json'
    path.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))
    return result


if __name__ == '__main__':
    audit()
