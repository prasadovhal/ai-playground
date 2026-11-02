"""Separate state/report diagnostics; the original scorer and denominators are unchanged."""
import argparse
from collections import defaultdict
from dataclasses import asdict
import json
from pathlib import Path
from statistics import mean

from ... import analysis as original
from ..analysis import CONTRASTS, METRICS as OLD_METRICS, task_interval
from ..evaluation import evaluate
from ..runner import infrastructure_complete
from ..tasks import BY_ID
from ..preparation import LIMITS
from .plan import PLAN, load_plan

DERIVED = ('verified_final_state_correct', 'final_report_status_correct', 'verification_current',
           'report_consistent_with_verification')
METRICS = DERIVED + OLD_METRICS


def derive(row):
    case = BY_ID[row['task_id']]
    score = evaluate(case, row['action_trace'], row['final_state'], row['final_text'], row['termination_reason'])
    report = json.loads(row['final_text']) if score['valid_final_report'] else {}
    verifications = [t for t in row['action_trace'] if t['tool']=='verify_completion' and t['response'].get('ok')]
    last = verifications[-1] if verifications else None
    current = bool(last and last['after']==row['final_state'] and last['response'].get('records')==row['final_state'])
    consistent = (bool(score['valid_final_report'] and report['status']==
                  ('completed' if last['response']['complete'] else 'blocked')) if current else None)
    return {**original.analysis_metrics(row), 'verified_final_state_correct':score['state_matches_gold'],
        'final_report_status_correct':bool(score['valid_final_report'] and report['status']==case.task.expected_status),
        'verification_current':current, 'report_consistent_with_verification':consistent}


def coverage(rows, plan):
    expected = {e['episode_id']:e for b in plan['batches'] for block in b['blocks'] for e in block['episodes']}
    recorded = {}
    for row in rows:
        rid = row['episode_id']
        if rid in recorded:
            raise ValueError('Duplicate episode in balanced analysis')
        if rid not in expected:
            raise ValueError('Episode is not in the frozen execution plan')
        item = expected[rid]
        if any(row[k]!=item[k] for k in ('task_id','model','condition','seed','repetition')) or (
                row['benchmark_manifest_sha256']!=plan['parent_manifest_sha256'] or
                row['source_sha256']!=plan['parent_source_sha256'] or row['execution_plan_sha256']!=plan['sha256'] or
                row['orchestration_source_sha256']!=plan['orchestration_source_sha256'] or
                row['limits']!=asdict(LIMITS) or row['reasoning_effort']!='low'):
            raise ValueError('Episode identity/configuration hash mismatch')
        recorded[rid] = row
    if len({r['dry_run'] for r in rows})>1:
        raise ValueError('Cannot mix offline and live records')
    blocks, complete = [], []
    for batch in plan['batches']:
        for block in batch['blocks']:
            present = [recorded[e['episode_id']] for e in block['episodes'] if e['episode_id'] in recorded]
            missing = [e['condition'] for e in block['episodes'] if e['episode_id'] not in recorded]
            infrastructure = [r['condition'] for r in present if not infrastructure_complete(r)]
            valid = not missing and not infrastructure
            blocks.append({'batch':batch['batch'],'block_id':block['id'],'model':block['model'],
                'task_id':block['task_id'],'missing_conditions':missing,'infrastructure_conditions':infrastructure,
                'paired_complete':valid})
            if valid:
                complete.append(present)
    return {'planned':192,'recorded':len(rows),'missing':192-len(rows),'dry_run':rows[0]['dry_run'] if rows else None,
            'note':'Scripted oracle data is infrastructure validation, not competitive-model evidence.' if rows and rows[0]['dry_run'] else
                   'Exploratory finite-benchmark coverage; incomplete blocks are excluded from paired contrasts.',
            'complete_blocks':len(complete),'planned_blocks':48,'blocks':blocks}, complete


def paired(complete):
    differences = defaultdict(dict)
    for block in complete:
        conditions = {r['condition']:derive(r) for r in block}
        example = block[0]
        for contrast, weights in CONTRASTS.items():
            for metric in METRICS:
                if any(conditions[c].get(metric) is None for c in weights):
                    continue
                delta = sum(conditions[c][metric]*w for c,w in weights.items())
                for category in (example['category'],'all'):
                    differences[(example['model'],category,contrast,metric)][example['task_id']] = delta
    results = []
    for (model,category,contrast,metric), values in sorted(differences.items()):
        low, high = task_interval(values)
        results.append({'model':model,'category':category,'comparison':contrast,'metric':metric,
            'paired_tasks':len(values),'mean_task_paired_difference':mean(values.values()),'ci_low':low,'ci_high':high,
            'interval_basis':'Exploratory task bootstrap within category, conditional on these benchmark tasks; shared templates limit independence/generalization. Zero difference is not equivalence.'})
    return results


def write_reports(rows, plan, directory):
    status, complete = coverage(rows, plan)
    augmented = [derive(r) for r in rows]
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    summaries = original.summarize(augmented, 20261027)
    for summary in summaries:
        group = [r for r in augmented if original.eligible(r) and r['model']==summary['model'] and
                 r['condition']==summary['condition'] and summary['category'] in (r['category'],'all')]
        for metric in DERIVED + ('incorrect_retries','duplicate_operations','uninspected_write_retries'):
            values = [r[metric] for r in group if r.get(metric) is not None]
            summary['mean_'+metric] = mean(values) if values else None
            summary['denominator_'+metric] = len(values)
    original.write_csv(directory/'summary.csv', summaries)
    original.write_csv(directory/'paired.csv', paired(complete))
    original.write_csv(directory/'task_summary.csv', original.summarize(augmented,20261027,by_task=True))
    original.write_csv(directory/'episodes.csv', [{k:r.get(k) for k in ('episode_id','task_id','category','model','condition',
        'termination_reason') + METRICS} for r in augmented])
    (directory/'coverage.json').write_text(json.dumps(status,indent=2)+'\n',encoding='utf-8')
    return status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('results',nargs='+',type=Path)
    parser.add_argument('--plan',type=Path,default=PLAN)
    parser.add_argument('--output-dir',type=Path,required=True)
    args = parser.parse_args(argv)
    rows = [json.loads(line) for p in args.results for line in p.read_text(encoding='utf-8').splitlines()]
    print(json.dumps(write_reports(rows,load_plan(args.plan),args.output_dir),indent=2))


if __name__=='__main__':
    main()
