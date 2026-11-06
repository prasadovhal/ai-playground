"""Exploratory weighting and observability-aware routing cost audit."""
from pathlib import Path
import json,time
import numpy as np
import pandas as pd
from lightgbm import LGBMRegressor
from statsmodels.tsa.seasonal import STL
import experiment4 as e

OUT=Path('experiment_5a'); OUT.mkdir(exist_ok=True)
ORIG=e.OUT
SELECTORS=['GlobalBest','HistoricalBest','Ridge','LGBM_regret_matched','HierarchyLoss']


def load():
    e.freeze_protocol()  # Verifies the original protocol without rewriting it.
    y=pd.read_csv(ORIG/'actual_series.csv').set_index('unique_id'); y.columns=pd.to_datetime(y.columns)
    s=pd.read_csv(ORIG/'summing_matrix.csv').set_index('unique_id')
    metrics=pd.read_csv(ORIG/'selector_metrics.csv'); metrics=metrics[metrics.split.eq('test')]
    candidates=pd.read_csv(ORIG/'forecast_metrics.csv',float_precision='round_trip'); feat=pd.read_csv(ORIG/'selector_features.csv',float_precision='round_trip'); labels=pd.read_csv(ORIG/'counterfactual_labels.csv',float_precision='round_trip')
    choices=pd.read_csv(ORIG/'selector_predictions.csv'); choices=choices[choices.split.eq('test')]
    logs=pd.read_csv(ORIG/'forecast_status.csv'); origins=pd.read_csv(ORIG/'origins.csv',parse_dates=['history_end','target_start','target_end'])
    if len(s)!=84 or len(s.columns)!=36 or len(metrics)!=11*84*5 or not logs.status.eq('ok').all(): raise ValueError('Incomplete Experiment 4 inputs')
    return y,s,metrics,candidates,feat,labels,choices,logs,origins


def objectives(y,s,metrics,origins):
    weights=[]
    for origin,g in metrics.groupby('origin'):
        d=g[g.selector.eq('HistoricalBest')].set_index('unique_id').reindex(s.index)
        valid=d.valid_scale & np.isfinite(d.scale) & (d.scale>0)
        if not valid.all(): raise ValueError('Experiment 4 scale exclusions require explicit common-node handling')
        level=d.level; nlevel=level.nunique()
        volume=y.iloc[:,:int(origin)].mean(axis=1).reindex(s.index)
        candidates={
            'equal_node':pd.Series(1.,index=s.index),
            'half_aggregate_half_leaf':pd.Series(np.where(level.eq('leaf'),.5/level.eq('leaf').sum(),.5/level.ne('leaf').sum()),index=s.index),
            'equal_level':level.map(lambda v:1/(nlevel*level.eq(v).sum())),
            'historical_volume':volume,
        }
        for name,w in candidates.items():
            w=pd.Series(w,index=s.index,dtype=float)
            if not np.isfinite(w).all() or (w<0).any() or w.sum()<=0: raise ValueError('Invalid weights')
            w=w/w.sum()
            weights.extend({'origin':origin,'unique_id':uid,'objective':name,'weight':v} for uid,v in w.items())
    weights=pd.DataFrame(weights);weights.to_csv(OUT/'objective_weights.csv',index=False)
    full=metrics.merge(weights,on=['origin','unique_id'],validate='many_to_many'); full['weighted_mase']=full.mase*full.weight
    per=full.groupby(['origin','objective','selector'],as_index=False).weighted_mase.sum().rename(columns={'weighted_mase':'mase'})
    per=per.merge(origins[['origin','target_start','target_end']],on='origin',validate='many_to_one')
    baseline=per[per.selector.eq('HistoricalBest')][['origin','objective','mase']].rename(columns={'mase':'historical_mase'})
    per=per.merge(baseline,on=['origin','objective'],validate='many_to_one')
    per['difference_vs_historical']=per.mase-per.historical_mase
    per['relative_change_vs_historical']=per.difference_vs_historical/per.historical_mase.where(per.historical_mase.ne(0))
    per['origin_rank']=per.groupby(['origin','objective']).mase.rank(method='min')
    per.to_csv(OUT/'objective_per_origin.csv',index=False)
    result=per.groupby(['objective','selector'],as_index=False).mase.mean()
    best=result.groupby('objective').mase.transform('min'); result['rank']=result.groupby('objective').mase.rank(method='min'); result['relative_to_best']=result.mase/best-1
    result.to_csv(OUT/'objective_comparison.csv',index=False)
    rng=np.random.default_rng(42); records=[]
    for objective,q in per.groupby('objective'):
        p=q.pivot(index='origin',columns='selector',values='mase').sort_index(); n=len(p)
        starts=rng.integers(0,n,size=(5000,int(np.ceil(n/2)))); ix=np.stack([np.concatenate([[j,(j+1)%n] for j in row])[:n] for row in starts])
        for selector in SELECTORS:
            diff=(p[selector]-p.HistoricalBest).to_numpy(); boot=diff[ix].mean(axis=1)
            records.append({'objective':objective,'selector':selector,'paired_difference':diff.mean(),'relative_change':diff.mean()/p.HistoricalBest.mean(),'wins':int((diff<-1e-12).sum()),'losses':int((diff>1e-12).sum()),'ties':int((abs(diff)<=1e-12).sum()),'ci_low':np.quantile(boot,.025),'ci_high':np.quantile(boot,.975),'origins':n})
    intervals=pd.DataFrame(records);intervals.to_csv(OUT/'objective_paired_intervals.csv',index=False)
    original=pd.read_csv(ORIG/'overall_comparison.csv').query("split=='test'").set_index('selector').mase
    audit=result[result.objective.eq('equal_node')].set_index('selector').mase.reindex(original.index)
    if not np.allclose(audit,original,rtol=1e-10,atol=1e-10): raise AssertionError('Original equal-node MASE does not reproduce')
    return weights,per,result,intervals


def tradeoffs(metrics):
    p=metrics.pivot(index=['origin','unique_id','level'],columns='selector',values='mase').reset_index()
    p['difference']=p.HierarchyLoss-p.HistoricalBest;p['gain']=-p.difference
    p.to_csv(OUT/'series_paired_differences.csv',index=False)
    series=p.groupby(['unique_id','level'],as_index=False).agg(mean_difference=('difference','mean'),wins=('gain',lambda a:(a>1e-12).sum()),losses=('gain',lambda a:(a< -1e-12).sum()))
    series['contribution_to_overall']=series.mean_difference/84;series.to_csv(OUT/'series_contributions.csv',index=False)
    levels=series.groupby('level',as_index=False).agg(nodes=('unique_id','size'),mean_difference=('mean_difference','mean'),contribution_to_overall=('contribution_to_overall','sum'))
    levels.to_csv(OUT/'level_contributions.csv',index=False)
    gross=series[series.mean_difference<0].sort_values('mean_difference'); gross['gross_gain_share']=(-gross.mean_difference)/(-gross.mean_difference).sum();gross.to_csv(OUT/'gains_concentration.csv',index=False)
    return p,series,levels,gross


def switching(choices,candidates,origins):
    c=choices.sort_values(['selector','unique_id','origin']).copy();c['previous_model']=c.groupby(['selector','unique_id']).model.shift();c['switched']=c.model.ne(c.previous_model)&c.previous_model.notna()
    selected=candidates[['origin','unique_id','model','mase']].rename(columns={'mase':'selected_mase'})
    previous=selected.rename(columns={'model':'previous_model','selected_mase':'previous_model_mase'})
    c=c.merge(selected,on=['origin','unique_id','model'],validate='many_to_one').merge(previous,on=['origin','unique_id','previous_model'],how='left',validate='many_to_one')
    c['selected_minus_previous']=c.selected_mase-c.previous_model_mase
    c=c.merge(origins[['origin','target_start']],on='origin',validate='many_to_one');c.to_csv(OUT/'switch_events.csv',index=False)
    t=c[c.previous_model.notna()].groupby('selector',as_index=False).agg(transitions=('switched','size'),switches=('switched','sum'),mean_selected_mase=('selected_mase','mean'))
    sw=c[c.switched].groupby('selector',as_index=False).agg(switch_mean_difference=('selected_minus_previous','mean'),better=('selected_minus_previous',lambda a:(a< -1e-12).sum()),worse=('selected_minus_previous',lambda a:(a>1e-12).sum()),tied=('selected_minus_previous',lambda a:(abs(a)<=1e-12).sum()))
    t=t.merge(sw,on='selector',how='left');t.to_csv(OUT/'switch_summary.csv',index=False)
    return c,t


def train_hierarchy(feat,candidates,labels):
    train=feat[feat.split.eq('train')].merge(candidates[['origin','unique_id','model','mase','valid_scale']],on=['origin','unique_id','model'],validate='one_to_one')
    train=train[train.groupby(['origin','unique_id']).valid_scale.transform('all')]
    train=train.merge(labels[['origin','unique_id','model','loss_change']],on=['origin','unique_id','model'],validate='one_to_one')
    models={}; start=time.perf_counter()
    for name in e.MODELS:
        d=train[train.model.eq(name)]; obj=LGBMRegressor(**e.PARAMS);obj.fit(d[e.FEATURES+['recent_candidate_mase']],d.loss_change);models[name]=obj
    return models,time.perf_counter()-start


def replay(y,s,feat,candidates,labels,choices,origins):
    models,fit_time=train_hierarchy(feat,candidates,labels)
    test=origins[origins.split.eq('test')].sort_values('origin'); first=int(test.origin.iloc[0]); leaf=set(s.columns)
    seed=candidates[(candidates.origin<first)&candidates.unique_id.isin(leaf)][['origin','unique_id','model','mase']].copy()
    output=[];observed_log=[];timings=[];feature_log=[]
    for policy in ['full_shadow','periodic_every_3']:
        for selector in ['GlobalBest','HistoricalBest','HierarchyLoss']:
            observed=seed.copy()
            for index,w in enumerate(test.itertuples(index=False)):
                if not observed.empty and not (observed.origin+e.H<=w.origin).all(): raise AssertionError('Unfinished outcome used')
                t=time.perf_counter()
                if selector=='GlobalBest':
                    score=observed.groupby('model').mase.mean().reindex(e.MODELS); name=score.idxmin() if score.notna().any() else 'SeasonalNaive'
                    picked=pd.DataFrame({'unique_id':list(s.columns),'model':name})
                elif selector=='HistoricalBest':
                    score=observed.groupby(['unique_id','model']).mase.mean().unstack().reindex(index=s.columns,columns=e.MODELS);picked=score.idxmin(axis=1).fillna('SeasonalNaive').rename('model').rename_axis('unique_id').reset_index()
                else:
                    a=feat[feat.origin.eq(w.origin)].copy(); vals=[]
                    for row in a.itertuples(index=False):
                        prior=observed[(observed.unique_id.eq(row.unique_id))&(observed.model.eq(row.model))].sort_values('origin').tail(3)
                        vals.append(prior.mase.mean() if not prior.empty else np.nan)
                    a['recent_candidate_mase']=vals; ps=[]
                    feature_log.append(a[['origin','unique_id','model','recent_candidate_mase']].assign(policy=policy,selector=selector))
                    for name in e.MODELS:
                        q=a[a.model.eq(name)].sort_values('unique_id').copy();q['score']=models[name].predict(q[e.FEATURES+['recent_candidate_mase']]);ps.append(q[['unique_id','model','score']])
                    picked=pd.concat(ps).sort_values(['unique_id','score','model']).drop_duplicates('unique_id')[['unique_id','model']]
                elapsed=time.perf_counter()-t; timings.append({'origin':w.origin,'policy':policy,'selector':selector,'decision_seconds_including_history_lookup':elapsed})
                picked=picked.assign(origin=w.origin,policy=policy,selector=selector,shadow=(policy=='full_shadow' or index%3==0));output.append(picked)
                available=candidates[(candidates.origin==w.origin)&candidates.unique_id.isin(leaf)][['origin','unique_id','model','mase']]
                if not picked.shadow.iloc[0]: available=available.merge(picked[['unique_id','model']],on=['unique_id','model'],validate='one_to_one')
                available=available.assign(policy=policy,selector=selector,shadow=bool(picked.shadow.iloc[0]));observed_log.append(available)
                observed=pd.concat([observed,available[['origin','unique_id','model','mase']]],ignore_index=True)
    output=pd.concat(output,ignore_index=True); observed_log=pd.concat(observed_log,ignore_index=True)
    output.to_csv(OUT/'policy_choices.csv',index=False);observed_log.to_csv(OUT/'policy_observed_outcomes.csv',index=False);pd.concat(feature_log).to_csv(OUT/'policy_recent_features.csv',index=False);pd.DataFrame(timings).to_csv(OUT/'policy_decision_timings.csv',index=False)
    base=choices[choices.selector.isin(['GlobalBest','HistoricalBest','HierarchyLoss'])][['origin','unique_id','selector','model']].sort_values(['origin','unique_id','selector']).reset_index(drop=True)
    full=output[output.policy.eq('full_shadow')][['origin','unique_id','selector','model']].sort_values(['origin','unique_id','selector']).reset_index(drop=True)
    if not base.equals(full): raise AssertionError('Full-shadow replay does not reproduce Experiment 4 choices')
    # Check every non-shadow observation belongs to the actual routed selection.
    sparse=observed_log[~observed_log.shadow]; matching=sparse.merge(output[['origin','policy','selector','unique_id','model']],on=['origin','policy','selector','unique_id','model'],validate='one_to_one')
    if len(matching)!=len(sparse): raise AssertionError('Unobserved candidate outcome was used')
    return output,observed_log,fit_time


def policy_accuracy(y,s,choices,candidates,origins):
    out=[]
    for (origin,policy,selector),q in choices.groupby(['origin','policy','selector']):
        cache={}
        for model in q.model.unique():
            f=pd.read_csv(e.CACHE/f'{origin}_{model}.csv',parse_dates=['ds']);cache[model]=f.pivot(index='unique_id',columns='ds',values='yhat').reindex(s.index)
        bottom=np.stack([cache[q.set_index('unique_id').loc[uid,'model']].loc[uid].to_numpy() for uid in s.columns]); aggregate=s.to_numpy()@bottom
        truth=y.iloc[:,int(origin):int(origin)+e.H].to_numpy();history=y.iloc[:,:int(origin)].to_numpy();scale=np.mean(abs(history[:,7:]-history[:,:-7]),axis=1)
        if not (scale>0).all(): raise ValueError('Invalid MASE scale')
        loss=np.mean(abs(aggregate-truth),axis=1)/scale
        out.append({'origin':origin,'policy':policy,'selector':selector,'mase':loss.mean()})
    out=pd.DataFrame(out).merge(origins[['origin','target_start','target_end']],on='origin',validate='many_to_one');out.to_csv(OUT/'policy_accuracy_by_origin.csv',index=False)
    return out


def benchmark_features(y,s,origins):
    rows=[]; test=origins[origins.split.eq('test')].sort_values('origin')
    for origin in test.origin.iloc[[0,len(test)//2,-1]]:
        start=time.perf_counter()
        for uid in s.columns:
            arr=y.loc[uid].iloc[:int(origin)].to_numpy(); sd=arr.std();mu=arr.mean()
            if sd>1e-12:
                d=STL(arr,period=7,robust=True).fit();vr=np.var(d.resid);_=(max(0.,1-vr/max(np.var(d.trend+d.resid),1e-12)),max(0.,1-vr/max(np.var(d.seasonal+d.resid),1e-12)))
            for lag in [1,7]:
                if np.std(arr[:-lag])>1e-12 and np.std(arr[lag:])>1e-12: _=np.corrcoef(arr[:-lag],arr[lag:])[0,1]
            _=(sd/abs(mu) if abs(mu)>1e-12 else np.nan,np.mean(arr==0),arr[-28:].mean(),arr[-56:-28].mean())
        rows.append({'origin':int(origin),'leaves':len(s.columns),'feature_seconds':time.perf_counter()-start})
    out=pd.DataFrame(rows);out.to_csv(OUT/'feature_benchmark.csv',index=False);return out


def costs(choices,logs,feature_bench,fit_time):
    rows=[]; t=logs.set_index(['origin','model']); initial=logs[(logs.model.eq('TimesFM3'))&(logs.fit_seconds>0.1)].fit_seconds
    init=float(initial.median()) if len(initial) else np.nan; feature_seconds=feature_bench.feature_seconds.median()
    for (origin,policy,selector),q in choices.groupby(['origin','policy','selector']):
        shadow=bool(q.shadow.iloc[0]);counts=q.model.value_counts(); active=e.MODELS if shadow else list(counts.index)
        fit_low=fit_high=infer_low=infer_high=0.
        for model in active:
            row=t.loc[(origin,model)]; fraction=1. if shadow else counts[model]/36
            # N-BEATS remains trained on all 36 series to reproduce the cached global model.
            if model!='TimesFM3':
                fit_high+=row.fit_seconds;fit_low+=row.fit_seconds*(1. if model=='NBEATS' else fraction)
            infer_high+=row.inference_seconds;infer_low+=row.inference_seconds*fraction
        routed=0.
        for model,count in counts.items():
            r=t.loc[(origin,model)];frac=count/36
            routed+=r.inference_seconds*frac
            if model!='TimesFM3': routed+=r.fit_seconds*(1. if model=='NBEATS' else frac)
        monitoring_extra=max(0.,fit_low+infer_low-routed) if shadow else 0.
        rows.append({'origin':origin,'policy':policy,'selector':selector,'shadow':shadow,'active_models':len(active),'fit_linear_estimate':fit_low,'fit_full_batch_scenario':fit_high,'inference_linear_estimate':infer_low,'inference_full_batch_scenario':infer_high,'shadow_monitoring_increment_linear_estimate':monitoring_extra,'feature_benchmark_seconds':feature_seconds if selector=='HierarchyLoss' else 0.,'timesfm_initialization_once_median':init,'note':'Per-model batch durations were measured under contention. Serial-sum scenarios are not measured routed wall-clock latency or strict bounds.'})
    out=pd.DataFrame(rows);out.to_csv(OUT/'policy_cost_by_origin.csv',index=False)
    totals=out.groupby(['policy','selector'],as_index=False)[['fit_linear_estimate','fit_full_batch_scenario','inference_linear_estimate','inference_full_batch_scenario','shadow_monitoring_increment_linear_estimate','feature_benchmark_seconds']].sum()
    totals['timesfm_initialization_once_median']=init;totals['selector_fit_once_seconds']=np.where(totals.selector.eq('HierarchyLoss'),fit_time,0.)
    decision=pd.read_csv(OUT/'policy_decision_timings.csv').groupby(['policy','selector'],as_index=False).decision_seconds_including_history_lookup.sum()
    totals=totals.merge(decision,on=['policy','selector'],validate='one_to_one')
    common=totals.feature_benchmark_seconds+totals.timesfm_initialization_once_median+totals.selector_fit_once_seconds+totals.decision_seconds_including_history_lookup
    totals['total_linear_scenario']=totals.fit_linear_estimate+totals.inference_linear_estimate+common
    totals['total_full_batch_scenario']=totals.fit_full_batch_scenario+totals.inference_full_batch_scenario+common
    totals.to_csv(OUT/'policy_cost_totals.csv',index=False)
    logs[logs.split.ne('test')].groupby(['split','model'],as_index=False)[['fit_seconds','inference_seconds']].sum().to_csv(OUT/'pretest_candidate_cost_sunk.csv',index=False)
    (OUT/'assumptions.json').write_text(json.dumps({'objective_volume':'mean unit sales per node over entire pre-origin history, normalized across nodes; grouped hierarchy counts overlapping sales multiple times','bootstrap':'paired origin differences, circular blocks of 2, 5000 resamples, seed 42','periodic':'full candidate shadow on test origins 1,4,7,10; otherwise observe only the model selected per leaf; every selector has its own observation history','initial_history':'all 25 training and 5 validation windows are fully observed before test','recent_performance':'last three available observations for each leaf and candidate, not unexecuted candidate errors','nbeats_cost':'global 36-leaf model is refit in full whenever used to reproduce cached forecasts','feature_cost':'median of three measured 36-leaf history feature computations; excludes data loading, full operational feature pipeline, and reconciliation','cost_scenarios':'serial sums of measured 36-leaf batch wall times: linear per-leaf allocation vs full batch for every active model; not wall-clock measurements or strict bounds','initialization':'median of recorded positive TimesFM load times once per standalone policy; workload contention affects times','omissions':'cache storage, orchestration, network, data loading, reconciliation, and prior 30-origin monitoring shown separately; no speedup claim'},indent=2))
    return out,totals
