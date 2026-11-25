"""Frozen study runner. No API request or final metric is made on import."""
import argparse
import csv
import json
import math
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path

import numpy as np
import torch
import common
from representation_candidates import NAMES, FORMULAS, LOW, HIGH, evaluate

HERE = Path(__file__).resolve().parent
OUT = HERE / 'representation_run'

class Candidate(torch.nn.Module):
    def __init__(self, name, coefficients, trainable=False):
        super().__init__()
        self.name = name
        self.coefficients = torch.nn.Parameter(torch.tensor(coefficients, dtype=torch.float64), requires_grad=trainable)
    def forward(self, t):
        return evaluate(self.name, t, self.coefficients)

def validate(coeff):
    if not isinstance(coeff, list) or len(coeff) != 4:
        raise ValueError('coefficients must be an array of four numbers')
    if any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) or not lo <= v <= hi for v, lo, hi in zip(coeff, LOW, HIGH)):
        raise ValueError('coefficients must be finite and inside bounds')

def feedback(name, coeff):
    validate(coeff)
    result = common.diagnostics(Candidate(name, coeff), 'free')
    if not all(math.isfinite(result[k]) for k in ('loss', 'physics_mse', 'ic_squared_error')):
        raise ValueError('nonfinite feedback')
    return result

def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, allow_nan=False), encoding='utf-8')

def agent_session(name, index, model):
    from openai import OpenAI
    # A placeholder inherited by the process must not shadow the workspace key.
    # Read only the relevant value; never print or persist credentials.
    current = os.environ.get('OPENAI_API_KEY', '').strip()
    if current.lower() in ('', 'dummy', 'test', 'your-api-key'):
        from dotenv import dotenv_values
        key = (dotenv_values(HERE.parent / '.env').get('OPENAI_API_KEY') or '').strip()
        if key:
            os.environ['OPENAI_API_KEY'] = key
    path = OUT / 'agent' / f'{name}_{index:02}.json'
    if path.exists():
        raise FileExistsError(f'Refusing to overwrite {path}')
    client = OpenAI(timeout=45, max_retries=0)
    system = ('You are an independent coefficient-programming agent. Select four coefficients by reasoning only; '
              'do not use numerical optimization, gradient training, an analytical solution, a reference trajectory, '
              'other sessions, or final metrics. You have three sequential physics evaluations. '
              'Output only a JSON object with keys coefficients (array [w,a,b,c]) and explanation (string).')
    prompt = (f'Solve x\'\'+0.4x\'+4x=0, x(0)=1, x\'(0)=0 over [0,10]. '
              f'Assigned representation: {FORMULAS[NAMES.index(name)]}. '
              'All four coefficients are adjustable; fixed constants are literal. '
              'Bounds: 0<=w<=4; 0<=a,b,c<=2. Choose your first coefficients.')
    messages = [{'role':'system','content':system}, {'role':'user','content':prompt}]
    record = {'representation':name, 'session':index, 'requested_model':model, 'settings':{'max_output_tokens':500, 'reasoning_effort':'none'},
              'system':system, 'initial_prompt':prompt, 'proposals':[], 'status':'running'}
    start = time.perf_counter()
    for i in range(3):
        try:
            before = time.perf_counter()
            response = client.responses.create(model=model, input=messages, max_output_tokens=500, reasoning={'effort':'none'})
            raw = response.output_text
            usage = response.usage.model_dump() if response.usage else None
            entry = {'raw_response':raw, 'model':response.model, 'response_id':response.id,
                     'usage':usage, 'inference_seconds':time.perf_counter()-before, 'cost_usd':None}
            record['proposals'].append(entry)
            obj = json.loads(raw)
            validate(obj['coefficients'])
            if not isinstance(obj.get('explanation'), str): raise ValueError('explanation missing')
            entry['coefficients'], entry['explanation'] = obj['coefficients'], obj['explanation']
            before = time.perf_counter()
            entry['feedback'] = feedback(name, obj['coefficients'])
            entry['feedback_seconds'] = time.perf_counter()-before
            messages.extend([{'role':'assistant','content':raw}, {'role':'user','content':json.dumps({'evaluation':i+1,'feedback':entry['feedback'], 'instruction':'Revise your coefficients using this feedback; return the same JSON format.'})}])
            save(path, record)
        except Exception as exc:
            # Provider authentication messages can echo credentials; record
            # the class and status without persisting the provider's text.
            record['error'] = {'type':type(exc).__name__, 'status_code':getattr(exc, 'status_code', None), 'proposal':i+1}
            break
    valid = [p for p in record['proposals'] if 'feedback' in p]
    record['status'] = 'complete' if len(valid)==3 else 'failed'
    record['wall_seconds'] = time.perf_counter()-start
    if valid:
        best = min(valid, key=lambda p:p['feedback']['loss'])
        record['selected_coefficients'] = best['coefficients']
        record['selection_loss'] = best['feedback']['loss']
    save(path, record)
    return record

def gradient_session(name, index, steps=1000):
    path = OUT / 'gradient' / f'{name}_{index:02}.json'
    if path.exists(): raise FileExistsError(f'Refusing to overwrite {path}')
    rng = np.random.default_rng(20261124 + index)
    initial = rng.uniform(LOW, HIGH).tolist()
    model = Candidate(name, initial, True)
    opt = torch.optim.Adam(model.parameters(), lr=.01)
    best_loss, best = math.inf, None
    start = time.perf_counter()
    error = None
    for step in range(steps):
        opt.zero_grad()
        loss, _, _ = common.losses(model, 'free')
        val = float(loss.detach())
        if not math.isfinite(val):
            error = 'nonfinite loss'; break
        if val < best_loss:
            best_loss, best = val, model.coefficients.detach().tolist()
        loss.backward()
        if not torch.isfinite(model.coefficients.grad).all():
            error = 'nonfinite gradient'; break
        opt.step()
        with torch.no_grad():
            model.coefficients.clamp_(torch.tensor(LOW), torch.tensor(HIGH))
    record = {'representation':name,'session':index,'method':'gradient','initial_coefficients':initial,
              'selected_coefficients':best,'selection_loss':best_loss if best is not None else None,
              'steps_completed':step+1,'status':'complete' if error is None else 'failed',
              'error':error,'wall_seconds':time.perf_counter()-start,'optimizer':'Adam','learning_rate':.01}
    save(path,record)
    return record

def final_grids():
    rng = np.random.default_rng(20261124)
    return np.sort(rng.uniform(0,10,2000)), np.sort(rng.uniform(10,20,2000))

def final_metrics(name, coeff):
    model = Candidate(name, coeff)
    inside, outside = final_grids()
    a, b = common.metrics(model, inside), common.metrics(model, outside)
    with torch.no_grad(): pred = model(torch.tensor(outside[:,None])).numpy().ravel()
    b['absolute_rmse'] = float(np.sqrt(np.mean((pred-common.numerical_reference(outside))**2)))
    return a, b

def export():
    # A record may be exported only after its session is closed. Partial failed
    # sessions are not treated as comparable three-evaluation sessions.
    records = []
    for method in ('agent','gradient'):
        for path in sorted((OUT / method).glob('*.json')):
            r = json.loads(path.read_text(encoding='utf-8'))
            if r['status'] == 'running': raise RuntimeError('Open session: '+str(path))
            row = {'representation':r['representation'],'method':method,'session':r['session'],
                   'status':r['status'],'coefficients':json.dumps(r.get('selected_coefficients')),
                   'selection_loss':r.get('selection_loss'),'adjustable_coefficients':4,
                   'physics_evaluations':len([p for p in r.get('proposals',[]) if 'feedback' in p]) if method=='agent' else r['steps_completed'],
                   'wall_seconds':r.get('wall_seconds'),'model':r.get('requested_model'),
                   'input_tokens':sum(p['usage'].get('input_tokens',0) for p in r.get('proposals',[]) if p.get('usage')) if any(p.get('usage') for p in r.get('proposals',[])) else None,
                   'output_tokens':sum(p['usage'].get('output_tokens',0) for p in r.get('proposals',[]) if p.get('usage')) if any(p.get('usage') for p in r.get('proposals',[])) else None,
                   'cost_usd':None,'error':json.dumps(r.get('error')) if r.get('error') else None}
            if r['status']=='complete' and r.get('selected_coefficients') is not None:
                a,b = final_metrics(r['representation'],r['selected_coefficients'])
                row.update(relative_l2=a['relative_l2'], residual_rmse=a['residual_rmse'], ic_error=a['ic_error'],
                           extrapolation_relative_l2=b['relative_l2'], extrapolation_absolute_rmse=b['absolute_rmse'])
            records.append(row)
    fields = ['representation','method','session','status','coefficients','selection_loss','adjustable_coefficients',
              'physics_evaluations','relative_l2','residual_rmse','ic_error','extrapolation_relative_l2',
              'extrapolation_absolute_rmse','wall_seconds','model','input_tokens','output_tokens','cost_usd','error']
    with (HERE/'representation_results.csv').open('w', newline='', encoding='utf-8') as f:
        writer=csv.DictWriter(f,fieldnames=fields); writer.writeheader(); writer.writerows(records)
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, axes=plt.subplots(1,2,figsize=(13,5),layout='constrained')
    for ax, metric, title in zip(axes,('relative_l2','extrapolation_absolute_rmse'),('In-window relative L2','Extrapolation absolute RMSE')):
        for j, method in enumerate(('agent','gradient')):
            for i,name in enumerate(NAMES):
                values=[r[metric] for r in records if r['representation']==name and r['method']==method and metric in r]
                if values:
                    ax.scatter(np.full(len(values),i+(j-.5)*.2),values,alpha=.55,s=14,color=('tab:blue','tab:orange')[j],label=method if i==0 else None)
        ax.set_yscale('log'); ax.set_title(title); ax.set_xticks(range(len(NAMES)),NAMES,rotation=70,ha='right'); ax.grid(alpha=.2)
    axes[0].legend(); fig.suptitle('Individual completed sessions; agent results absent if unavailable')
    fig.savefig(HERE/'representation_comparison.png',dpi=160); plt.close(fig)
    return records

def main():
    p=argparse.ArgumentParser(); p.add_argument('action',choices=['agent','gradient','export']); p.add_argument('--model',default='gpt-5.4'); p.add_argument('--sessions',type=int,default=10); p.add_argument('--start-session',type=int,default=1); p.add_argument('--only',choices=NAMES); p.add_argument('--workers',type=int,default=1)
    args=p.parse_args()
    if args.action=='export': export(); return
    jobs=[(n,i) for n in NAMES for i in range(args.start_session,args.start_session+args.sessions) if not args.only or n==args.only]
    random.Random(20261124).shuffle(jobs)
    jobs=[(n,i) for n,i in jobs if not (OUT / args.action / f'{n}_{i:02}.json').exists()]
    if args.action=='agent' and args.workers>1:
        if args.workers>8: p.error('workers must be at most 8')
        pending={}; iterator=iter(jobs); blocked=False
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            while True:
                while not blocked and len(pending)<args.workers:
                    try: name,i=next(iterator)
                    except StopIteration: break
                    pending[pool.submit(agent_session,name,i,args.model)]=(name,i)
                if not pending: break
                done,_=wait(pending,return_when=FIRST_COMPLETED)
                for future in done:
                    name,i=pending.pop(future); r=future.result()
                    print(name,i,r['status'],flush=True)
                    if (r.get('error') or {}).get('type') in ('AuthenticationError','PermissionDeniedError','APIConnectionError','NotFoundError','RateLimitError'):
                        blocked=True
            if blocked: print('Stopped scheduling after access or rate limit failure.',flush=True)
        return
    for name,i in jobs:
        r=agent_session(name,i,args.model) if args.action=='agent' else gradient_session(name,i)
        print(name,i,r['status'],flush=True)
        if args.action=='agent' and r.get('error',{}).get('type') in ('AuthenticationError','PermissionDeniedError','APIConnectionError','NotFoundError'):
            print('Stopping after access failure; remaining sessions unrun.',flush=True); break

if __name__=='__main__': main()
