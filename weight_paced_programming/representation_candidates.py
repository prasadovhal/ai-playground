"""Frozen four-parameter candidates. Coefficient order is (w, a, b, c)."""
import torch

NAMES = ('original', 'exp_chirp', 'rational_chirp', 'polynomial',
         'gaussian_sine', 'two_harmonics', 'mixed_envelopes',
         'saturating_phase', 'rational_sine', 'exp_harmonic')
FORMULAS = (
    'cos(w*t)*(1+a*t)/(1+b*t+c*t^2)',
    'exp(-a*t-b*t^2)*cos(w*t+c*t^2)',
    'cos(w*t+c*t^2)/(1+a*t+b*t^2)',
    '(1+a*t+b*t^2+c*t^3)*cos(w*t)',
    'exp(-a*t^2)*(cos(w*t)+b*sin(w*t)+c*t*cos(w*t))',
    '(cos(w*t)+a*sin(w*t)+b*cos(2*w*t))/(1+c*t^2)',
    '(a*exp(-b*t^2)+(1-a)/(1+c*t))*cos(w*t)',
    'cos(w*t+a*t/(1+t))/(1+b*t+c*t^2)',
    '(cos(w*t)+a*t*sin(w*t))/(1+b*t+c*t^2)',
    'exp(-a*t)*(cos(w*t)+b*sin(2*w*t)+c*cos(2*w*t))',
)
# Common bounds are applied to every coefficient, irrespective of role. The
# nonnegative bounds guarantee denominators >= 1 for the entire t >= 0 domain.
LOW = (0.0, 0.0, 0.0, 0.0)
HIGH = (4.0, 2.0, 2.0, 2.0)

def evaluate(name, t, coeff):
    w, a, b, c = coeff
    co, si, ex = torch.cos, torch.sin, torch.exp
    if name == 'original': return co(w*t)*(1+a*t)/(1+b*t+c*t*t)
    if name == 'exp_chirp': return ex(-a*t-b*t*t)*co(w*t+c*t*t)
    if name == 'rational_chirp': return co(w*t+c*t*t)/(1+a*t+b*t*t)
    if name == 'polynomial': return (1+a*t+b*t*t+c*t**3)*co(w*t)
    if name == 'gaussian_sine': return ex(-a*t*t)*(co(w*t)+b*si(w*t)+c*t*co(w*t))
    if name == 'two_harmonics': return (co(w*t)+a*si(w*t)+b*co(2*w*t))/(1+c*t*t)
    if name == 'mixed_envelopes': return (a*ex(-b*t*t)+(1-a)/(1+c*t))*co(w*t)
    if name == 'saturating_phase': return co(w*t+a*t/(1+t))/(1+b*t+c*t*t)
    if name == 'rational_sine': return (co(w*t)+a*t*si(w*t))/(1+b*t+c*t*t)
    if name == 'exp_harmonic': return ex(-a*t)*(co(w*t)+b*si(2*w*t)+c*co(2*w*t))
    raise ValueError('Unknown representation: '+str(name))
