"""Australian Labour rolling-origin experiment; called by experiment.ipynb."""
import json
import os
import platform
import random
import time
import traceback
from importlib.metadata import version, PackageNotFoundError
from pathlib import Path

import numpy as np
import pandas as pd

H, WINDOWS, SEASON, SEED = 12, 3, 12, 42
NBEATS_STEPS = 100
MODELS = ['SeasonalNaive', 'AutoARIMA', 'NBEATS', 'TimesFM3']
OUT = Path('outputs')
OUT.mkdir(exist_ok=True)
random.seed(SEED)
np.random.seed(SEED)


def environment():
    print('Python:', platform.python_version(), 'platform:', platform.platform(), 'CPU:', platform.processor(), 'cores:', os.cpu_count())
    for p in ['datasetsforecast', 'statsforecast', 'hierarchicalforecast', 'neuralforecast', 'timesfm', 'torch', 'numpy', 'pandas']:
        try: print(p, version(p))
        except PackageNotFoundError: print(p, 'NOT INSTALLED')
    try:
        import torch, psutil
        print('RAM GB:', round(psutil.virtual_memory().total / 2**30, 2), 'CUDA:', torch.cuda.is_available(), 'GPU:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'none')
        torch.manual_seed(SEED)
    except ImportError as e: print('Hardware details unavailable:', e)


def load_data():
    from datasetsforecast.hierarchical import HierarchicalData
    y, s, tags = HierarchicalData.load(directory='data', group='Labour')
    y['ds'] = pd.to_datetime(y.ds)
    s.index = s.index.astype(str)
    s.columns = s.columns.astype(str)
    if 'unique_id' in s.columns: s = s.set_index('unique_id')
    ids = list(s.index)
    levels = {str(i): k for k, v in tags.items() for i in v}
    def check(ok, msg):
        if not ok: raise ValueError('Invalid hierarchy: ' + msg)
    check(len(ids) == 57 and len(tags) == 4, f'expected 57 series and 4 levels; got {len(ids)} and {len(tags)}')
    check(s.index.is_unique and s.columns.is_unique and set(ids) == set(y.unique_id) == set(levels), 'IDs or levels mismatch')
    check(sum(map(len, tags.values())) == len(levels), 'overlapping level tags')
    check(not y[['unique_id','ds','y']].isna().any().any() and np.isfinite(y.y).all(), 'missing or nonfinite observations')
    check(not y.duplicated(['unique_id','ds']).any(), 'duplicate observations')
    a = s.to_numpy(dtype=float)
    check(np.isfinite(a).all() and np.isin(a, [0,1]).all(), 'invalid summing matrix')
    check(set(s.columns).issubset(ids), 'bottom IDs missing')
    check(np.array_equal(s.loc[s.columns].to_numpy(), np.eye(len(s.columns))), 'bottom identity missing or misordered')
    level_arrays = [s.loc[list(v)].to_numpy(dtype=float) for v in tags.values()]
    for i, level in enumerate(level_arrays):
        check(np.array_equal(level.sum(axis=0), np.ones(len(s.columns))), f'level {i} does not partition bottom series')
    for parent, child in zip(level_arrays, level_arrays[1:]):
        for row in child:
            check(sum(np.all(row <= p) for p in parent) == 1, 'levels are not a nested hierarchy')
    dates = pd.date_range(y.ds.min(), y.ds.max(), freq='MS')
    p = y.pivot(index='unique_id', columns='ds', values='y').reindex(index=ids, columns=dates)
    check(not p.isna().any().any(), 'series do not share a complete monthly calendar')
    check(np.allclose(p.to_numpy(), a @ p.loc[s.columns].to_numpy(), rtol=1e-5, atol=1e-3), 'data do not aggregate according to S')
    check(len(dates) > H * WINDOWS + 2 * SEASON, 'insufficient training history')
    print('Rows:', len(y), 'series:', len(ids), 'months:', len(dates), 'dates:', dates[0], 'to', dates[-1], 'missing:', y.isna().sum().to_dict())
    print('Hierarchy:', {k: len(v) for k,v in tags.items()}, 'S shape:', s.shape)
    return y.sort_values(['unique_id','ds']), s, tags, levels, dates


def fit_predict(model, train, dates):
    start = time.perf_counter()
    if model in ('SeasonalNaive','AutoARIMA'):
        from statsforecast import StatsForecast
        from statsforecast.models import SeasonalNaive, AutoARIMA
        obj = StatsForecast(models=[SeasonalNaive(season_length=SEASON) if model == 'SeasonalNaive' else AutoARIMA(season_length=SEASON, approximation=True, max_p=2, max_q=2, max_P=1, max_Q=1)], freq='MS', n_jobs=1)
        obj.fit(train)
        fit = time.perf_counter() - start
        start = time.perf_counter(); pred = obj.predict(h=H).reset_index()
        pred = pred[['unique_id','ds',model]].rename(columns={model:'yhat'})
    elif model == 'NBEATS':
        from neuralforecast import NeuralForecast
        from neuralforecast.models import NBEATS
        obj = NeuralForecast(models=[NBEATS(h=H, input_size=2*H, max_steps=NBEATS_STEPS, mlp_units=[[128,128]]*3, scaler_type='robust', random_seed=SEED, enable_progress_bar=False, logger=False, enable_model_summary=False)], freq='MS')
        obj.fit(df=train)
        fit = time.perf_counter() - start
        start = time.perf_counter(); pred = obj.predict().reset_index()
        pred = pred[['unique_id','ds','NBEATS']].rename(columns={'NBEATS':'yhat'})
    else:
        import torch
        from timesfm3 import TimesFM3Evaluator, ModelConfig
        device = 'cuda' if torch.cuda.is_available() else 'cpu'
        obj = TimesFM3Evaluator(ModelConfig(checkpoint_path='google/timesfm-3.0-pytorch', per_core_batch_size=4, device=device))
        fit = time.perf_counter() - start  # checkpoint loading, not training
        start = time.perf_counter()
        groups = list(train.groupby('unique_id', sort=True))
        values = list(obj.predict_batch([g.y.to_numpy(dtype=np.float32) for _,g in groups], horizon=H, return_quantiles=False, use_symmetric_averaging=False))
        pred = pd.concat([pd.DataFrame({'unique_id': uid, 'ds': dates, 'yhat': np.asarray(v.forecast)}) for (uid,_),v in zip(groups,values)], ignore_index=True)
    infer = time.perf_counter() - start
    pred.ds = pd.to_datetime(pred.ds)
    return pred, fit, infer


def run(y, s, tags, levels, dates):
    from hierarchicalforecast.core import HierarchicalReconciliation
    from hierarchicalforecast.methods import BottomUp, MinTrace
    predictions, logs, scales = [], [], []
    ids = set(s.index)
    for w in range(WINDOWS):
        end = len(dates) - H * (WINDOWS-w)
        train = y[y.ds < dates[end]].copy(); future = dates[end:end+H]
        print('Window', w+1, 'train through', dates[end-1].date(), 'test', future[0].date(), 'to', future[-1].date(), flush=True)
        for uid,g in train.groupby('unique_id'):
            x=g.y.to_numpy(); denom=np.mean(np.abs(x[SEASON:]-x[:-SEASON]))
            scales.append({'window':w+1,'unique_id':uid,'scale':denom})
        for model in MODELS:
            log={'window':w+1,'model':model,'status':'failed','fit_seconds':np.nan,'inference_seconds':np.nan,'error':''}
            try:
                pred, fit, infer = fit_predict(model, train, future)
                expected = pd.MultiIndex.from_product([sorted(ids),future], names=['unique_id','ds'])
                got = pd.MultiIndex.from_frame(pred[['unique_id','ds']])
                if got.has_duplicates or set(got) != set(expected) or not np.isfinite(pred.yhat).all():
                    raise ValueError(f'Forecast coverage invalid: expected {len(expected)}, got {len(got)}; missing {len(expected.difference(got))}')
                log.update(status='ok', fit_seconds=fit, inference_seconds=infer)
                base=pred.assign(window=w+1,model=model,method='Base')
                predictions.append(base)
                for method, reconciler in [('BottomUp',BottomUp()),('MinTrace_wls_struct',MinTrace(method='wls_struct'))]:
                    try:
                        hrec=HierarchicalReconciliation(reconcilers=[reconciler])
                        inp=pred.rename(columns={'yhat':'forecast'})
                        sdf=s.rename_axis('unique_id').reset_index()
                        rec=hrec.reconcile(Y_hat_df=inp, S_df=sdf, tags=tags)
                        col=[c for c in rec.columns if c.startswith('forecast/')][0]
                        rec=rec[['unique_id','ds',col]].rename(columns={col:'yhat'})
                        pivot=rec.pivot(index='unique_id',columns='ds',values='yhat').reindex(index=s.index,columns=future)
                        if pivot.isna().any().any() or not np.allclose(pivot.to_numpy(),s.to_numpy() @ pivot.loc[s.columns].to_numpy(),rtol=1e-6,atol=1e-5): raise ValueError('Reconciled forecasts are incoherent or incomplete')
                        predictions.append(rec.assign(window=w+1,model=model,method=method))
                    except Exception as e:
                        logs.append({'window':w+1,'model':model,'status':'reconciliation_failed','method':method,'error':repr(e)})
                        print(model,method,'FAILED:',repr(e),flush=True)
            except Exception as e:
                log['error']=repr(e); print(model,'FAILED:',repr(e),flush=True)
            logs.append(log); print(model,log['status'],flush=True)
            pd.DataFrame(logs).to_csv(OUT/'run_status.csv',index=False)
    if not predictions: raise RuntimeError('No successful model forecasts; see outputs/run_status.csv')
    forecasts=pd.concat(predictions,ignore_index=True).merge(y,on=['unique_id','ds'],validate='many_to_one')
    forecasts['level']=forecasts.unique_id.map(levels)
    forecasts.to_csv(OUT/'forecasts.csv',index=False)
    scales=pd.DataFrame(scales); forecasts=forecasts.merge(scales,on=['window','unique_id'],validate='many_to_one')
    forecasts['abs_error']=(forecasts.yhat-forecasts.y).abs(); forecasts['sq_error']=(forecasts.yhat-forecasts.y)**2
    metrics=forecasts.groupby(['window','unique_id','level','model','method'],as_index=False).agg(mae=('abs_error','mean'),mse=('sq_error','mean'),scale=('scale','first'),n=('y','size'))
    metrics['rmse']=np.sqrt(metrics.pop('mse')); metrics['mase']=metrics.mae/metrics.scale.where(metrics.scale>0)
    times=pd.DataFrame(logs).query("status == 'ok'")[['window','model','inference_seconds']].copy()
    times['amortized_inference_seconds_per_series']=times.pop('inference_seconds')/len(ids)
    metrics=metrics.merge(times,on=['window','model'],validate='many_to_one')
    assert (metrics.n==H).all() and not metrics.duplicated(['window','unique_id','model','method']).any()
    metrics.to_csv(OUT/'metrics.csv',index=False)
    return forecasts, metrics, pd.DataFrame(logs)


def analyze(forecasts, metrics, logs):
    from IPython.display import display
    import matplotlib.pyplot as plt
    print('Run status (fit time includes checkpoint loading for TimesFM):'); display(logs)
    print('Per-series metrics:'); display(metrics)
    # Comparisons require identical complete model/method coverage in each window.
    ok=logs[logs.status.eq('ok')].groupby('model').window.nunique()
    eligible=sorted(ok[ok.eq(WINDOWS)].index)
    print('Models successful in every window:', eligible, '; excluded:', sorted(set(MODELS)-set(eligible)))
    if not eligible: print('No comparable models.'); return
    m=metrics[metrics.model.isin(eligible)].copy()
    counts=m.groupby(['window','unique_id','method']).model.nunique()
    complete=counts[counts.eq(len(eligible))].index
    m=m.set_index(['window','unique_id','method']).loc[complete].reset_index()
    m=m[np.isfinite(m.mase)]
    valid=m.groupby(['window','unique_id','method']).model.nunique(); complete=valid[valid.eq(len(eligible))].index
    m=m.set_index(['window','unique_id','method']).loc[complete].reset_index()
    if m.empty: print('No complete finite MASE comparisons.'); return
    winners=m.sort_values(['mase','model']).drop_duplicates(['window','unique_id','method'])
    winners.to_csv(OUT/'winners.csv',index=False)
    print('Best per series and window (MASE; alphabetical tie-break):'); display(winners)
    print('Win counts by method, level and model:'); wins=winners.groupby(['method','level','model']).size().rename('wins').reset_index(); display(wins); wins.to_csv(OUT/'win_counts.csv',index=False)
    base=winners[winners.method.eq('Base')]; base.model.value_counts().plot.bar(title='Base forecast wins (MASE)'); plt.ylabel('Series-windows'); plt.show()
    print('Mean MASE by model/method (equal series-window weighting):'); comparison=m.groupby(['method','model']).mase.agg(['mean','count']); display(comparison); comparison.to_csv(OUT/'comparison.csv')
    print('Paired reconciliation changes in MASE (same model, series and window; negative improves):')
    paired_errors=m.pivot_table(index=['window','unique_id','model'],columns='method',values='mase',aggfunc='first')
    for method in ['BottomUp','MinTrace_wls_struct']:
        if method in paired_errors and 'Base' in paired_errors:
            pair=paired_errors[['Base',method]].dropna().copy()
            pair['change']=pair[method]-pair['Base']
            display(pair.groupby('model').agg(n=('change','size'),base=('Base','mean'),reconciled=(method,'mean'),change=('change','mean')))
            pair.to_csv(OUT/f'paired_{method}.csv')
    b=m[m.method.eq('Base')]; global_scores=b.groupby('model').mase.mean(); oracle=base.mase.mean()
    print('Global model:',global_scores.idxmin(),'mean MASE:',global_scores.min(),'oracle mean MASE:',oracle,'relative oracle gain:',1-oracle/global_scores.min() if global_scores.min() else np.nan)
    print('Oracle selects using the actual test errors: unattainable reference, not a deployable selector. The global model is also selected retrospectively.')
    stability=base.groupby('unique_id').model.agg(lambda x: x.nunique()).value_counts().sort_index(); print('Distinct winners across windows per series:'); display(stability)
    paired=winners.pivot_table(index=['window','unique_id'],columns='method',values='model',aggfunc='first'); print('Winner changes after reconciliation:'); display(paired)
    for method in ['BottomUp','MinTrace_wls_struct']:
        if method in paired and 'Base' in paired:
            p=paired[['Base',method]].dropna()
            print(method, 'winner changed:', int((p['Base'] != p[method]).sum()), 'of', len(p), 'matched series-windows')
    print('Inference and fit times are total batch wall time, not per-series measurements:'); display(logs[logs.status.eq('ok')].groupby('model')[['fit_seconds','inference_seconds']].mean())
    logs[logs.status.eq('ok')].groupby('model')[['fit_seconds','inference_seconds']].mean().plot.bar(title='Mean wall time per window (seconds)'); plt.ylabel('Seconds'); plt.show()
    ex=forecasts[(forecasts.unique_id.eq(sorted(forecasts.unique_id.unique())[0])) & (forecasts.window.eq(1)) & (forecasts.method.eq('Base'))]
    if not ex.empty:
        for name,g in ex.groupby('model'): plt.plot(g.ds,g.yhat,label=name)
        g=ex.drop_duplicates('ds'); plt.plot(g.ds,g.y,label='Actual',color='black'); plt.legend(); plt.title('Example: '+str(g.unique_id.iloc[0])); plt.xticks(rotation=45); plt.show()
    print('Interpretation: assess effect sizes, winner stability and failures before considering a selector. Three windows on one dataset are exploratory, not proof of deployable gains. Compare reconciliation using matched series-windows; MASE has equal series weight, unlike pooled MAE.')
