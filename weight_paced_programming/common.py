"""Shared physics, MLP, baselines and evaluation for 01_wsp / 02_iwsp.

Calculations copied from the original notebook and scripts, without algorithm
changes. The original WSP reference remains analytical for the free case;
metrics() uses the later DOP853 reference on [0,20]. No API calls on import.
"""
import copy
import json
import math
import time
from pathlib import Path
import numpy as np
import torch
from scipy.integrate import solve_ivp

HERE = Path(__file__).resolve().parent
torch.set_num_threads(1)
torch.set_default_dtype(torch.float64)
T = torch.linspace(0, 10, 101).reshape(-1, 1)
THELD = np.linspace(.005, 9.995, 1000)
EXTRA = np.linspace(10.005, 20, 1000)
CONFIGS = {'free': (0., 0.), 'forced': (0.5, 0.), 'duffing': (0., 1.)}

def force(t, case):
    return CONFIGS[case][0] * torch.cos(1.7*t)
def reference(t, case):
    t = np.asarray(t)
    if case == 'free':
        wd = np.sqrt(4 - .2**2)
        return np.exp(-.2*t)*(np.cos(wd*t)+.2/wd*np.sin(wd*t))
    amp,beta = CONFIGS[case]
    sol=solve_ivp(lambda s,y: [y[1], amp*np.cos(1.7*s)-.4*y[1]-4*y[0]-beta*y[0]**3], (0,10), [1,0], method='DOP853', rtol=1e-12, atol=1e-13, dense_output=True)
    assert sol.success
    return sol.sol(t)[0]

class Net(torch.nn.Module):
    def __init__(self, seed, zero=False):
        super().__init__(); torch.manual_seed(seed)
        self.net=torch.nn.Sequential(torch.nn.Linear(1,16),torch.nn.Tanh(),torch.nn.Linear(16,16),torch.nn.Tanh(),torch.nn.Linear(16,1))
        for layer in (self.net[0],self.net[2],self.net[4]):
            torch.nn.init.zeros_(layer.bias)
            if zero: torch.nn.init.zeros_(layer.weight)
            else: torch.nn.init.xavier_uniform_(layer.weight)
    def forward(self,t): return self.net(t)
def terms(model, t, case, graph=True):
    z=t.detach().clone().requires_grad_(True); x=model(z)
    v=torch.autograd.grad(x,z,torch.ones_like(x),create_graph=True)[0]
    a=torch.autograd.grad(v,z,torch.ones_like(v),create_graph=graph)[0]
    r=a+.4*v+4*x+CONFIGS[case][1]*x**3-force(z,case)
    return x,v,r
def losses(model,case):
    x,v,r=terms(model,T,case)
    x0,v0,_=terms(model,torch.zeros((1,1)),case)
    lp=(r*r).mean(); ic=(x0-1).square().mean()+v0.square().mean()
    return lp+ic,lp,ic
def evaluate(model,case):
    x,v,r=terms(model,torch.tensor(THELD[:,None]),case,False)
    pred=x.detach().numpy().ravel(); target=reference(THELD,case)
    _,_,ic=losses(model,case)
    return {'relative_l2':float(np.linalg.norm(pred-target)/np.linalg.norm(target)), 'residual_rmse':float(torch.sqrt((r*r).mean()).detach()), 'ic_error':float(torch.sqrt(ic).detach())}
def train(model,case,steps):
    opt=torch.optim.Adam(model.parameters(),lr=.003); start=time.perf_counter()
    for _ in range(steps):
        opt.zero_grad(); loss,_,_=losses(model,case); loss.backward(); opt.step()
    return time.perf_counter()-start

def apply_patch(model, obj):
    if not isinstance(obj,dict) or set(obj)!={'rationale','operations'} or not isinstance(obj['rationale'],str) or len(obj['rationale'])>1000: raise ValueError('invalid top-level patch')
    ops=obj['operations']; params=dict(model.named_parameters()); checked=[]; seen=set()
    if not isinstance(ops,list) or not 1<=len(ops)<=400: raise ValueError('operations must contain 1..400 edits')
    for op in ops:
        if not isinstance(op,dict) or set(op)!={'tensor','index','value'}: raise ValueError('invalid operation fields')
        name,idx,val=op['tensor'],op['index'],op['value']
        if name not in params or not isinstance(idx,list) or len(idx)!=params[name].ndim: raise ValueError('tensor or index rank invalid')
        if any(type(i) is not int or i<0 or i>=params[name].shape[j] for j,i in enumerate(idx)): raise ValueError('index out of range')
        if type(val) not in (int,float) or not math.isfinite(val) or abs(val)>100: raise ValueError('invalid value')
        key=(name,tuple(idx))
        if key in seen: raise ValueError('duplicate index')
        seen.add(key); checked.append((params[name],tuple(idx),float(val)))
    with torch.no_grad():
        for tensor,idx,val in checked: tensor[idx]=val
    return len(checked)
def diagnostics(model,case):
    loss,lp,ic=losses(model,case)
    points=torch.tensor([[0.],[1.],[2.],[4.],[6.],[8.],[10.]])
    x,v,r=terms(model,points,case,False)
    return {'loss':float(loss.detach()),'physics_mse':float(lp.detach()),'ic_squared_error':float(ic.detach()),'points':[{'t':float(t),'x':float(a),'velocity':float(b),'residual':float(c)} for t,a,b,c in zip(points.ravel(),x.detach().ravel(),v.detach().ravel(),r.detach().ravel())]}

def fixed_features(model,case):
    # Build the linear operator by evaluating output basis networks, not by fitting hidden weights.
    begin=time.perf_counter(); base=copy.deepcopy(model)
    with torch.no_grad(): base.net[4].weight.zero_(); base.net[4].bias.zero_()
    def vector(net):
        _,_,r=terms(net,T,case,False); x0,v0,_=terms(net,torch.zeros((1,1)),case,False)
        return np.r_[r.detach().numpy().ravel()/np.sqrt(len(T)),float(x0.detach()),float(v0.detach())]
    offset=vector(base); cols=[]
    for j in range(17):
        b=copy.deepcopy(base)
        with torch.no_grad():
            if j<16: b.net[4].weight[0,j]=1
            else: b.net[4].bias[0]=1
        cols.append(vector(b)-offset)
    coef=np.linalg.lstsq(np.column_stack(cols),-offset+np.r_[np.zeros(len(T)),1.,0.],rcond=None)[0]
    with torch.no_grad(): base.net[4].weight.copy_(torch.tensor(coef[:16][None,:])); base.net[4].bias.copy_(torch.tensor(coef[16:]))
    return base,time.perf_counter()-begin,36

def numerical_reference(t, case='free'):
    """Independent high-accuracy numerical reference, extended to t=20."""
    t = np.asarray(t)
    sol = solve_ivp(lambda s, y: [y[1], -.4*y[1]-4*y[0]], (0, 20), [1, 0],
                    method='DOP853', rtol=1e-12, atol=1e-13, dense_output=True)
    assert sol.success
    return sol.sol(t)[0]

def metrics(model, grid):
    x, v, r = terms(model, torch.tensor(grid[:, None]), 'free', False)
    pred = x.detach().numpy().ravel()
    ref = numerical_reference(grid)
    x0, v0, _ = terms(model, torch.zeros((1, 1)), 'free', False)
    return {'relative_l2': float(np.linalg.norm(pred-ref)/np.linalg.norm(ref)),
            'residual_rmse': float(torch.sqrt((r*r).mean()).detach()),
            'ic_position_abs': abs(float(x0.detach())-1),
            'ic_velocity_abs': abs(float(v0.detach())),
            'ic_error': math.hypot(float(x0.detach())-1, float(v0.detach()))}

def replay_patches(initials):
    """Replay accepted historical WSP patches; never requests an LLM response."""
    history = json.loads((HERE / 'iwsp_results' / 'original_wsp_history.json').read_text(encoding='utf-8'))
    for entry in history:
        if entry['valid']:
            apply_patch(initials[entry['initialization']], entry['patch'])
    return initials
