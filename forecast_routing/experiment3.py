"""Experiment 3: counterfactual hierarchy-aware leaf routing."""
from pathlib import Path
import json, time
import numpy as np
import pandas as pd
from lightgbm import LGBMRegressor
import experiment2b as b

OUT=Path('experiment_3'); OUT.mkdir(exist_ok=True)
MODELS=b.MODELS
BASE=b.BASE
HIER=['ancestor_count','parent_share','sibling_variability','parent_discrepancy']
RECENT='recent_candidate_mase'
PARAMS=b.PARAMS.copy()


def panel(y,s,tags,data):
    """Build feature rows through 2019 and counterfactual labels through 2017."""
    started=time.perf_counter()
    leaves=list(s.columns); ids=list(s.index); level=list(tags)
    assert np.array_equal(s.loc[leaves].to_numpy(),np.eye(len(leaves)))
    lookup={}
    for uid in leaves:
        parents=[v for v in tags[level[-2]] if np.all(s.loc[uid].to_numpy()<=s.loc[v].to_numpy())]
        if len(parents)!=1: raise ValueError('Expected exactly one immediate parent')
        parent=parents[0]; siblings=[v for v in leaves if s.loc[parent,v]==1]
        ancestors=sum(np.all(s.loc[uid].to_numpy()<=s.loc[v].to_numpy()) for v in ids if v not in leaves)
        lookup[uid]=(parent,siblings,ancestors)
    # Cached forecasts are base forecasts at all levels, in explicit S order.
    fc={}
    for year in range(2006,2020):
        for model in MODELS:
            f=pd.read_csv(b.x.CACHE/f'{year}_{model}.csv',parse_dates=['ds'])
            dates=pd.date_range(f'{year}-01-01',periods=12,freq='MS')
            pivot=f.pivot(index='unique_id',columns='ds',values='yhat').reindex(index=ids,columns=dates)
            if pivot.isna().any().any(): raise ValueError(f'Missing candidate forecasts: {year} {model}')
            fc[year,model]=pivot
    rows=[]; labels=[]; refs=[]
    prior_data=data[data.unique_id.isin(leaves)]
    for year in range(2006,2020):
        # Freeze completed-window performance in validation and exploratory evaluations.
        cap=min(year-1,2015 if year in (2016,2017) else 2017 if year>=2018 else year-1)
        past=prior_data[prior_data.year.le(cap)]
        reference=(past.groupby(['unique_id','model']).mase.mean().unstack().reindex(index=leaves,columns=MODELS).idxmin(axis=1) if not past.empty else pd.Series('SeasonalNaive',index=leaves))
        if reference.isna().any(): raise ValueError(f'Incomplete historical reference for {year}')
        origin=pd.Timestamp(year,1,1); history=y[(y.ds<origin)&(y.ds>=origin-pd.DateOffset(months=24))]
        avg=history.groupby('unique_id').y.mean()
        # Previous completed forecast windows only, with cutoff frozen for validation/test.
        recent_years=sorted(past.year.unique())[-3:]
        recent=past[past.year.isin(recent_years)].groupby(['unique_id','model']).mase.mean()
        for uid in leaves:
            parent,siblings,ancestors=lookup[uid]
            parent_mean=avg.loc[parent]; child_means=avg.reindex(siblings).to_numpy()
            share=avg.loc[uid]/parent_mean if abs(parent_mean)>1e-12 else np.nan
            variability=np.std(child_means)/abs(np.mean(child_means)) if abs(np.mean(child_means))>1e-12 else np.nan
            refs.append({'year':year,'unique_id':uid,'reference_model':reference.loc[uid],'latest_reference_year':cap if not past.empty else np.nan})
            for model in MODELS:
                discrepancies=[]
                for prev in recent_years:
                    p=fc[prev,model]; scale=data[(data.year.eq(prev))&(data.unique_id.eq(parent))].scale.iloc[0]
                    discrepancies.append(np.mean(np.abs(p.loc[parent].to_numpy()-p.loc[siblings].to_numpy().sum(axis=0)))/scale)
                rows.append({'year':year,'unique_id':uid,'model':model,'ancestor_count':ancestors,'parent_share':share,'sibling_variability':variability,'parent_discrepancy':np.mean(discrepancies) if discrepancies else np.nan,RECENT:recent.get((uid,model),np.nan),'latest_feature_performance_year':max(recent_years) if recent_years else np.nan,'history_end':str((origin-pd.offsets.MonthBegin(1)).date())})
        if year>2017: continue
        target=y[(y.ds>=origin)&(y.ds<origin+pd.DateOffset(years=1))].pivot(index='unique_id',columns='ds',values='y').reindex(ids)
        scales=data[data.year.eq(year)][['unique_id','scale']].drop_duplicates().set_index('unique_id').reindex(ids).scale.to_numpy()
        if target.isna().any().any() or not np.isfinite(scales).all() or (scales<=0).any(): raise ValueError('Invalid truth or scale')
        truth=target.to_numpy(); selected=np.stack([fc[year,reference.loc[uid]].loc[uid].to_numpy() for uid in leaves])
        reconciled=s.to_numpy() @ selected
        if not np.allclose(reconciled,s.to_numpy()@reconciled[[ids.index(i) for i in leaves],:]): raise ValueError('Reference is incoherent')
        loss=lambda a: float(np.mean(np.mean(np.abs(a-truth),axis=1)/scales))
        ref_loss=loss(reconciled)
        for j,uid in enumerate(leaves):
            for model in MODELS:
                candidate=fc[year,model].loc[uid].to_numpy()
                changed=reconciled+s.to_numpy()[:,j,None]*(candidate-selected[j])[None,:]
                labels.append({'year':year,'unique_id':uid,'model':model,'reference_model':reference.loc[uid],'reference_loss':ref_loss,'counterfactual_loss':loss(changed),'loss_change':loss(changed)-ref_loss})
    features=pd.DataFrame(rows); labels=pd.DataFrame(labels); refs=pd.DataFrame(refs)
    merged=data[data.unique_id.isin(leaves)].merge(features,on=['year','unique_id','model'],validate='one_to_one')
    labels=labels.merge(merged,on=['year','unique_id','model'],validate='one_to_one')
    assert len(merged)==14*32*4 and len(labels)==12*32*4
    assert ((refs.latest_reference_year.isna())|(refs.latest_reference_year<refs.year)).all()
    assert ((features.latest_feature_performance_year.isna())|(features.latest_feature_performance_year<features.year)).all()
    assert (labels[labels.model.eq(labels.reference_model)].loss_change.abs()<1e-10).all()
    refs.to_csv(OUT/'reference_assignments.csv',index=False); merged.to_csv(OUT/'selector_features.csv',index=False); labels.to_csv(OUT/'counterfactual_labels.csv',index=False)
    cutoffs=features[['year','history_end','latest_feature_performance_year']].drop_duplicates().copy()
    cutoffs['target_start']=pd.to_datetime(cutoffs.year.astype(str)+'-01-01'); cutoffs['target_end']=cutoffs.target_start+pd.DateOffset(months=11)
    cutoffs['partition']=np.select([cutoffs.year.le(2015),cutoffs.year.le(2017)],['training','validation'],default='exploratory')
    cutoffs.to_csv(OUT/'cutoffs.csv',index=False)
    (OUT/'panel_timing.json').write_text(json.dumps({'feature_and_counterfactual_construction_seconds':time.perf_counter()-started},indent=2))
    return merged,labels,refs


def predict_aware(train,test,use_hierarchy):
    cols=BASE+[RECENT]+(HIER if use_hierarchy else [])
    models={}; start=time.perf_counter()
    for model in MODELS:
        d=train[train.model.eq(model)]; obj=LGBMRegressor(**PARAMS); obj.fit(d[cols],d.loss_change); models[model]=obj
    fit=time.perf_counter()-start; start=time.perf_counter(); result=[]
    for model in MODELS:
        d=test[test.model.eq(model)].sort_values(['year','unique_id']).copy(); d['score']=models[model].predict(d[cols]); result.append(d[['year','unique_id','model','score']])
    scores=pd.concat(result,ignore_index=True)
    choices=scores.sort_values(['year','unique_id','score','model']).drop_duplicates(['year','unique_id'])[['year','unique_id','model']]
    return choices,scores,fit,time.perf_counter()-start


def predict_matched_regret(train,test):
    cols=BASE+[RECENT]; d=train.copy()
    d['target']=d.mase-d.groupby(['year','unique_id']).mase.transform('min')
    models={}; start=time.perf_counter()
    for model in MODELS:
        a=d[d.model.eq(model)]; obj=LGBMRegressor(**PARAMS); obj.fit(a[cols],a.target); models[model]=obj
    fit=time.perf_counter()-start; start=time.perf_counter(); result=[]
    for model in MODELS:
        q=test[test.model.eq(model)].sort_values(['year','unique_id']).copy(); q['score']=models[model].predict(q[cols]); result.append(q[['year','unique_id','model','score']])
    scores=pd.concat(result,ignore_index=True)
    choices=scores.sort_values(['year','unique_id','score','model']).drop_duplicates(['year','unique_id'])[['year','unique_id','model']]
    return choices,fit,time.perf_counter()-start


def select(leaf,labels,stage,years,last_year):
    train=leaf[leaf.year.le(last_year)]; test=leaf[leaf.year.isin(years)]
    history=labels[labels.year.le(last_year)]
    choices=[]; timing=[]; scores=[]
    for name,p in b.baselines(train,test).items(): choices.append(p.assign(selector=name))
    for name,kind,cols in [('Ridge','ridge',BASE),('LGBM_regret','regret',b.CONFIGS['LGBM_regret_no_length'][1])]:
        p,fit,infer=b.fit_predict(train,test,kind,cols); choices.append(p.assign(selector=name)); timing.append({'stage':stage,'selector':name,'fit_seconds':fit,'inference_seconds':infer})
    p,fit,infer=predict_matched_regret(train,test)
    choices.append(p.assign(selector='LGBM_regret_matched_features')); timing.append({'stage':stage,'selector':'LGBM_regret_matched_features','fit_seconds':fit,'inference_seconds':infer})
    for name,extra in [('HierarchyLoss_no_hierarchy_features',False),('HierarchyLoss_with_hierarchy_features',True)]:
        p,sc,fit,infer=predict_aware(history,test,extra); choices.append(p.assign(selector=name)); scores.append(sc.assign(selector=name)); timing.append({'stage':stage,'selector':name,'fit_seconds':fit,'inference_seconds':infer})
    return pd.concat(choices,ignore_index=True),pd.concat(scores,ignore_index=True),pd.DataFrame(timing)


def run(y,s,tags,levels,data,logs,leaf,labels):
    v,vs,vt=select(leaf,labels,'validation',[2016,2017],2015)
    vf,vm=b.evaluate_choices(v,data,y,s,levels,logs)
    vm[vm.method.eq('BottomUp')].groupby(['year','selector'],as_index=False)[['mase','mae','rmse']].mean().to_csv(OUT/'validation_scores.csv',index=False)
    choices,scores,tt=select(leaf,labels,'exploratory_frozen_2015',[2018,2019],2015)
    choices.to_csv(OUT/'selector_predictions.csv',index=False); scores.to_csv(OUT/'predicted_counterfactuals.csv',index=False)
    f,m=b.evaluate_choices(choices,data,y,s,levels,logs)
    f.to_csv(OUT/'mixed_forecasts.csv',index=False); m.to_csv(OUT/'metrics.csv',index=False)
    bu=m[m.method.eq('BottomUp')]
    bu.groupby(['year','selector'],as_index=False)[['mase','mae','rmse']].mean().to_csv(OUT/'yearly_comparison.csv',index=False)
    bu.groupby('selector',as_index=False)[['mase','mae','rmse']].mean().to_csv(OUT/'overall_comparison.csv',index=False)
    bu.groupby(['year','selector','level'],as_index=False)[['mase','mae','rmse']].mean().to_csv(OUT/'level_comparison.csv',index=False)
    p=bu.pivot(index=['year','unique_id','level'],columns='selector',values='mase')
    diffs=[]
    for comparator in ['Global','HistoricalBest','Ridge','LGBM_regret','LGBM_regret_matched_features','HierarchyLoss_no_hierarchy_features']:
        for selector in ['HierarchyLoss_no_hierarchy_features','HierarchyLoss_with_hierarchy_features']:
            if selector==comparator: continue
            for year in [2018,2019,'all']:
                q=p if year=='all' else p.loc[year]; delta=q[selector]-q[comparator]
                diffs.append({'year':year,'selector':selector,'comparator':comparator,'delta_mase':delta.mean(),'relative_change':delta.mean()/q[comparator].mean(),'improved':int((delta<-1e-12).sum()),'worsened':int((delta>1e-12).sum()),'tied':int((abs(delta)<=1e-12).sum())})
    pd.DataFrame(diffs).to_csv(OUT/'paired_differences.csv',index=False)
    choices.groupby(['year','selector','model']).size().rename('count').reset_index().to_csv(OUT/'selection_frequency.csv',index=False)
    costs=choices.merge(logs[['year','model','inference_seconds']],on=['year','model'],validate='many_to_one'); costs['allocated_seconds']=costs.inference_seconds/57
    costs.groupby(['year','selector'],as_index=False).allocated_seconds.sum().to_csv(OUT/'forecast_cost_estimates.csv',index=False)
    # Separate sensitivity refit reproduces Experiment 2B's training convention.
    refit,_,rt=select(leaf,labels,'sensitivity_refit_2017',[2018,2019],2017)
    _,rm=b.evaluate_choices(refit,data,y,s,levels,logs)
    refit.to_csv(OUT/'sensitivity_refit_predictions.csv',index=False)
    rm[rm.method.eq('BottomUp')].groupby(['year','selector'],as_index=False)[['mase','mae','rmse']].mean().to_csv(OUT/'sensitivity_refit_comparison.csv',index=False)
    pd.concat([vt,tt,rt]).to_csv(OUT/'selector_timings.csv',index=False)
    (OUT/'metadata.json').write_text(json.dumps({'reference':'HistoricalBest from strictly earlier completed windows; SeasonalNaive fallback for 2006','label':'single-leaf replacement change in mean 57-series MASE vs fixed reference assignment','primary_training':'2006–2015, frozen','validation':'2016–2017, historical performance frozen through 2015','exploratory':'2018–2019, selector training frozen through 2015; origin features may use completed windows through 2017','sensitivity':'refit through 2017, matching Experiment 2B','common_extra_feature':'recent candidate MASE is included in both hierarchy-loss variants and the matched-feature regret control','hierarchy_features':HIER,'caveat':'single-leaf effects are conditional on the reference and are not additive; evaluate assembled forecasts directly'},indent=2))
    return choices,scores,f,m,vm,pd.concat([vt,tt,rt])
