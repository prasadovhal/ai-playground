from itertools import product
from math import isclose
import random

import pytest

from competitive_prompting.phase3.luna_planning_v1.power import (
    approximate_power,required_tasks,icc_moments,family_signflip,draw_families,simulate)
from competitive_prompting.phase3.results.balanced_v1.final_analysis.statistics_helpers import mcnemar_exact
from competitive_prompting.phase3.luna_planning_v1.prepare import observed_pairs,summarize_pairs


@pytest.mark.parametrize('sums',[[1]*7+[-1]*3,[2,-3,1,0],[0,0,0],[5,5,5,-5],[1,-1]])
def test_signflip_matches_brute_force(sums):
    observed=abs(sum(sums))
    values=[abs(s) for s in sums if s]
    exact=sum(abs(sum(a*b for a,b in zip(signs,values)))>=observed for signs in product((-1,1),repeat=len(values)))/2**len(values)
    assert family_signflip(sums)==exact
    assert family_signflip([-x for x in sums])==exact


def test_single_task_families_match_exact_mcnemar():
    for wins in range(11):
        for losses in range(11):
            assert family_signflip([1]*wins+[-1]*losses+[0]*5)==mcnemar_exact(wins,losses)


def test_shared_family_does_not_pretend_independent_tasks():
    assert family_signflip([6])==1
    assert family_signflip([1]*6)==2/64
    r=simulate(50,.2,.1,1,size=50,repetitions=100,seed=3)
    assert r['estimated_power']==0 and r['families']==1


def test_sample_size_effect_and_cluster_monotonicity():
    base=required_tasks(7/54,.05,.8,0,5)
    assert base%15==0
    assert required_tasks(7/54,.05,.8,.3,5)>base
    assert required_tasks(7/54,.05,.9,0,5)>base
    assert required_tasks(7/54,.03,.8,0,5)>base
    assert required_tasks(7/54,-.05,.8,0,5)==base
    assert required_tasks(7/54,.05,.8,.1,5)==570
    assert required_tasks(7/54,.05,.9,.1,5)==765
    assert approximate_power(500,7/54,.05,.3,5)<approximate_power(500,7/54,.05,0,5)


@pytest.mark.parametrize('q,delta,rho,size',[(0,.05,0,5),(.1,.2,0,5),(.1,.05,-.1,5),(.1,.05,0,0)])
def test_invalid_assumptions(q,delta,rho,size):
    with pytest.raises(ValueError):required_tasks(q,delta,.8,rho,size)


def test_no_zero_variance_final_state_power_claim():
    with pytest.raises(ValueError):required_tasks(0,0,.8,.1,5)
    with pytest.raises(ValueError):required_tasks(.1,0,.8,.1,5)
    assert icc_moments([[0,0],[0,0],[0]]) is None


def test_dirichlet_simulation_reproducible_and_has_target_moments():
    assert draw_families(random.Random(4),100,5,.2,.05,.3)==draw_families(random.Random(4),100,5,.2,.05,.3)
    rng=random.Random(7)
    sums=draw_families(rng,100000,5,.2,.05,.3)
    assert abs(sum(sums)/100000-.05)<.01
    # Var(sum D_i)=m*(q-delta²)*(1+(m-1)*rho).
    expected=5*(.2-.05**2)*(1+4*.3)
    average=sum(sums)/len(sums)
    variance=sum((s-average)**2 for s in sums)/len(sums)
    assert abs(variance-expected)<.10


def test_operational_and_final_state_are_distinct():
    rows=[{'model':'gpt-6-luna','task_id':'t','family':'f','category':'execution','condition':c,
           'operational_task_success':c=='C','verified_final_state_correct':True} for c in 'ABCD']
    op=observed_pairs(rows,'operational_task_success')
    state=observed_pairs(rows,'verified_final_state_correct')
    assert op[0]['difference']==1 and state[0]['difference']==0


def test_icc_includes_signed_pair_information():
    assert isclose(icc_moments([[1,1],[-1,-1],[0,0]]),1)
    assert icc_moments([[1,-1],[1,-1],[0,0]])<0


def test_insufficient_families_do_not_produce_degenerate_confidence_claim():
    pairs=[{'task_id':str(i),'family':'single','category':'execution','difference':i%2} for i in range(4)]
    summary=summarize_pairs(pairs)
    assert summary['families']==1 and summary['delta_cluster_ci'] is None
    assert summary['q_cluster_ci'] is None
