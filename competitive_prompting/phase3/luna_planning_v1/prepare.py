"""Read-only evidence review and prospective power/budget feasibility. No API calls.

This intentionally cannot manufacture a frozen 500-task study by renaming the
existing linear-workflow template. The candidate protocol is not executable.
"""
import argparse
from collections import Counter,defaultdict
import csv
from dataclasses import asdict
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
from math import sqrt
import os
from pathlib import Path
import random
from statistics import mean

from ... import runner as shared
from ...budget import call_allowance,usage_cost
from ...prompts import CONDITIONS,SYSTEM,FINAL_FORMAT
from ...simulator import TOOLS
from ..preparation import LIMITS
from ..extension_v1.analysis import AuditLedger
from ..extension_v1.plan import load as load_extension
from ..extension_v1.runner import all_rows
from ..results.balanced_v1.final_analysis.recovery_v1.execute import effective_rows
from .power import required_tasks,approximate_power,icc_moments,simulate

HERE=Path(__file__).resolve().parent
SEED=20261029
MODEL='gpt-6-luna'
CAP=Decimal('2.50')
REPORTED_BALANCE=Decimal('4.91')


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def protected_files():
    result={}
    for directory,dirs,names in os.walk(shared.ROOT):
        dirs[:]=[d for d in dirs if d not in ('.venv','__pycache__','.pytest_cache','luna_planning_v1')]
        for name in names:
            p=Path(directory)/name
            if p.suffix in ('.pyc','.lock'):continue
            result[str(p.relative_to(shared.ROOT)).replace('\\','/')]=digest(p)
    return result


def write_csv(path,rows):
    with Path(path).open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,list(rows[0]));w.writeheader();w.writerows(rows)


def observed_pairs(rows,metric):
    selected=[r for r in rows if r['model']==MODEL]
    lookup={(r['task_id'],r['condition']):r for r in selected}
    result=[]
    for c in selected:
        if c['condition']!='C':continue
        b=lookup[(c['task_id'],'B')]
        # Saved analysis CSVs are used for operational/final-state diagnostics;
        # no saved scoring or research results are changed.
        convert=lambda v: int(v is True or v=='True')
        result.append({'task_id':c['task_id'],'family':c.get('family',c['category']),
            'category':c['category'],'C':convert(c[metric]),'B':convert(b[metric]),
            'difference':convert(c[metric])-convert(b[metric])})
    return result


def summarize_pairs(pairs):
    groups=defaultdict(list)
    for r in pairs:groups[r['family']].append(r['difference'])
    values=[r['difference'] for r in pairs]
    rho=icc_moments(list(groups.values()))
    # Family-resampled uncertainty for q and delta, stratified by category.
    strata=defaultdict(dict)
    for r in pairs:strata[r['category']].setdefault(r['family'],[]).append(r['difference'])
    rng=random.Random(SEED);deltas=[];qs=[]
    for _ in range(10000):
        sample=[]
        for families in strata.values():
            for key in rng.choices(list(families),k=len(families)):sample.extend(families[key])
        deltas.append(mean(sample));qs.append(mean(abs(v) for v in sample))
    deltas.sort();qs.sort()
    enough_clusters=len(groups)>=8 and all(len(g)>=2 for g in strata.values())
    return {'paired_tasks':len(pairs),'families':len(groups),'wins_C':values.count(1),
        'wins_B':values.count(-1),'ties':values.count(0),'delta':mean(values),
        'discordance_q':mean(abs(v) for v in values),'delta_cluster_ci':[deltas[250],deltas[9750]] if enough_clusters else None,
        'q_cluster_ci':[qs[250],qs[9750]] if enough_clusters else None,'signed_difference_icc_raw':rho,
        'signed_difference_icc_clipped':max(0,min(1,rho)) if rho is not None else None,
        'icc_warning':'Few pairs and unequal two/singleton families; ICC is uncertain. Negative/zero does not prove independence; a positive point estimate is not known future ICC. Intervals omitted with <8 families or fewer than 2 per stratum.'}


def cost_forecast(old_rows,new_rows):
    studies={}
    calls=[];uncertain_ids=[]
    for name,rows in (('original_24_tasks',old_rows),('extension_observed_54_tasks',new_rows)):
        rr=[r for r in rows if r['model']==MODEL]
        costs=sum((sum((Decimal(i['reported_cost_usd']) for i in r['interactions'] if i.get('reported_cost_usd') is not None),Decimal(0)) for r in rr),Decimal(0))
        requests=[i for r in rr for i in r['interactions'] if i.get('reported_cost_usd') is not None]
        complete=[r for r in rr if r['estimated_api_cost_usd'] is not None]
        complete_cost=sum((sum((Decimal(i['reported_cost_usd']) for i in r['interactions']),Decimal(0)) for r in complete),Decimal(0))
        uncertain_ids.extend(r['episode_id'] for r in rr if r['estimated_api_cost_usd'] is None)
        for r in rr:
            for i in r['interactions']:
                if i.get('reported_cost_usd') is None:continue
                actual=usage_cost(i['response']['usage'],r['pricing'])
                if actual!=Decimal(i['reported_cost_usd']):raise ValueError('Historical request usage/cost disagreement')
                calls.append(actual)
        studies[name]={'episodes':len(rr),'known_spend':str(costs),'requests_with_reported_usage':len(requests),
            'complete_cost_episodes':len(complete),'complete_cost_spend':str(complete_cost),
            'unknown_cost_episodes':len(rr)-len(complete),
            'mean_episode_cost':str(complete_cost/len(complete)),'mean_reported_request_cost':str(costs/len(requests)),
            'mean_reported_requests_per_episode':len(requests)/len(rr)}
    known=sum((Decimal(x['known_spend']) for x in studies.values()),Decimal(0))
    episodes=sum(x['episodes'] for x in studies.values())
    complete_n=sum(x['complete_cost_episodes'] for x in studies.values())
    mean_episode=sum((Decimal(x['complete_cost_spend']) for x in studies.values()),Decimal(0))/complete_n
    holds=sum((Decimal(s['allowance']) for s in AuditLedger().states.values()
               if s['episode_id'] in uncertain_ids and s['state']!='settled'),Decimal(0))
    stress_unit=(known+holds)/episodes
    calls.sort()
    # Frozen Luna maximum per-call reservation is $0.02 under original limits/rates.
    price=json.loads((shared.ROOT/'pricing.json').read_text())['models'][MODEL]
    peak=max(Decimal('.02'),((LIMITS.request_bytes*2+1024)*max(Decimal(str(price['input'])),Decimal(str(price['cache_write'])))+
        LIMITS.output_tokens*Decimal(str(price['output'])))/Decimal(1000000)*Decimal('1.25'))
    table=[]
    for n in (420,450,480,489,490,495,500,510):
        for name,unit in [('pooled_complete_cost_episodes',mean_episode),
                ('known_plus_uncertain_allowance_stress_not_confirmed_charge',stress_unit)]+[(k,Decimal(v['mean_episode_cost'])) for k,v in studies.items()]:
            table.append({'tasks':n,'episodes':n*4,'basis':name,'mean_episode_usd':str(unit),
                'point_cost_usd':str(unit*n*4),'with_50pct_contingency_and_peak_usd':str(unit*n*4*Decimal('1.5')+peak),
                'fits_2_50_forecast':unit*n*4*Decimal('1.5')+peak<=CAP,
                'note':'Workload-conditional forecast, not a bound. Task-aware oracle/request forecast remains required; reservations are reconciled, not summed as charges.'})
    return {'historical_studies':studies,'pooled_mean_episode_cost_usd':str(mean_episode),
        'reported_request_cost_p95_usd':str(calls[int(.95*(len(calls)-1))]),
        'reported_request_cost_max_usd':str(max(calls)),'peak_single_request_reservation_usd':str(peak),
        'episode_count':episodes,'known_request_count':len(calls),
        'complete_cost_episode_count':complete_n,'unknown_cost_episode_ids_excluded_from_unit_mean':uncertain_ids,
        'unknown_usage_allowance_for_forecast_stress_only_usd':str(holds),
        'stress_mean_not_confirmed_charge_usd':str(stress_unit),
        'maximum_tasks_under_pooled_mean_50pct_plus_peak':int((CAP-peak)/(4*mean_episode*Decimal('1.5'))),
        'caution':'A larger/more diverse simulator may change cost and failure rates. This is preliminary and cannot replace task-aware forecasts or the hard $2.50 new-study exposure ceiling.'},table


def prepare(simulations=2000):
    prior=load_extension();preserved=protected_files();ledger=AuditLedger()
    pending=[rid for rid,s in ledger.states.items() if s['state']=='pending']
    if pending:raise ValueError('Blocking unresolved reservation; no proposed paid execution')
    new_rows=all_rows();old_rows,_=effective_rows()
    with (shared.ROOT/'phase3/extension_v1/analysis/final_results.csv').open(encoding='utf-8',newline='') as f:csv_new=list(csv.DictReader(f))
    with (shared.ROOT/'phase3/results/balanced_v1/final_analysis/final_results.csv').open(encoding='utf-8',newline='') as f:csv_old=list(csv.DictReader(f))
    observed={}
    for dataset,rows in (('extension',csv_new),('original',csv_old)):
        observed[dataset]={metric:summarize_pairs(observed_pairs(rows,metric)) for metric in
            ('operational_task_success','verified_final_state_correct')}
    q=observed['extension']['operational_task_success']['discordance_q']
    observed_rho=observed['extension']['operational_task_success']['signed_difference_icc_clipped']
    effects=(.02,.03,.05,3/54,.075,.10)
    sample_sizes=[]
    for discordance in (.05,q,.25,.35):
        for size in (1,2,5,10):
            for rho in (0,.1,.3,observed_rho,.5,.8):
                if size==1 and rho!=0:continue
                for delta in effects:
                    if delta>discordance:continue
                    for target in (.8,.9):
                        n=required_tasks(discordance,delta,target,rho,size)
                        sample_sizes.append({'q':discordance,'absolute_effect':delta,'icc_of_signed_difference':rho,
                            'tasks_per_family':size,'target_power':target,'approx_required_tasks':n,
                            'approx_required_families':n//size,'episodes_all_four_conditions':n*4,
                            'approx_power_at_480':approximate_power(480,discordance,delta,rho,size),
                            'approx_power_at_500':approximate_power(500,discordance,delta,rho,size),
                            'method':'Paired binary normal approximation inflated by signed-difference DE; balanced complete families/category; validate exact endpoint test by simulation.'})
    write_csv(HERE/'power_sample_sizes.csv',sample_sizes)
    simulated=[]
    scenarios=[(n,rho,delta) for n in (480,500) for rho in (0,.1,.3,observed_rho) for delta in (0,.03,.05,3/54,.075,.10)]
    # Validate some formula-derived N values, rather than claiming they guarantee target power.
    scenarios.extend((required_tasks(q,delta,target,.1,5),.1,delta) for delta in (.03,.05,3/54,.075,.10) for target in (.8,.9))
    for index,(n,rho,delta) in enumerate(dict.fromkeys(scenarios)):
        result=simulate(n,q,delta,rho,5,simulations,SEED+index)
        simulated.append(result)
        print(f'Power simulation {index+1}: N={n}, rho={rho}, delta={delta:.5f}, power={result["estimated_power"]:.3f}',flush=True)
    write_csv(HERE/'power_simulations.csv',simulated)
    secondary=[]
    for discordance in (.02,.05,.10):
        for delta in (.01,.02,.03,.05):
            if delta>discordance:continue
            secondary.append({'endpoint':'final_state_correctness','assumed_q':discordance,'effect':delta,
                'required_80pct_tasks_rho_0_1_m5':required_tasks(discordance,delta,.8,.1,5),
                'required_90pct_tasks_rho_0_1_m5':required_tasks(discordance,delta,.9,.1,5),
                'warning':'Observed q=0 across 54 new pairs does not identify power under nonzero alternatives; these are sensitivity assumptions.'})
    write_csv(HERE/'final_state_power_sensitivity.csv',secondary)
    cost,forecasts=cost_forecast(old_rows,new_rows);write_csv(HERE/'cost_forecasts.csv',forecasts)
    phase=ledger.phase_exposure();total=ledger.exposure()
    budget={'user_reported_api_balance_usd':str(REPORTED_BALANCE),'balance_verified_by_provider':False,
        'known_experiment_spend_usd':str(sum((Decimal(s['cost']) for s in ledger.states.values() if s['state']=='settled'),Decimal(0))),
        'encumbered_unverified_usd':str(ledger.encumbered_exposure()),'experiment_exposure_usd':str(total),
        'phase3_exposure_usd':str(phase),'existing_phase3_cap_usd':'6.00','existing_experiment_cap_usd':'9.00',
        'existing_phase3_headroom_usd':str(Decimal(6)-phase),'existing_experiment_headroom_usd':str(Decimal(9)-total),
        'proposed_new_study_exposure_cap_usd':str(CAP),'authorization_granted':False,
        'exact_required_phase3_cap_usd':str(phase+CAP),'exact_phase3_cap_increase_usd':str(phase+CAP-Decimal(6)),
        'optional_cent_rounded_phase3_cap_usd':str((phase+CAP).quantize(Decimal('.01'),rounding=ROUND_CEILING)),
        'projected_global_exposure_at_new_study_cap_usd':str(total+CAP),
        'global_cap_increase_required':total+CAP>Decimal(9),
        'account_balance_note':'Reported balance does not authorize raising experiment/Phase 3 caps. Holds remain fully counted; no recharge is relied upon.',
        'ledger_entries':len(ledger.entries),'ledger_sha256':digest(shared.ROOT/'results/spend.jsonl'),
        'blocking_reservations':pending,'cap_change_applied':False,'paid_calls_made':0}
    protocol={'status':'NOT_RELEASED_DIVERSITY_AND_PHASE3_AUTHORIZATION_REQUIRED','version':'luna-planning-v1',
        'candidate_tasks':480,'candidate_episodes':1920,'requested_comparison_tasks':500,
        'sample_size_note':'480 is a prospective budget-balanced alternative, not a frozen executed allocation. No power guarantee for modest effects; no task set has passed novelty review.',
        'provisional_family_design':{'families':96,'tasks_per_family':5,'families_per_category':32,'tasks_per_category':160,
            'independence_confirmed':False,'note':'Family labels are hypotheses about dependence, not certification. No existing task template may simply be renamed.'},
        'model':MODEL,'conditions':CONDITIONS,'primary':'Two-sided C-B operational success at alpha=.05; one model/one primary hypothesis.',
        'secondary':['Final-state correctness separately','Final-status correctness','Violations/retries/duplicates/stopping','Tools/tokens/cost/latency','D-C, B-A, C-A, D-A'],
        'analysis':'Task-weighted paired differences; category-stratified whole-family bootstrap (10000 draws) for CI; exact integer whole-family sign-flip test at complete endpoint; conditional joint sign-symmetry/independent-family assumptions. Secondary Holm correction across predeclared family of secondary contrasts/metrics.',
        'settings':{'limits':asdict(LIMITS),'reasoning_effort':'low','request':shared.REQUEST_SETTINGS,
            'system':SYSTEM,'final_format':FINAL_FORMAT,'tools':TOOLS},
        'randomization_seed':20261030,'randomization_rule':'After task/family freeze, shuffle family membership and task/model blocks once with seed; shuffle ABCD within each task. Fresh conversations/state, deterministic fault schedules independent of condition.',
        'endpoint':'Primary inferential analysis only after all fixed-N ABCD blocks complete and validate. No interim p-values, observed-power updates, sample-size increases or task replacement based on outcomes.',
        'stopping_rule':'Hard $2.50 cumulative study exposure (settled costs plus active/unknown reservations). Existing global $9 and approved Phase 3 ceiling simultaneously enforced. Conservative block admission plus per-request reserve. Budget/infrastructure stop is terminal audit/descriptive partial report, not a confirmatory endpoint or permission to top up. No automatic encumbrances/retries/repeated acknowledged requests.',
        'exclusions':'Agent errors, limits, output truncation and invalid reports are failures. Infrastructure separate; unattempted spending-cap censoring is not failure. Paired comparisons require complete ABCD allocation. Preserve all attempts.',
        'taskset_sha256':None,'allocation_sha256':None,'execution_enabled':False,
        'release_gates':['Explicit Phase 3 cap authorization','Independent scenario-family inventory/structural review','All task specifications and gold/oracle/negative/reset/leakage tests','Task-aware forecast within study cap','Final N, task hashes, schedule and analysis implementation frozen together before any call']}
    diversity={'confirmed':False,'current_simulator_file_sha256':digest(shared.ROOT/'simulator.py'),
        'existing_templates':'Independent records, ordered count increments, fixed record-level eligibility/authorization/availability, before/after commit or read/validation transients.',
        'missing_mechanisms':['Cross-record prerequisites or branching decisions','Resource/content-dependent eligibility and meaningful alternative valid plans','Atomic multi-record partial writes/idempotency or compensation','Dynamic authorization transitions and genuinely asynchronous status'],
        'decision':'Do not generate hundreds of names/count vectors and claim independent new scenarios. Current interface supports new parameterizations, not confirmed 96 new independent template families.',
        'required_review':'Individually authored task briefs; mechanistic family taxonomy, not domain labels alone; name/order/parameter-normalized duplicate and isomorphism checks against previous tasks; independent golden traces and forbidden-action tests; no tuning from live C/B effects.',
        'new_task_count':0,'frozen_taskset':False,'old_52_unattempted_episodes':'Remain preserved, separate and untouched; this proposal does not resume them.'}
    outputs={'observed_pairs.json':observed,'cost_evidence.json':cost,'budget_authorization_request.json':budget,
             'candidate_protocol.json':protocol,'diversity_feasibility.json':diversity}
    for name,data in outputs.items(): (HERE/name).write_text(json.dumps(data,indent=2)+'\n',encoding='utf-8')
    for name,sha in preserved.items():
        if digest(shared.ROOT/name)!=sha:raise ValueError('Prior file changed during offline preparation: '+name)
    (HERE/'preservation_audit.json').write_text(json.dumps({'unchanged_files':len(preserved),'file_sha256':preserved,
        'paid_calls_made':0,'ledger_unchanged':True,'source_files':{p.name:digest(p) for p in HERE.glob('*.py')},
        'prior_extension_manifest_sha256':prior['sha256']},indent=2),encoding='utf-8')
    report(observed,cost,budget,protocol,simulated)
    print(json.dumps({'status':protocol['status'],'paid_calls':0,'phase3_headroom':budget['existing_phase3_headroom_usd'],
        'new_study_cap_request':budget['exact_required_phase3_cap_usd'],'preserved_files':len(preserved)}))


def report(observed,cost,budget,protocol,simulated):
    q=observed['extension']['operational_task_success']['discordance_q']
    lines=['# Luna-focused study: prospective feasibility and authorization request','',
        '**Not released. No paid calls; no cap changes; no frozen task set or execution command.**','',
        'The existing results and full ledger were read and preserved. User-reported API balance is $4.91, not verified account data and not a spending-cap override.',
        '', '## Current accounting','',
        f"Known experiment spending: ${budget['known_experiment_spend_usd']}; unverified holds: ${budget['encumbered_unverified_usd']}; total exposure: ${budget['experiment_exposure_usd']}.",
        f"Phase 3 exposure: ${budget['phase3_exposure_usd']}; headroom under $6: ${budget['existing_phase3_headroom_usd']}. Global headroom under $9: ${budget['existing_experiment_headroom_usd']}.",
        '',f"Authorization request: raise only the Phase 3 cap from $6.00 to **${budget['exact_required_phase3_cap_usd']}**, an increase of **${budget['exact_phase3_cap_increase_usd']}**. Retain global $9, and impose an independent new-study $2.50 exposure cap INCLUDING active/unknown reservations. Optional cent-rounded Phase 3 cap is ${budget['optional_cent_rounded_phase3_cap_usd']}; the local cap stays $2.50. This does not authorize spending on the old 52 unfinished episodes or release either historical hold.",
        '', '## Observed paired outcomes and uncertainty','',
        f'Extension Luna: 54 paired tasks/30 domain families; C-only successes 5, B-only 2, ties 47. Operational delta=3/54; discordance q=7/54={q:.6f}. These seven events do not identify a stable future effect or correlation.',
        f"Signed paired-difference ANOVA ICC: {observed['extension']['operational_task_success']['signed_difference_icc_raw']}. This positive estimate is uncertain with seven discordances and unequal two/singleton families. ICC .10 is an optimistic sensitivity, not the observed central estimate. The observed point and higher correlations are included explicitly.",
        'Original Luna: 24 pairs, 4 C-only and 2 B-only operational successes. Do not silently pool task distributions or old broad templates with new domain-family labels. Both prior operational intervals include zero; a true null or regression is plausible.',
        'Final-state correctness: no B/C discordances in the 54 extension pairs. This cannot be plugged in as zero future variance or proof of equivalence. Separate final-state power sensitivities assume new positive discordance rates; no operational/reporting gain is treated as a physical-state gain.',
        '', '## Prospective sample sizes','',
        'Single Luna primary, two-sided alpha .05. Let D=C-B, q=P(|D|=1), delta=E[D]. Var(D)=q-delta². Inflate paired normal-approximation N by 1+(m-1)rho, where rho is correlation of SIGNED PAIRED DIFFERENCES, not raw successes. Round up to complete equal-sized families in three categories. This adapts paired-binary and cluster-design formulas; it is not a claim that our experiment randomizes treatments by family.',
        '', 'At q=7/54, m=5 tasks/family, and the observed ICC point estimate (about .358):', '', '| Absolute operational effect | Tasks for 80% (approx.) | Tasks for 90% (approx.) |','|---|---:|---:|']
    rho=observed['extension']['operational_task_success']['signed_difference_icc_clipped']
    for d in (.02,.03,.05,3/54,.075,.10):lines.append(f'| {100*d:.2f} percentage points | {required_tasks(q,d,.8,rho,5)} | {required_tasks(q,d,.9,rho,5)} |')
    lines+=['','Opposite-direction effects of the same magnitude require the same approximate N. Small 2–3 point effects need far more than about 500 tasks. The observed 5.56-point estimate is not a reliable planning truth. Approximately 500 tasks do not guarantee 80% power for five points, and do not attain 90% under this central sensitivity scenario.',
        '', 'power_sample_sizes.csv includes discordance .05, 7/54, .25 and .35; family sizes 1,2,5,10; ICC 0,.1,.3, observed .358,.5,.8, and 80/90% targets. Some discordance/effect combinations imply different baseline success rates from prior data. New-task discordance may change.',
        '', '## Exact-test simulation checks','',
        'Dirichlet-multinomial signed paired differences give marginal q/delta and within-family ICC rho. Whole-family sign-flip p-values use exact integer subset sums, not independent-task McNemar. The null distribution assumes independent families and joint sign symmetry. All simulations are prospective hypothetical data, not new model results. Wilson intervals quantify Monte Carlo error only; they exclude parameter/design uncertainty.',
        '', '| N | rho | Delta | Simulated power | 95% Monte Carlo interval |','|---:|---:|---:|---:|---|']
    for s in simulated:
        if s['tasks'] in (480,500) and s['delta'] in (0,.05,3/54) and s['icc_rho'] in (.1,.3):
            lines.append(f"| {s['tasks']} | {s['icc_rho']} | {s['delta']:.5f} | {s['estimated_power']:.3f} | [{s['mc_interval_low']:.3f}, {s['mc_interval_high']:.3f}] |")
    lines+=['','Formula-derived N values are approximations, not guaranteed test power. power_simulations.csv checks selected rounded-N endpoints as well as 480 and 500. A future task/family allocation must be reviewed against these assumptions; the family correlation is not certified by its label.',
        '', '## Cost feasibility','',
        f"Observed Luna evidence: {cost['episode_count']} prior episodes and {cost['known_request_count']} usage-reported requests, recomputed at preserved configured pricing. Pooled mean episode cost ${cost['pooled_mean_episode_cost_usd']}. Original and extension workloads differ; both remain visible in cost_forecasts.csv.",
        'One original episode contains an unknown-usage timeout; it is excluded from the complete-episode unit-cost mean rather than assigned zero cost. Its full $0.02 remains in ledger exposure and appears in a separate forecast stress, not as a confirmed charge.',
        'At the pooled mean of 311 complete-cost episodes, 500 tasks × four conditions forecasts **$2.54353** including 50% contingency and the $0.02 peak request allowance. That misses the $2.50 target. A balanced 480-task/1,920-episode alternative forecasts **$2.44259** on that basis. Using only the cheaper latest extension would be more optimistic. The additional unknown-charge calibration stress is more expensive; neither figure is a task-aware or hard upper-bound forecast.',
        'The 480-task candidate is conditional, not selected to maximize significance. It has 96 hypothetical families of five, 32/category. It is not powered for small effects and may still stop early if new tasks are costlier. The hard study ceiling counts confirmed new cost plus active/unknown reservations without double counting settled allowances. Every request must reserve before sending; no automatic retries/encumbrances. No account recharge is assumed.',
        '', '## Diversity gate and candidate protocol','',
        '**Sufficient independent scenario diversity has NOT been confirmed.** All current environments use record-local linear count updates and a small set of before/after-commit/read/validation faults. Hundreds of new names, permutations or count vectors would reuse that task template. Structural uniqueness is not independent scenario generation. The previous zero final-state CB discordance and repeated Sol ceiling reinforce this limitation, but new tasks must not be engineered solely around Luna errors.',
        'No 480/500-task set is claimed frozen. No task hashes, episode IDs or final randomization are fabricated. A separate simulator/family-design decision and independently authored/oracle-validated cases are needed before a genuine task-set freeze. Prior definitions/results remain immutable. Richer versioned mechanisms would change the benchmark distribution and require offline and task-aware cost review; old-task power/cost would remain only provisional.',
        'candidate_protocol.json records the intended original A/B/C/D prompts, Luna low reasoning and existing limits, primary/secondary outcomes, seed 20261030, fixed endpoint, no interim p-values and a hard budget/infrastructure abort. It is explicitly NOT a preregistered executable study. Once novelty is independently reviewed, freeze the actual N, all task/source hashes, complete allocation, scorer, oracle/negative tests and endpoint implementation together before calls.',
        'At a complete fixed-N endpoint: report C-B effect, family-cluster CI and primary p; state correctness separately. At a budget/infrastructure abort: audit and descriptive partial coverage only; no confirmatory p-value, endpoint extension, top-up or task replacement based on outcomes.',
        '', '## Reproduce offline','', '```powershell',
        'competitive_prompting/.venv/Scripts/python.exe -m competitive_prompting.phase3.luna_planning_v1.prepare --simulations 2000',
        'competitive_prompting/.venv/Scripts/python.exe -m competitive_prompting.phase3.luna_planning_v1.calibrate',
        'competitive_prompting/.venv/Scripts/python.exe -m pytest competitive_prompting/phase3/luna_planning_v1/tests -q', '```',
        '', 'No live execution entry point is provided. Approving the cap alone does not satisfy the unresolved diversity/task-freeze gates.',
        '', 'Method references: paired discordance/sample-size formula in [Ying et al., correlated binary paired outcomes](https://pmc.ncbi.nlm.nih.gov/articles/PMC11223621/); cluster design-effect/reporting principles in [CONSORT cluster extension](https://pmc.ncbi.nlm.nih.gov/articles/PMC381234/). The adaptation to signed task differences and exact-test simulation is implemented explicitly here.',
        '', 'No paid calls, cap changes, commits, pushes or PRs.']
    (HERE/'readiness_report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--simulations',type=int,default=2000)
    args=parser.parse_args()
    if args.simulations<100:parser.error('Use at least 100 Monte Carlo repetitions')
    prepare(args.simulations)


if __name__=='__main__':main()
