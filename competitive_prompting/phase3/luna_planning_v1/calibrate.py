"""Conservative sufficient-N checks for the exact proposed endpoint test.

Offline hypothetical simulations, not optional looks at candidate model results.
The step search finds sufficient sizes, NOT exact mathematical minimum sizes.
"""
import csv
import argparse
import hashlib
import json
from math import ceil
from pathlib import Path

from .power import required_tasks,simulate
from .prepare import HERE,SEED,write_csv


def next_size(n):
    return ceil(n*1.10/15)*15


def calibrate(rho=None):
    observed=json.loads((HERE/'observed_pairs.json').read_text())
    q=observed['extension']['operational_task_success']['discordance_q']
    observed_rho=observed['extension']['operational_task_success']['signed_difference_icc_clipped']
    if rho is None:rho=observed_rho
    suffix='' if rho==observed_rho else '_rho010'
    previous=list(csv.DictReader((HERE/'power_simulations.csv').open(encoding='utf-8',newline='')))
    rows=[];selected=[]
    for index,(delta,target) in enumerate((d,t) for d in (.05,3/54,.075,.10) for t in (.8,.9)):
        n=required_tasks(q,delta,target,rho,5)
        for attempt in range(10):
            prior=next((r for r in previous if int(r['tasks'])==n and float(r['delta'])==delta and float(r['icc_rho'])==rho),None)
            result=({k:float(v) if k in ('estimated_power','mc_interval_low','mc_interval_high') else v for k,v in prior.items()}
                    if prior else simulate(n,q,delta,rho,5,2000,SEED+30000+index*10+attempt))
            row={'effect':delta,'target_power':target,'sufficient_tasks':n,'families':n//5,
                'icc_rho':rho,'estimated_power':float(result['estimated_power']),'mc_low':float(result['mc_interval_low']),
                'mc_high':float(result['mc_interval_high']),'simulations':int(result['simulations']),
                'selection_rule':'Increase by 10%, round to balanced 15-task units until Wilson lower MC bound >= target; not an exact minimum.',
                'selected':float(result['mc_interval_low'])>=target}
            rows.append(row)
            print(f'Exact-test sufficient-N check: delta={delta:.5f}, target={target}, N={n}, power={row["estimated_power"]:.3f}',flush=True)
            if row['selected']:
                selected.append(row);break
            n=next_size(n)
        else:raise ValueError('Sufficient-size search exhausted; no sample-size claim')
    write_csv(HERE/f'simulation_sufficient_sample_sizes{suffix}.csv',rows)
    # More precise, fresh-seed checks for the practically relevant 480/500 designs.
    precise=[]
    for i,(n,delta) in enumerate((n,d) for n in (480,500) for d in (.05,3/54)):
        result=simulate(n,q,delta,rho,5,10000,SEED+50000+i)
        precise.append(result)
        print(f'Precision check N={n}, delta={delta:.5f}: power={result["estimated_power"]:.4f}',flush=True)
    write_csv(HERE/f'power_precision_checks{suffix}.csv',precise)
    report=['# Simulation-calibrated sufficient sample sizes','',
        f'These are conservative sufficient sizes under q=7/54, signed-difference ICC={rho:.6f} and five tasks/family. They are not exact minimum N, not confidence about the true effect, and not a guarantee for new tasks. Each uses 2,000 simulation replicates with the exact planned family sign-flip test; lower 95% Monte Carlo bound must reach target.',
        '', '| Effect | Target | Sufficient tasks | Families | Estimated power | 95% MC interval |',
        '|---|---|---:|---:|---:|---|']
    for r in selected:
        report.append(f"| {100*r['effect']:.2f} pp | {r['target_power']:.0%} | {r['sufficient_tasks']} | {r['families']} | {r['estimated_power']:.3f} | [{r['mc_low']:.3f}, {r['mc_high']:.3f}] |")
    report+=['','## More precise candidate-design checks','',
        '| Tasks | Assumed effect | Power | 95% MC interval |','|---:|---:|---:|---|']
    for r in precise:
        report.append(f"| {r['tasks']} | {100*r['delta']:.2f} pp | {r['estimated_power']:.4f} | [{r['mc_interval_low']:.4f}, {r['mc_interval_high']:.4f}] |")
    report+=['','The earlier normal approximation underestimates some sufficient sizes because the exact family test is discrete/conservative. Prefer these simulation checks to an unsupported exact 80/90% claim from the formula. Positive and negative effects of the same magnitude have symmetric power under this model. Recheck if the actual family allocation, marginal discordance or analysis changes.',
        'This supplement does not freeze a task set or authorize execution. No caps changed and no API calls.']
    (HERE/f'calibrated_power_report{suffix}.md').write_text('\n'.join(report)+'\n',encoding='utf-8')
    record={'source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'base_simulations_sha256':hashlib.sha256((HERE/'power_simulations.csv').read_bytes()).hexdigest(),
        'selected_sizes':selected,'paid_calls':0}
    (HERE/f'power_calibration_audit{suffix}.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
    print('Offline sufficient-N calibration complete',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--icc',type=float,choices=(.1,),help='Optional optimistic ICC .10 sensitivity; default uses observed point estimate.')
    args=parser.parse_args();calibrate(args.icc)
