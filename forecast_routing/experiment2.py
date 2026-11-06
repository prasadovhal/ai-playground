"""Experiment 2: cached expanding-origin forecasts and chronological routing."""
from pathlib import Path
import hashlib, inspect, json, time, traceback, warnings
from importlib.metadata import version
import numpy as np
import pandas as pd
import experiment as e

OUT = Path('experiment_2'); CACHE = OUT/'cache'
CACHE.mkdir(parents=True, exist_ok=True)
YEARS = range(2006, 2020)
MODELS = sorted(e.MODELS)
EXP1_VERSIONS={'datasetsforecast':'1.1.0','statsforecast':'2.1.1','neuralforecast':'3.3.0','timesfm':'3.0.2','torch':'2.15.0','numpy':'2.4.6','pandas':'2.3.3'}
EXP1_SOURCE_SHA='09059ddfe88672790a3e92869d66a5ab095b9ae6fbc2213fd66841b315f8d98a'
EXP1_DATA_SHA='afcc4acbe68ea1f47e724119fcdb3df152960163723cf41dfe89332c377e01ac'


def signature(y):
    h = hashlib.sha256(inspect.getsource(e.fit_predict).encode())
    h.update(pd.util.hash_pandas_object(y, index=False).values.tobytes())
    h.update(json.dumps({p: version(p) for p in ['datasetsforecast','statsforecast','neuralforecast','timesfm','torch','numpy','pandas']},sort_keys=True).encode())
    h.update(json.dumps({'H':e.H,'season':e.SEASON,'nbeats_steps':e.NBEATS_STEPS,'seed':e.SEED,'models':MODELS},sort_keys=True).encode())
    return h.hexdigest()


def forecasts_cache(y,s):
    sig=signature(y); logs=[]
    exp1_match=(hashlib.sha256(inspect.getsource(e.fit_predict).encode()).hexdigest()==EXP1_SOURCE_SHA and hashlib.sha256(pd.util.hash_pandas_object(y,index=False).values.tobytes()).hexdigest()==EXP1_DATA_SHA and (e.H,e.SEASON,e.NBEATS_STEPS,e.SEED)==(12,12,100,42) and {p:version(p) for p in EXP1_VERSIONS}==EXP1_VERSIONS)
    old=pd.read_csv('outputs/forecasts.csv',parse_dates=['ds'])
    old=old[old.method.eq('Base')]
    oldlog=pd.read_csv('outputs/run_status.csv')
    for year in YEARS:
        start=pd.Timestamp(year,1,1); dates=pd.date_range(start, periods=12, freq='MS')
        train=y[y.ds<start].copy()
        assert train.ds.max()==start-pd.offsets.MonthBegin(1)
        for model in MODELS:
            path=CACHE/f'{year}_{model}.csv'; meta=path.with_suffix('.json')
            try:
                valid=False
                if path.exists() and meta.exists():
                    info=json.loads(meta.read_text()); valid=info.get('signature')==sig and info.get('forecast_sha256')==hashlib.sha256(path.read_bytes()).hexdigest()
                if valid:
                    pred=pd.read_csv(path,parse_dates=['ds']); source='cache'
                elif year>=2017 and exp1_match:
                    q=old[(old.window==year-2016)&(old.model==model)]
                    entry=oldlog[(oldlog.window==year-2016)&(oldlog.model==model)&(oldlog.status=='ok')]
                    if len(entry)!=1: raise ValueError('Experiment 1 status is not a unique success')
                    check=q[['unique_id','ds','y']].merge(y,on=['unique_id','ds'],suffixes=('_old','_current'),validate='one_to_one')
                    if len(check)!=len(q) or not np.allclose(check.y_old,check.y_current,rtol=1e-10,atol=1e-10): raise ValueError('Experiment 1 actuals differ from current dataset')
                    pred=q[['unique_id','ds','yhat']].copy()
                    info={'signature':sig,'fit_seconds':float(entry.iloc[0].fit_seconds),'inference_seconds':float(entry.iloc[0].inference_seconds),'provenance':'Experiment 1 output; same session configuration, no original immutable config hash'}
                    source='experiment_1'
                else:
                    with warnings.catch_warnings(record=True) as found:
                        warnings.simplefilter('default')
                        pred,fit,infer=e.fit_predict(model,train,dates)
                    summary={}
                    for warning in found:
                        key=str(warning.message); summary[key]=summary.get(key,0)+1
                    info={'signature':sig,'fit_seconds':fit,'inference_seconds':infer,'provenance':'new expanding-origin run','warnings':summary}
                    source='computed'
                expected=pd.MultiIndex.from_product([sorted(s.index),dates],names=['unique_id','ds'])
                got=pd.MultiIndex.from_frame(pred[['unique_id','ds']])
                if got.has_duplicates or set(got)!=set(expected) or not np.isfinite(pred.yhat).all(): raise ValueError('Invalid forecast coverage, dates or values')
                if not valid:
                    pred.to_csv(path,index=False); info['forecast_sha256']=hashlib.sha256(path.read_bytes()).hexdigest(); meta.write_text(json.dumps(info,indent=2))
                logs.append({'year':year,'model':model,'status':'ok','source':source,**info})
                print(year,model,source,'ok',flush=True)
            except Exception as exc:
                logs.append({'year':year,'model':model,'status':'failed','error':repr(exc)})
                print(year,model,'FAILED',repr(exc),flush=True)
            pd.DataFrame(logs).to_csv(OUT/'backtest_status.csv',index=False)
    if any(z['status']!='ok' for z in logs): raise RuntimeError('Incomplete backtests; see experiment_2/backtest_status.csv; rerun to resume')
    return pd.DataFrame(logs)


def features_and_labels(y,s,tags,levels,logs):
    from statsmodels.tsa.seasonal import STL
    rows=[]; labs=[]
    level_names=list(tags); child={}
    for i,lev in enumerate(level_names):
        for uid in tags[lev]:
            parent=s.loc[uid].to_numpy()
            child[uid]=0 if i==len(level_names)-1 else sum(np.all(row<=parent) for row in s.loc[list(tags[level_names[i+1]])].to_numpy())
    for year in YEARS:
        start=pd.Timestamp(year,1,1); history=y[y.ds<start]
        for uid,g in history.groupby('unique_id',sort=True):
            a=g.sort_values('ds').y.to_numpy(dtype=float); missing=float(np.mean(~np.isfinite(a))); a=pd.Series(a).interpolate(limit_direction='both').to_numpy(); a=a[np.isfinite(a)]
            mean=np.mean(a) if len(a) else np.nan; sd=np.std(a) if len(a) else np.nan
            def ac(lag):
                if len(a)<=lag or np.std(a[:-lag])<1e-12 or np.std(a[lag:])<1e-12: return np.nan
                return float(np.corrcoef(a[:-lag],a[lag:])[0,1])
            trend=season=np.nan
            if len(a)>=24 and sd>1e-12:
                z=STL(a,period=12,robust=True).fit(); vr=np.var(z.resid)
                trend=max(0.,1-vr/max(np.var(z.trend+z.resid),1e-12)); season=max(0.,1-vr/max(np.var(z.seasonal+z.resid),1e-12))
            growth=(np.mean(a[-12:])-np.mean(a[-24:-12]))/abs(np.mean(a[-24:-12])) if len(a)>=24 and abs(np.mean(a[-24:-12]))>1e-12 else np.nan
            rows.append({'year':year,'origin':str(start.date()),'unique_id':uid,'series_length':len(g),'trend_strength':trend,'seasonal_strength':season,'std':sd,'cv':sd/abs(mean) if abs(mean)>1e-12 else np.nan,'lag1':ac(1),'lag12':ac(12),'zero_ratio':np.mean(a==0) if len(a) else np.nan,'missing_ratio':missing,'recent_growth':growth,'level':levels[uid],'level_code':level_names.index(levels[uid]),'children':child[uid]})
            denom=np.mean(np.abs(a[12:]-a[:-12])) if len(a)>12 else np.nan
            truth=y[(y.unique_id==uid)&(y.ds>=start)&(y.ds<start+pd.DateOffset(years=1))].sort_values('ds')
            if len(truth)!=12 or not np.isfinite(denom) or denom<=0: raise ValueError(f'Undefined MASE or missing test observations for {uid} {year}')
            for model in MODELS:
                p=pd.read_csv(CACHE/f'{year}_{model}.csv',parse_dates=['ds']); p=p[p.unique_id.eq(uid)].sort_values('ds')
                err=p.yhat.to_numpy()-truth.y.to_numpy(); tm=logs[(logs.year==year)&(logs.model==model)].iloc[0]
                labs.append({'year':year,'unique_id':uid,'model':model,'mae':np.mean(abs(err)),'rmse':np.sqrt(np.mean(err**2)),'mase':np.mean(abs(err))/denom,'scale':denom,'fit_seconds_batch':tm.fit_seconds,'inference_seconds_batch':tm.inference_seconds,'amortized_inference_seconds':tm.inference_seconds/len(s)})
    feat=pd.DataFrame(rows); labels=pd.DataFrame(labs)
    data=feat.merge(labels,on=['year','unique_id'],validate='one_to_many')
    assert len(data)==len(YEARS)*len(s)*len(MODELS) and np.isfinite(data.mase).all()
    data.to_csv(OUT/'feature_labels.csv',index=False)
    return data


FEATURES=['series_length','trend_strength','seasonal_strength','std','cv','lag1','lag12','zero_ratio','missing_ratio','recent_growth','level_code','children']
CONFIGS=[{'num_leaves':5,'max_depth':3,'n_estimators':100,'min_child_samples':40,'reg_lambda':5.0}, {'num_leaves':7,'max_depth':3,'n_estimators':150,'min_child_samples':60,'reg_lambda':10.0}]


def fit_selectors(train,config):
    from lightgbm import LGBMRegressor
    models={}
    for name in MODELS:
        d=train[train.model.eq(name)]
        model=LGBMRegressor(**config,learning_rate=.03,random_state=42,n_jobs=1,verbosity=-1,importance_type='gain')
        model.fit(d[FEATURES],d.mase)
        models[name]=model
    return models


def choice_table(data,train,models,years, include_oracle=True):
    rows=[]; times=[]
    global_model=train.groupby('model').mase.mean().sort_index().idxmin()
    historical=train.groupby(['unique_id','model']).mase.mean().unstack().reindex(columns=MODELS).idxmin(axis=1)
    for year in years:
        q=data[data.year.eq(year)]; feat=q.drop_duplicates('unique_id').sort_values('unique_id')
        start=time.perf_counter()
        scores=pd.DataFrame({name:models[name].predict(feat[FEATURES]) for name in MODELS},index=feat.unique_id.to_numpy())
        elapsed=time.perf_counter()-start
        for uid in feat.unique_id:
            actual=q[q.unique_id.eq(uid)].set_index('model').mase.reindex(MODELS)
            choices={'Global':global_model,'HistoricalBest':historical.loc[uid],'LightGBM':scores.loc[uid].idxmin()}
            if include_oracle: choices['Oracle_base_unattainable']=actual.idxmin()
            for selector,name in choices.items():
                rows.append({'year':year,'unique_id':uid,'selector':selector,'model':name,'predicted_mase':scores.loc[uid,name] if selector=='LightGBM' else np.nan})
            for name in MODELS:
                times.append({'year':year,'unique_id':uid,'candidate':name,'predicted_mase':scores.loc[uid,name], 'selector_batch_seconds':elapsed})
    return pd.DataFrame(rows),pd.DataFrame(times)


def reconcile_choices(choices,data,y,s,tags):
    from hierarchicalforecast.core import HierarchicalReconciliation
    from hierarchicalforecast.methods import BottomUp, MinTrace
    result=[]
    for (year,selector), group in choices.groupby(['year','selector']):
        selected=[]
        for model,items in group.groupby('model'):
            f=pd.read_csv(CACHE/f'{year}_{model}.csv',parse_dates=['ds'])
            selected.append(f[f.unique_id.isin(items.unique_id)])
        f=pd.concat(selected,ignore_index=True)
        if len(f)!=len(s)*12 or f.duplicated(['unique_id','ds']).any() or set(f.unique_id)!=set(s.index): raise ValueError('Incomplete mixed hierarchy')
        f=f.rename(columns={'yhat':'forecast'}); out=[f.assign(method='Base')]
        for method,rec in [('BottomUp',BottomUp()),('MinTrace_wls_struct',MinTrace(method='wls_struct'))]:
            r=HierarchicalReconciliation(reconcilers=[rec]).reconcile(Y_hat_df=f,S_df=s.rename_axis('unique_id').reset_index(),tags=tags)
            col=[c for c in r.columns if c.startswith('forecast/')][0]
            r=r[['unique_id','ds',col]].rename(columns={col:'forecast'})
            p=r.pivot(index='unique_id',columns='ds',values='forecast').reindex(s.index)
            if p.isna().any().any() or not np.allclose(p.to_numpy(),s.to_numpy() @ p.loc[s.columns].to_numpy(),rtol=1e-6,atol=1e-5): raise ValueError('Incoherent mixed forecast')
            out.append(r.assign(method=method))
        result.append(pd.concat(out).assign(year=year,selector=selector))
    f=pd.concat(result,ignore_index=True).merge(y,on=['unique_id','ds'],validate='many_to_one')
    scales=data[['year','unique_id','scale','level']].drop_duplicates()
    f=f.merge(scales,on=['year','unique_id'],validate='many_to_one')
    f['ae']=abs(f.forecast-f.y); f['se']=(f.forecast-f.y)**2
    m=f.groupby(['year','unique_id','level','selector','method'],as_index=False).agg(mae=('ae','mean'),mse=('se','mean'),scale=('scale','first'),n=('y','size'))
    m['rmse']=np.sqrt(m.pop('mse')); m['mase']=m.mae/m.scale
    assert (m.n==12).all() and np.isfinite(m[['mae','rmse','mase']]).all().all()
    return f,m


def evaluate(data,y,s,tags,logs):
    train=data[data.year.between(2006,2015)]
    validation=[]
    for i,config in enumerate(CONFIGS):
        models=fit_selectors(train,config)
        picks,_=choice_table(data,train,models,[2016,2017],include_oracle=False)
        _,m=reconcile_choices(picks[picks.selector.eq('LightGBM')],data,y,s,tags)
        validation.append({'config':i,'validation_mint_mase':m[m.method.eq('MinTrace_wls_struct')].mase.mean(),**config})
    validation=pd.DataFrame(validation).sort_values(['validation_mint_mase','config'])
    validation.to_csv(OUT/'validation.csv',index=False)
    chosen=int(validation.iloc[0]['config']); train=data[data.year.between(2006,2017)]
    t=time.perf_counter(); models=fit_selectors(train,CONFIGS[chosen]); fit_seconds=time.perf_counter()-t
    picks,scores=choice_table(data,train,models,[2018,2019])
    picks.to_csv(OUT/'selector_predictions.csv',index=False); scores.to_csv(OUT/'candidate_predictions.csv',index=False)
    f,m=reconcile_choices(picks,data,y,s,tags)
    f.to_csv(OUT/'mixed_forecasts.csv',index=False); m.to_csv(OUT/'selector_metrics.csv',index=False)
    imp=pd.DataFrame({'feature':FEATURES,**{name:models[name].feature_importances_ for name in MODELS}})
    imp.to_csv(OUT/'feature_importance.csv',index=False)
    summary=m.groupby(['year','selector','method'],as_index=False).agg(mase=('mase','mean'),mae=('mae','mean'),rmse=('rmse','mean'))
    summary.to_csv(OUT/'yearly_comparison.csv',index=False)
    m.groupby(['selector','method'],as_index=False)[['mase','mae','rmse']].mean().to_csv(OUT/'overall_comparison.csv',index=False)
    m.groupby(['year','level','selector','method'],as_index=False)[['mase','mae','rmse']].mean().to_csv(OUT/'level_comparison.csv',index=False)
    picks.merge(data[['year','unique_id','level']].drop_duplicates(),on=['year','unique_id'],validate='many_to_one').groupby(['year','level','selector','model']).size().rename('count').reset_index().to_csv(OUT/'selection_frequency.csv',index=False)
    p=m[m.method.eq('MinTrace_wls_struct')].pivot(index=['year','unique_id'],columns='selector',values='mase')
    comparisons=[]
    for col in p.columns:
        delta=p[col]-p['Global']
        for year in [2018,2019,'all']:
            z=delta if year=='all' else delta.loc[year]
            ref=p['Global'] if year=='all' else p['Global'].loc[year]
            comparisons.append({'selector':col,'year':year,'delta_mase':z.mean(),'relative_change':z.mean()/ref.mean(),'improved':int((z < -1e-12).sum()),'worsened':int((z > 1e-12).sum()),'tied':int((abs(z)<=1e-12).sum())})
    pd.DataFrame(comparisons).to_csv(OUT/'paired_vs_global.csv',index=False)
    opportunity=[]
    for year in [2018,2019,'all']:
        z=p if year=='all' else p.loc[year]
        g=z.Global.mean(); oracle=z.Oracle_base_unattainable.mean(); selector=z.LightGBM.mean()
        opportunity.append({'year':year,'global_mase':g,'base_oracle_after_reconciliation_mase':oracle,'lightgbm_mase':selector,'oracle_gap':g-oracle,'fraction_of_reference_gap':(g-selector)/(g-oracle) if abs(g-oracle)>1e-12 else np.nan})
    pd.DataFrame(opportunity).to_csv(OUT/'oracle_opportunity.csv',index=False)
    t=picks.pivot(index=['year','unique_id'],columns='selector',values='model')
    confusion=pd.crosstab(t['Oracle_base_unattainable'],t['LightGBM'],rownames=['oracle_base'],colnames=['LightGBM']); confusion.to_csv(OUT/'confusion_matrix.csv')
    t.assign(agreement=t.LightGBM.eq(t.Oracle_base_unattainable)).to_csv(OUT/'oracle_agreement.csv')
    # Allocation is an approximation: measured full-hierarchy batch seconds weighted by selected fraction.
    costs=picks.merge(logs[['year','model','inference_seconds']],on=['year','model'],validate='many_to_one')
    costs['allocated_forecast_seconds']=costs.inference_seconds/len(s)
    cost=costs.groupby(['year','selector'],as_index=False).allocated_forecast_seconds.sum()
    sel=scores.groupby('year').selector_batch_seconds.first(); cost['selector_seconds']=cost.apply(lambda r: sel.loc[r.year] if r.selector=='LightGBM' else 0,axis=1)
    cost['total_estimated_seconds']=cost.allocated_forecast_seconds+cost.selector_seconds
    cost.to_csv(OUT/'estimated_costs.csv',index=False)
    cost.merge(summary[summary.method.eq('MinTrace_wls_struct')],on=['year','selector'],validate='one_to_one').to_csv(OUT/'cost_accuracy.csv',index=False)
    (OUT/'run_metadata.json').write_text(json.dumps({'validation_selected':chosen,'config':CONFIGS[chosen],'selector_fit_seconds':fit_seconds,'lightgbm_version':version('lightgbm'),'split':'train 2006-2015; validation 2016-2017; refit 2006-2017; final test 2018-2019','note':'Oracle chooses base errors and is not an oracle for reconciled losses. Forecast inference allocation is estimated, not a measured routed deployment. Experiment 1 had no immutable configuration manifest. AutoARIMA emitted numerical optimizer convergence warnings during the initial historical runs; warning counts were not recorded for those initial cache entries.'},indent=2))
    return picks,scores,f,m,imp,validation,cost
