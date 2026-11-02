"""Derive a frozen eight-batch plan; all inputs are local, never API calls."""
from collections import defaultdict
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from statistics import mean

from ... import runner as shared
from ...budget import money
from ..preparation import ROOT, LIMITS, load_manifest, source_hash
from ..runner import check_development_release
from ..tasks import BY_ID

HERE = Path(__file__).resolve().parent
VERSION = 'hard-v1-balanced-v1'
MANIFEST = ROOT / 'manifest_heldout_one_rep_hard-v1.json'
PLAN = HERE / 'execution_plan_v1.json'
DEV_FILES = [ROOT / f'results/hard-v1-development-stage{s}-live/episodes.jsonl' for s in (1, 2)]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def orchestration_hash():
    h = hashlib.sha256(source_hash().encode())
    for path in sorted(HERE.glob('*.py')):
        h.update(path.name.encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def request_peak(pricing):
    # Upper bound of the EXISTING request reservation formula at frozen byte/output limits.
    return max(Decimal('.02'), ((LIMITS.request_bytes * 2 + 1024) *
        max(money(pricing['input']), money(pricing['cache_write'])) +
        LIMITS.output_tokens * money(pricing['output'])) / Decimal(1_000_000) * Decimal('1.25'))


def cost_factors():
    dev = load_manifest(ROOT / 'manifest_development_hard-v1.json')
    planned = {p['episode_id']: p for p in dev['plan']}
    factors = defaultdict(list)
    for path in DEV_FILES:
        for row in map(json.loads, path.read_text(encoding='utf-8').splitlines()):
            baseline = money(planned[row['episode_id']]['forecast']['nominal_usd'])
            price = row['pricing']
            no_cache = (row['input_tokens'] * max(money(price['input']), money(price['cache_write'])) +
                        row['output_tokens'] * money(price['output'])) / Decimal(1_000_000)
            factors[(row['model'], row['category'])].append((money(row['known_api_cost_usd']) / baseline, no_cache / baseline))
    if len(factors) != 6 or any(len(v) != 2 for v in factors.values()):
        raise ValueError('Expected two neutral calibration tasks per model/category')
    return factors


def build_plan():
    parent = load_manifest(MANIFEST)
    factors = cost_factors()
    membership = {}
    for stage in range(1, 5):
        for category in ('execution', 'recovery', 'stopping'):
            tasks = list(dict.fromkeys(p['task_id'] for p in parent['plan']
                         if p['stage'] == stage and BY_ID[p['task_id']].task.category == category))
            if len(tasks) != 2:
                raise ValueError('Frozen stage is not category-balanced')
            for rank, task in enumerate(tasks):
                membership[task] = stage * 2 - 1 + rank
    ordered = sorted(parent['plan'], key=lambda p: membership[p['task_id']])  # Stable; no new randomization.
    batches = []
    for number in range(1, 9):
        items = [p for p in ordered if membership[p['task_id']] == number]
        blocks = []
        for start in range(0, len(items), 4):
            episodes = items[start:start + 4]
            model, task = episodes[0]['model'], episodes[0]['task_id']
            if len(episodes) != 4 or {p['condition'] for p in episodes} != {'A','B','C','D'} or any(
                    (p['model'],p['task_id'],p['repetition']) != (model,task,0) for p in episodes):
                raise ValueError('Frozen four-condition block is incomplete')
            price = parent['pricing']['models'][model]
            ratios = factors[(model, BY_ID[task].task.category)]
            observed = sum((money(p['forecast']['nominal_usd']) * mean(v[0] for v in ratios) for p in episodes), Decimal(0))
            conservative = sum((money(p['forecast']['nominal_usd']) * max(v[1] for v in ratios) * Decimal('1.5') +
                p['forecast']['oracle_model_calls'] * 256 * money(price['output']) / Decimal(1_000_000)
                for p in episodes), Decimal(0))
            peak = request_peak(price)
            blocks.append({'id': f'{VERSION}:batch{number}:block{len(blocks)+1}', 'model':model, 'task_id':task,
                'episodes':episodes, 'observed_cache_forecast_usd':str(observed),
                'conservative_forecast_usd':str(conservative), 'peak_request_reservation_usd':str(peak),
                'admission_allowance_usd':str(conservative + peak)})
        if len(blocks) != 6:
            raise ValueError('Each batch must have six model/task blocks')
        batches.append({'batch':number, 'task_ids':list(dict.fromkeys(p['task_id'] for p in items)), 'blocks':blocks,
            'episodes':len(items), 'observed_cache_forecast_usd':str(sum(money(b['observed_cache_forecast_usd']) for b in blocks)),
            'conservative_forecast_usd':str(sum(money(b['conservative_forecast_usd']) for b in blocks))})
    plan = {'version':VERSION, 'parent_manifest':str(MANIFEST.relative_to(shared.ROOT.parent)).replace('\\','/'),
        'parent_manifest_sha256':parent['sha256'], 'parent_manifest_file_sha256':digest(MANIFEST),
        'parent_source_sha256':parent['source_sha256'], 'orchestration_source_sha256':orchestration_hash(),
        'episodes':192, 'batches':batches,
        'partition_rule':'Within each original stage/category, first task appearance goes to batch 2*stage-1, second to 2*stage. Stable partition preserves parent block order within each batch and original condition order. Episode objects and IDs are unchanged; parent order fields remain authoritative provenance.',
        'admission_rule':'Before EACH four-condition block: require headroom for its worst observed model/category no-discount workload ratio, 50% contingency, 256 additional output tokens per oracle request, PLUS the maximum frozen-size single-request reservation. This is an admission estimate, not a second ledger reservation or a guarantee. Every request still reserves and reconciles through the original guard. No future tasks are selected by outcomes.',
        'continuation_rule':'Only prior infrastructure audit and ledger headroom govern continuation. Legitimate agent failures do not block later conditions. No selective retries, task replacement, condition skipping or automatic run resumption. An unadmitted or interrupted block stops the run; preserve incomplete coverage.',
        'analysis_rules':{'primary':'C-B operational task success; only complete, infrastructure-evaluable A/B/C/D blocks enter paired contrasts.',
            'derived_state':'Exact independent gold-state match, regardless of final report; verification freshness recorded separately.',
            'derived_report':'Schema-valid final status equals frozen expected_status. Also record consistency with latest fresh successful tool verification; do not alter operational scorer.',
            'uncertainty':'Task bootstrap stratified by category, >=8 paired tasks. Conditional exploratory benchmark intervals; shared workflow templates do not establish independent evidence of population generalization. Zero difference is not equivalence.'},
        # The user's explicit decision supersedes the previous recommendation to postpone.
        'decision':'freeze', 'heldout_manifest_sha256':parent['sha256'],
        'reviewer':'User instruction: prepare balanced hard-v1 exploratory evaluation',
        'difficulty_review':'Neutral development: 11/12 operational successes, 12/12 correct states, one incorrect final status. User elects to proceed as exploratory despite ceilings and cost uncertainty. No paid execution approved by this preparation task.',
        'development_results':[str(p.relative_to(shared.ROOT.parent)).replace('\\','/') for p in DEV_FILES],
        'development_results_sha256':hashlib.sha256(b''.join(p.read_bytes() for p in DEV_FILES)).hexdigest(),
        'project_id_sha256':hashlib.sha256(json.loads((DEV_FILES[0].parent/'access.json').read_text(encoding='utf-8'))['project_id'].encode()).hexdigest(),
        'budget_caps_usd':{'phase3':'6.00','experiment':'9.00','historical_encumbrance':'0.048950'}}
    plan['sha256'] = shared.json_hash(plan)
    return plan


def prepare():
    plan = build_plan()
    with PLAN.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(plan, indent=2) + '\n')
    return plan


def load_plan(path=PLAN):
    plan = json.loads(Path(path).read_text(encoding='utf-8'))
    if plan != build_plan():
        raise ValueError('Execution plan/source/calibration/parent manifest hash mismatch; create a new version')
    check_development_release(path, load_manifest(MANIFEST))
    return plan
