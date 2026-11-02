"""Paired-difference, family-clustered power calculations using only stdlib.

The analytic design-effect calculation is an approximation. Simulation uses the
same exact whole-family sign-flip test proposed for a complete future endpoint.
No candidate-task outcomes or API calls are used.
"""
from functools import lru_cache
from math import ceil, sqrt
import random
from statistics import NormalDist, mean

NORMAL=NormalDist()
ALPHA=.05


def validate(q, delta, rho, size):
    if not (0<q<=1 and abs(delta)<=q and q-delta*delta>0 and 0<=rho<=1):
        raise ValueError('Invalid discordance/effect/ICC')
    if type(size) is not int or size<1: raise ValueError('Invalid family size')


def approximate_power(n,q,delta,rho=0,size=1,alpha=ALPHA):
    validate(q,delta,rho,size)
    if n<1: raise ValueError('Positive task count required')
    effect=abs(delta);de=1+(size-1)*rho
    critical=NORMAL.inv_cdf(1-alpha/2)*sqrt(q*de/n)
    sd=sqrt((q-effect*effect)*de/n)
    return 1-NORMAL.cdf((critical-effect)/sd)+NORMAL.cdf((-critical-effect)/sd)


def required_tasks(q,delta,power,rho=0,size=1,alpha=ALPHA):
    validate(q,delta,rho,size)
    if not 0<abs(delta) or not .5<power<1: raise ValueError('Nonzero effect and target power >.5 required')
    # Paired binary normal approximation; variance of D=C-B is q-delta^2.
    # Round to balanced complete families across three categories.
    n=((NORMAL.inv_cdf(1-alpha/2)*sqrt(q)+NORMAL.inv_cdf(power)*sqrt(q-delta*delta))/abs(delta))**2
    unit=3*size
    return ceil(n*(1+(size-1)*rho)/unit)*unit


def icc_moments(groups):
    """Unequal-size one-way ANOVA ICC of SIGNED paired differences, not successes."""
    sizes=[len(g) for g in groups];n=sum(sizes);k=len(groups)
    values=[v for g in groups for v in g]
    if k<2 or n<=k or not values: return None
    grand=mean(values)
    msb=sum(len(g)*(mean(g)-grand)**2 for g in groups)/(k-1)
    msw=sum(sum((v-mean(g))**2 for v in g) for g in groups)/(n-k)
    effective=(n-sum(s*s for s in sizes)/n)/(k-1)
    denominator=msb+(effective-1)*msw
    return (msb-msw)/denominator if denominator>0 else None


@lru_cache(maxsize=100000)
def _signflip(weights,observed):
    # Count exact sign assignments by integer subset sums. Zero-sum families do
    # not alter the statistic; their omitted sign factors cancel in numerator/denominator.
    if observed==0: return 1.0
    dp=[1]
    for w in weights:
        nxt=dp+[0]*w
        for i,count in enumerate(dp): nxt[i+w]+=count
        dp=nxt
    total=sum(weights)
    tail=sum(count for i,count in enumerate(dp) if abs(total-2*i)>=observed)
    return tail/(1<<len(weights))


def family_signflip(sums):
    if any(type(x) is not int for x in sums): raise ValueError('Integer family sums required')
    return _signflip(tuple(sorted(abs(s) for s in sums if s)),abs(sum(sums)))


def draw_families(rng,n,size,q,delta,rho):
    validate(q,delta,rho,size)
    if n%size: raise ValueError('Complete equal-sized families required for simulation')
    marginal=((q-delta)/2,1-q,(q+delta)/2)
    sums=[]
    for _ in range(n//size):
        if rho==1:
            u=rng.random();d=-1 if u<marginal[0] else (0 if u<marginal[0]+marginal[1] else 1)
            sums.append(size*d);continue
        if rho==0: prob=marginal
        else:
            concentration=(1-rho)/rho
            gamma=[rng.gammavariate(p*concentration,1) if p else 0 for p in marginal]
            total=sum(gamma)
            if total==0: raise ValueError('Numerically degenerate Dirichlet draw')
            prob=[v/total for v in gamma]
        total=0
        for _ in range(size):
            u=rng.random();total+=-1 if u<prob[0] else (0 if u<prob[0]+prob[1] else 1)
        sums.append(total)
    return sums


def wilson(successes,n):
    z=NORMAL.inv_cdf(.975);p=successes/n;den=1+z*z/n
    center=(p+z*z/(2*n))/den
    half=z*sqrt(p*(1-p)/n+z*z/(4*n*n))/den
    return center-half,center+half


def simulate(n,q,delta,rho,size=5,repetitions=2000,seed=20261029):
    rng=random.Random(seed);rejections=0
    for _ in range(repetitions):
        sums=draw_families(rng,n,size,q,delta,rho)
        rejections+=family_signflip(sums)<=ALPHA
    low,high=wilson(rejections,repetitions)
    return {'tasks':n,'families':n//size,'tasks_per_family':size,'discordance_q':q,'delta':delta,
        'icc_rho':rho,'simulations':repetitions,'seed':seed,'estimated_power':rejections/repetitions,
        'mc_interval_low':low,'mc_interval_high':high,'rejections':rejections,
        'test':'Exact whole-family sign flip; Dirichlet-multinomial signed-difference model',
        'role':'type-I calibration' if delta==0 else 'prospective alternative'}
