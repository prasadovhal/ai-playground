"""Experiment 2B: exploratory diagnosis and leaf-only routing."""
from pathlib import Path
import json, time
import numpy as np
import pandas as pd
from lightgbm import LGBMRegressor, LGBMRanker
from sklearn.pipeline import make_pipeline
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import Ridge
import experiment2 as x

OUT=Path('experiment_2b'); OUT.mkdir(exist_ok=True)
MODELS=x.MODELS
BASE=[c for c in x.FEATURES if c not in ('series_length','level_code','children')]
CONFIGS={
    'LGBM_raw_full':('raw',x.FEATURES),
    'LGBM_raw_no_length':('raw',[c for c in x.FEATURES if c!='series_length']),
    'LGBM_raw_no_length_no_hierarchy':('raw',BASE),
    'LGBM_regret_no_length':('regret',[c for c in x.FEATURES if c!='series_length']),
    'LGBM_rank_no_length':('rank',[c for c in x.FEATURES if c!='series_length']),
}
PARAMS=dict(num_leaves=7,max_depth=3,n_estimators=150,min_child_samples=40,reg_lambda=10.,learning_rate=.03,random_state=42,n_jobs=1,verbosity=-1)


def load():
    y,s,tags,levels,dates=x.e.load_data()
    data=pd.read_csv(x.OUT/'feature_labels.csv')
    if len(data)!=14*57*4 or data.duplicated(['year','unique_id','model']).any(): raise ValueError('Incomplete Experiment 2 feature table')
    logs=pd.read_csv(x.OUT/'backtest_status.csv')
    if len(logs)!=56 or not logs.status.eq('ok').all(): raise ValueError('Incomplete original cached forecasts')
    # Verify cache against current data and source; never regenerate candidate forecasts.
    sig=x.signature(y)
    import hashlib
    for year in x.YEARS:
        for model in MODELS:
            path=x.CACHE/f'{year}_{model}.csv'; m=json.loads(path.with_suffix('.json').read_text())
            if m['signature']!=sig or m['forecast_sha256']!=hashlib.sha256(path.read_bytes()).hexdigest(): raise ValueError(f'Invalid cache: {path}')
    return y,s,tags,levels,data,logs


def diagnose(s,tags):
    old=pd.read_csv(x.OUT/'selector_metrics.csv')
    piv=old.pivot(index=['year','unique_id','level'],columns=['selector','method'],values='mase')
    out=pd.DataFrame(index=piv.index)
    for selector in ['LightGBM','HistoricalBest','Global']:
        out[selector+'_base']=piv[(selector,'Base')];out[selector+'_mint']=piv[(selector,'MinTrace_wls_struct')]
        out[selector+'_mint_correction']=out[selector+'_mint']-out[selector+'_base']
    out['lgbm_minus_historical_mint']=out.LightGBM_mint-out.HistoricalBest_mint
    out['lgbm_minus_historical_base']=out.LightGBM_base-out.HistoricalBest_base
    out=out.reset_index().merge(pd.read_csv(x.OUT/'selector_predictions.csv').pivot(index=['year','unique_id'],columns='selector',values='model').reset_index(),on=['year','unique_id'])
    out.to_csv(OUT/'experiment2_series_diagnostics.csv',index=False)
    f=pd.read_csv(x.OUT/'mixed_forecasts.csv',parse_dates=['ds']); f=f[f.selector.isin(['Global','HistoricalBest','LightGBM'])]
    p=f.pivot(index=['year','selector','unique_id','ds'],columns='method',values='forecast').reset_index().merge(f[['year','selector','unique_id','ds','y']].drop_duplicates(),on=['year','selector','unique_id','ds'],validate='one_to_one')
    p['base_residual']=p.Base-p.y; p['mint_residual']=p.MinTrace_wls_struct-p.y; p['correction']=p.MinTrace_wls_struct-p.Base
    picks=pd.read_csv(x.OUT/'selector_predictions.csv')
    edges=[]; names=list(tags)
    for j in range(len(names)-1):
        for parent in tags[names[j]]:
            a=s.loc[parent].to_numpy()
            for child in tags[names[j+1]]:
                if np.all(s.loc[child].to_numpy()<=a): edges.append((parent,child,names[j],names[j+1]))
    edge=pd.DataFrame(edges,columns=['parent','child','parent_level','child_level'])
    par=p.rename(columns={'unique_id':'parent','base_residual':'parent_residual','correction':'parent_correction'})
    chi=p.rename(columns={'unique_id':'child','base_residual':'child_residual','correction':'child_correction'})
    pairs=edge.merge(par[['parent','year','selector','ds','parent_residual','parent_correction']],on='parent').merge(chi[['child','year','selector','ds','child_residual','child_correction']],on=['child','year','selector','ds'])
    pp=picks.rename(columns={'unique_id':'parent','model':'parent_model'}); cp=picks.rename(columns={'unique_id':'child','model':'child_model'})
    pairs=pairs.merge(pp[['year','selector','parent','parent_model']],on=['year','selector','parent'],validate='many_to_one').merge(cp[['year','selector','child','child_model']],on=['year','selector','child'],validate='many_to_one')
    pairs.to_csv(OUT/'parent_child_monthly_residuals.csv',index=False)
    mismatch=pairs.groupby(['year','selector','parent','parent_level','parent_model','ds'],as_index=False).agg(parent_residual=('parent_residual','first'),children_residual_sum=('child_residual','sum'),parent_correction=('parent_correction','first'),children=('child','nunique'),child_model_count=('child_model','nunique'))
    mismatch['incoherence']=mismatch.parent_residual-mismatch.children_residual_sum
    mismatch.to_csv(OUT/'parent_residual_vs_children.csv',index=False)
    mismatch.assign(abs_incoherence=lambda a:a.incoherence.abs(),abs_correction=lambda a:a.parent_correction.abs()).groupby(['year','selector','parent_level'],as_index=False)[['abs_incoherence','abs_correction']].mean().to_csv(OUT/'residual_discrepancy_summary.csv',index=False)
    def corr(g):
        a,b=g.parent_residual.to_numpy(),g.child_residual.to_numpy()
        return np.corrcoef(a,b)[0,1] if np.std(a)>1e-12 and np.std(b)>1e-12 else np.nan
    rel=pairs.groupby(['year','selector','parent','child','parent_level','child_level','parent_model','child_model'],as_index=False).apply(lambda g:pd.Series({'residual_correlation':corr(g),'parent_residual_mae':g.parent_residual.abs().mean(),'child_residual_mae':g.child_residual.abs().mean(),'parent_correction_mae':g.parent_correction.abs().mean(),'child_correction_mae':g.child_correction.abs().mean()}),include_groups=False).reset_index(drop=True)
    rel.to_csv(OUT/'parent_child_relationships.csv',index=False)
    comb=out.groupby(['LightGBM','HistoricalBest'],as_index=False).agg(n=('unique_id','size'),base_gap=('lgbm_minus_historical_base','mean'),mint_gap=('lgbm_minus_historical_mint','mean'),lgbm_correction=('LightGBM_mint_correction','mean'),historical_correction=('HistoricalBest_mint_correction','mean'))
    comb.to_csv(OUT/'selection_pair_diagnostics.csv',index=False)
    return out,rel,comb


def fit_predict(train, test, kind, columns):
    pred={}; start=time.perf_counter()
    if kind=='rank':
        d=train.sort_values(['year','unique_id','model']).copy()
        d['relevance']=3-(d.groupby(['year','unique_id']).mase.rank(method='first',ascending=True).astype(int)-1)
        def frame(z):
            v=z[columns].reset_index(drop=True).copy()
            for name in MODELS: v['model_'+name]=(z.model.to_numpy()==name).astype(int)
            return v
        ranker=LGBMRanker(**PARAMS); ranker.fit(frame(d),d.relevance.to_numpy(),group=[4]*(len(d)//4))
        fit=time.perf_counter()-start; start=time.perf_counter()
        q=test.sort_values(['year','unique_id','model']).copy(); q['score']=ranker.predict(frame(q)); q=q.sort_values(['year','unique_id','score','model'],ascending=[True,True,False,True]).drop_duplicates(['year','unique_id'])
        return q[['year','unique_id','model']],fit,time.perf_counter()-start
    d=train.copy()
    if kind=='regret': d['target']=d.mase-d.groupby(['year','unique_id']).mase.transform('min')
    else: d['target']=d.mase
    models={}
    for model in MODELS:
        a=d[d.model.eq(model)]
        obj=make_pipeline(SimpleImputer(strategy='median',keep_empty_features=True),StandardScaler(),Ridge(alpha=10.)) if kind=='ridge' else LGBMRegressor(**PARAMS)
        obj.fit(a[columns],a.target); models[model]=obj
    fit=time.perf_counter()-start; start=time.perf_counter()
    feat=test.drop_duplicates(['year','unique_id']).sort_values(['year','unique_id']).copy()
    for model in MODELS: feat[model]=models[model].predict(feat[columns])
    feat['model']=feat[MODELS].idxmin(axis=1)
    return feat[['year','unique_id','model']],fit,time.perf_counter()-start


def baselines(train,test):
    global_model=train.groupby('model').mase.mean().sort_index().idxmin()
    hist=train.groupby(['unique_id','model']).mase.mean().unstack().reindex(columns=MODELS).idxmin(axis=1)
    p=test[['year','unique_id']].drop_duplicates().sort_values(['year','unique_id'])
    return {'Global':p.assign(model=global_model),'HistoricalBest':p.assign(model=p.unique_id.map(hist))}


def evaluate_choices(choices,data,y,s,levels,logs):
    allf=[]; allm=[]
    for (year,selector),choice in choices.groupby(['year','selector']):
        leaf=[]
        for model,part in choice.groupby('model'):
            p=pd.read_csv(x.CACHE/f'{year}_{model}.csv',parse_dates=['ds']); leaf.append(p[p.unique_id.isin(part.unique_id)])
        leaf=pd.concat(leaf); expected=pd.MultiIndex.from_product([s.columns,pd.date_range(f'{year}-01-01',periods=12,freq='MS')],names=['unique_id','ds'])
        if len(leaf)!=len(expected) or set(pd.MultiIndex.from_frame(leaf[['unique_id','ds']]))!=set(expected): raise ValueError('Incomplete bottom forecasts')
        matrix=leaf.pivot(index='unique_id',columns='ds',values='yhat').reindex(s.columns)
        aggregate=s.to_numpy()@matrix.to_numpy()
        f=pd.DataFrame(aggregate,index=s.index,columns=matrix.columns).rename_axis('unique_id').stack().rename('forecast').reset_index()
        check=f.pivot(index='unique_id',columns='ds',values='forecast').reindex(s.index)
        assert np.allclose(check.to_numpy(),s.to_numpy()@check.loc[s.columns].to_numpy())
        f=f.assign(year=year,selector=selector,method='BottomUp')
        # Before aggregation only the selected bottom base forecasts exist.
        raw=leaf.rename(columns={'yhat':'forecast'}).assign(year=year,selector=selector,method='LeafBase')
        allf.append(pd.concat([f,raw],ignore_index=True))
    forecasts=pd.concat(allf,ignore_index=True).merge(y,on=['unique_id','ds'],validate='many_to_one')
    scales=data[['year','unique_id','scale','level']].drop_duplicates()
    forecasts=forecasts.merge(scales,on=['year','unique_id'],validate='many_to_one')
    forecasts['ae']=abs(forecasts.forecast-forecasts.y); forecasts['se']=(forecasts.forecast-forecasts.y)**2
    m=forecasts.groupby(['year','selector','method','level','unique_id'],as_index=False).agg(mae=('ae','mean'),mse=('se','mean'),scale=('scale','first'),n=('y','size'))
    m['mase']=m.mae/m.scale; m['rmse']=np.sqrt(m.pop('mse'))
    assert m.n.eq(12).all() and np.isfinite(m.mase).all()
    return forecasts,m


def run(data,y,s,levels,logs):
    leaf=data[data.unique_id.isin(s.columns)].copy()
    train=leaf[leaf.year.between(2006,2015)]; val=leaf[leaf.year.between(2016,2017)]
    base=baselines(train,val); choices=[]; timings=[]
    for name,p in base.items(): choices.append(p.assign(selector=name))
    for name,(kind,cols) in {**CONFIGS,'Ridge_raw_no_length':('ridge',BASE)}.items():
        p,fit,infer=fit_predict(train,val,kind,cols); choices.append(p.assign(selector=name)); timings.append({'stage':'validation','selector':name,'fit_seconds':fit,'inference_seconds':infer})
    valchoices=pd.concat(choices); _,vm=evaluate_choices(valchoices,data,y,s,levels,logs)
    vs=vm[vm.method.eq('BottomUp')].groupby('selector',as_index=False).mase.mean().sort_values(['mase','selector']); vs.to_csv(OUT/'validation_scores.csv',index=False)
    chosen=vs[vs.selector.isin(CONFIGS)].iloc[0].selector
    train=leaf[leaf.year.between(2006,2017)]; test=leaf[leaf.year.between(2018,2019)]
    choices=[]
    for name,p in baselines(train,test).items(): choices.append(p.assign(selector=name))
    # All pre-specified variants are shown descriptively; only chosen uses validation selection.
    for name,(kind,cols) in {**CONFIGS,'Ridge_raw_no_length':('ridge',BASE)}.items():
        p,fit,infer=fit_predict(train,test,kind,cols); choices.append(p.assign(selector=name)); timings.append({'stage':'exploratory','selector':name,'fit_seconds':fit,'inference_seconds':infer})
    choices=pd.concat(choices,ignore_index=True); choices.to_csv(OUT/'leaf_choices.csv',index=False)
    f,m=evaluate_choices(choices,data,y,s,levels,logs); f.to_csv(OUT/'forecasts.csv',index=False); m.to_csv(OUT/'metrics.csv',index=False)
    m.groupby(['year','selector','method'],as_index=False)[['mase','mae','rmse']].mean().to_csv(OUT/'yearly_comparison.csv',index=False)
    m.groupby(['selector','method'],as_index=False)[['mase','mae','rmse']].mean().to_csv(OUT/'overall_comparison.csv',index=False)
    m.groupby(['year','selector','method','level'],as_index=False)[['mase','mae','rmse']].mean().to_csv(OUT/'level_comparison.csv',index=False)
    previous=pd.read_csv(x.OUT/'yearly_comparison.csv'); previous=previous[(previous.selector.isin(['Global','HistoricalBest','LightGBM'])) & (previous.method.isin(['BottomUp','MinTrace_wls_struct']))].copy(); previous['protocol']='Experiment2_all_levels'
    current=m[m.method.eq('BottomUp')].groupby(['year','selector','method'],as_index=False)[['mase','mae','rmse']].mean(); current['protocol']='Experiment2B_bottom_only'
    pd.concat([previous,current],ignore_index=True).to_csv(OUT/'cross_experiment_comparison.csv',index=False)
    cost=choices.merge(logs[['year','model','inference_seconds']],on=['year','model'],validate='many_to_one')
    # Full-hierarchy batch cost allocated by the fraction of 57 series selected; not measured leaf deployment cost.
    cost['allocated_seconds']=cost.inference_seconds/57
    cost.groupby(['year','selector'],as_index=False).allocated_seconds.sum().to_csv(OUT/'forecast_cost_estimates.csv',index=False)
    pd.DataFrame(timings).to_csv(OUT/'selector_timings.csv',index=False)
    choices.groupby(['year','selector','model']).size().rename('count').reset_index().to_csv(OUT/'selection_frequency.csv',index=False)
    stable=choices.pivot(index=['selector','unique_id'],columns='year',values='model'); stable['same_model']=stable[2018].eq(stable[2019]); stable.to_csv(OUT/'selection_stability.csv')
    (OUT/'metadata.json').write_text(json.dumps({'chosen_validation_variant':chosen,'training':'2006-2015; refit through 2017','validation':'2016-2017','exploratory':'2018-2019 already inspected','ranking':'LambdaRank relevance 3=best to 0=worst within each series-origin; one-hot candidate model','baseline_scope':'global and historical model choices trained on bottom-series MASE','note':'2018-2019 are exploratory; all variants reported, only the validation winner is selected. Leaf series all share hierarchy level and child count, so hierarchy features are constant.'},indent=2))
    return choices,f,m,vs,chosen,pd.DataFrame(timings)
