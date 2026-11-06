"""TourismLarge independent evaluation; fixed quarterly-origin protocol."""
from pathlib import Path
import hashlib,inspect,json,time,warnings
from importlib.metadata import version
import numpy as np
import pandas as pd
from statsmodels.tsa.seasonal import STL
from lightgbm import LGBMRegressor
from sklearn.pipeline import make_pipeline
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import Ridge
from datasetsforecast.hierarchical import HierarchicalData

OUT=Path('experiment_5b');CACHE=OUT/'cache';CACHE.mkdir(parents=True,exist_ok=True)
H=3; SEASON=12; SEED=42
MODELS=['AutoARIMA','NBEATS','SeasonalNaive','TimesFM3']
FEATURES=['trend_strength','seasonal_strength','std','cv','lag1','lag_season','zero_ratio','missing_ratio','recent_growth']
PARAMS=dict(num_leaves=7,max_depth=3,n_estimators=150,min_child_samples=40,reg_lambda=10.,learning_rate=.03,random_state=42,n_jobs=1,verbosity=-1)


def dataset():
    y,original,tags=HierarchicalData.load('data','TourismLarge');y.ds=pd.to_datetime(y.ds)
    if original.shape!=(555,304) or y.unique_id.nunique()!=555 or y.isna().any().any(): raise ValueError('Invalid original TourismLarge hierarchy')
    if not original.index.is_unique or not original.columns.is_unique or not np.array_equal(original.loc[original.columns].to_numpy(),np.eye(len(original.columns))): raise ValueError('Invalid original summing matrix')
    full=y.pivot(index='unique_id',columns='ds',values='y').reindex(original.index)
    if full.isna().any().any() or not np.allclose(full.to_numpy(),original.to_numpy()@full.loc[original.columns].to_numpy(),rtol=1e-5,atol=1e-3): raise ValueError('Original aggregation mismatch')
    dates=pd.date_range('1998-01-01','2016-12-01',freq='MS')
    if not full.columns.equals(dates): raise ValueError('TourismLarge does not have the expected complete calendar')
    states=sorted(map(str,tags['Country/State']))[:4]; region_ids=sorted(map(str,tags['Country/State/Zone/Region']))
    purposes=sorted(map(str,tags['Country/Purpose']))
    if len(states)!=4 or len(purposes)!=4: raise ValueError('Unexpected geography or purpose tags')
    selected=[]
    for state in states:
        parent=original.loc[state].to_numpy(); regions=[r for r in region_ids if np.all(original.loc[r].to_numpy()<=parent)]
        if len(regions)<3: raise ValueError(f'Fewer than three metadata regions in {state}')
        selected.extend((state,r) for r in regions[:3])
    chosen=[]
    for state,region in selected:
        for purpose in purposes:
            mask=(original.loc[region].to_numpy()==1)&(original.loc[purpose].to_numpy()==1)
            members=original.columns[mask].tolist()
            if len(members)!=1: raise ValueError(f'Non-unique leaf for {region} {purpose}')
            chosen.append({'state':state,'region':region,'purpose':purpose,'source_leaf':members[0]})
    meta=pd.DataFrame(chosen);meta.to_csv(OUT/'selected_geography.csv',index=False)
    leaves=meta.source_leaf.tolist();n=len(leaves); rows=[];levels=[]
    def add(level,key,mask): rows.append((level+'|'+key,np.asarray(mask,dtype=int)));levels.append(level)
    add('total','selected',np.ones(n))
    for purpose in purposes: add('purpose',purpose,meta.purpose.eq(purpose))
    for state in states: add('state',state,meta.state.eq(state))
    for state in states:
        for purpose in purposes: add('state_purpose',state+'|'+purpose,meta.state.eq(state)&meta.purpose.eq(purpose))
    for _,region in selected: add('region',region,meta.region.eq(region))
    for leaf in leaves: add('leaf',leaf,meta.source_leaf.eq(leaf))
    ids=[r[0] for r in rows];leaf_ids=['leaf|'+v for v in leaves];matrix=np.stack([r[1] for r in rows]);s=pd.DataFrame(matrix,index=ids,columns=leaf_ids)
    if len(ids)!=85 or len(set(ids))!=85 or len(set(map(tuple,matrix)))!=85 or not np.array_equal(s.loc[leaf_ids].to_numpy(),np.eye(48)): raise ValueError('Invalid subset hierarchy or duplicate masks')
    values=matrix@full.loc[leaves,dates].to_numpy();actual=pd.DataFrame(values,index=ids,columns=dates)
    if len(set(map(tuple,values)))!=85 or not np.allclose(actual.to_numpy(),s.to_numpy()@actual.loc[leaf_ids].to_numpy(),rtol=1e-10,atol=1e-8): raise ValueError('Duplicate or incoherent series')
    for _,region in selected:
        if not np.allclose(actual.loc['region|'+region].to_numpy(),full.loc[region,dates].to_numpy(),rtol=1e-5,atol=1e-3): raise ValueError('Selected region does not match original data')
    s.rename_axis('unique_id').to_csv(OUT/'summing_matrix.csv'); actual.rename_axis('unique_id').to_csv(OUT/'actual_series.csv');hier=pd.DataFrame({'unique_id':ids,'level':levels});hier.to_csv(OUT/'hierarchy.csv',index=False)
    wins=[]
    for start in range(120,228,H):
        first=dates[start];last=dates[start+H-1];split='train' if last.year<=2011 else 'validation' if last.year<=2013 else 'test'
        if first.year!=last.year: raise ValueError('Window crosses year boundary')
        wins.append({'origin':start,'history_end':dates[start-1],'target_start':first,'target_end':last,'split':split})
    wins=pd.DataFrame(wins);wins.to_csv(OUT/'origins.csv',index=False)
    print('Subset:',len(states),'states',len(selected),'regions',len(leaves),'leaves',len(ids),'nodes; origins',wins.split.value_counts().to_dict(),flush=True)
    return actual,s,hier,wins


_tfm=None
def predict(name,train,dates):
    global _tfm
    import torch
    np.random.seed(SEED);torch.manual_seed(SEED);t=time.perf_counter()
    if name in ('AutoARIMA','SeasonalNaive'):
        from statsforecast import StatsForecast
        from statsforecast.models import AutoARIMA,SeasonalNaive
        obj=StatsForecast(models=[AutoARIMA(season_length=12,approximation=True,max_p=2,max_q=2,max_P=1,max_Q=1) if name=='AutoARIMA' else SeasonalNaive(season_length=12)],freq='MS',n_jobs=1)
        obj.fit(train);fit=time.perf_counter()-t;t=time.perf_counter();f=obj.predict(h=H).reset_index()[['unique_id','ds',name]].rename(columns={name:'yhat'})
    elif name=='NBEATS':
        from neuralforecast import NeuralForecast
        from neuralforecast.models import NBEATS
        obj=NeuralForecast(models=[NBEATS(h=H,input_size=2*H,max_steps=100,mlp_units=[[128,128]]*3,scaler_type='robust',random_seed=SEED,enable_progress_bar=False,logger=False,enable_model_summary=False)],freq='MS')
        obj.fit(df=train);fit=time.perf_counter()-t;t=time.perf_counter();f=obj.predict().reset_index()[['unique_id','ds','NBEATS']].rename(columns={'NBEATS':'yhat'})
    else:
        from timesfm3 import TimesFM3Evaluator,ModelConfig
        if _tfm is None:_tfm=TimesFM3Evaluator(ModelConfig(checkpoint_path='google/timesfm-3.0-pytorch',per_core_batch_size=4,device='cuda' if torch.cuda.is_available() else 'cpu'))
        fit=time.perf_counter()-t;t=time.perf_counter();groups=list(train.groupby('unique_id',sort=True));out=list(_tfm.predict_batch([g.y.to_numpy(dtype=np.float32) for _,g in groups],horizon=H,return_quantiles=False,use_symmetric_averaging=False))
        f=pd.concat([pd.DataFrame({'unique_id':uid,'ds':dates,'yhat':np.asarray(v.forecast)}) for (uid,_),v in zip(groups,out)],ignore_index=True)
    return f,fit,time.perf_counter()-t


def forecast_cache(y,s,wins):
    sig=hashlib.sha256(pd.util.hash_pandas_object(y,index=True).values.tobytes()+inspect.getsource(predict).encode()+json.dumps({'h':H,'season':SEASON,'seed':SEED,'versions':{p:version(p) for p in ['statsforecast','neuralforecast','timesfm','torch','numpy','pandas']}},sort_keys=True).encode()).hexdigest()
    logs=[]
    for w in wins.itertuples(index=False):
        dates=y.columns[w.origin:w.origin+H];train=y.loc[s.columns].iloc[:,:w.origin].rename_axis('unique_id').stack().rename('y').reset_index().rename(columns={'level_1':'ds'})
        for model in MODELS:
            path=CACHE/f'{w.origin}_{model}.csv';meta=path.with_suffix('.json')
            try:
                ok=False
                if path.exists() and meta.exists():
                    info=json.loads(meta.read_text());ok=info['signature']==sig and info['sha256']==hashlib.sha256(path.read_bytes()).hexdigest()
                if ok: f=pd.read_csv(path,parse_dates=['ds']);source='cache'
                else:
                    with warnings.catch_warnings(record=True) as caught:
                        warnings.simplefilter('default');leaf,fit,infer=predict(model,train,dates)
                    pivot=leaf.pivot(index='unique_id',columns='ds',values='yhat').reindex(index=s.columns,columns=dates)
                    if pivot.isna().any().any():raise ValueError('Missing leaf predictions')
                    f=pd.DataFrame(s.to_numpy()@pivot.to_numpy(),index=s.index,columns=dates).rename_axis('unique_id').stack().rename('yhat').reset_index().rename(columns={'level_1':'ds'})
                    info={'signature':sig,'fit_seconds':fit,'inference_seconds':infer,'warnings':sorted(set(str(x.message) for x in caught))};source='computed'
                expected=pd.MultiIndex.from_product([s.index,dates],names=['unique_id','ds']);got=pd.MultiIndex.from_frame(f[['unique_id','ds']])
                if got.has_duplicates or set(got)!=set(expected) or not np.isfinite(f.yhat).all(): raise ValueError('Missing or invalid predictions')
                if not ok:f.to_csv(path,index=False);info['sha256']=hashlib.sha256(path.read_bytes()).hexdigest();meta.write_text(json.dumps(info,indent=2))
                logs.append({'origin':w.origin,'split':w.split,'model':model,'status':'ok','source':source,**info});print(w.origin,w.split,model,source,'ok',flush=True)
            except Exception as ex:
                logs.append({'origin':w.origin,'split':w.split,'model':model,'status':'failed','error':repr(ex)});print(w.origin,model,'FAILED',repr(ex),flush=True)
            pd.DataFrame(logs).to_csv(OUT/'forecast_status.csv',index=False)
    if any(r['status']!='ok' for r in logs):raise RuntimeError('Forecast runs failed; inspect forecast_status.csv')
    return pd.DataFrame(logs)


def freeze():
    spec={'source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'dataset':'Nixtla TourismLarge; first four coded states alphabetically and first three coded regions within each state using original summing-matrix metadata','selection':'metadata only; no observed test outcomes','n_nodes':85,'leaves':48,'horizon_months':3,'season':12,'models':MODELS,'model_settings':'48 leaves; AutoARIMA bounded approximation season 12; NBEATS input 2*h=6, steps 100, robust scaling; TimesFM3 pretrained complete expanding history; SeasonalNaive 12','split':'training targets 2008-2011, validation 2012-2013, test 2014-2016','features':'Experiment 2B/3 features with lag 12 and recent growth over 3 vs preceding 3 months','selection_parameters':PARAMS,'ridge_alpha':10,'recent_model_performance':'last three earlier completed full-shadow windows','online_policy':'global/historical best and recent candidate-performance features update after completed windows, including prior test windows; learned selectors fitted on training only','primary':'equal-node MASE','secondary':'equal level, half aggregate half leaf, leaf-only, MAE and RMSE','bootstrap':'paired origin circular block bootstrap, block length 2, 5000 resamples, seed 42','sensitivity':'repeat whole protocol without TimesFM3, reselect references and refit selectors','versions':{p:version(p) for p in ['datasetsforecast','statsforecast','neuralforecast','timesfm','torch','numpy','pandas','lightgbm','scikit-learn']}}
    path=OUT/'protocol_lock.json'
    if path.exists():
        if json.loads(path.read_text())!=spec:raise RuntimeError('Protocol lock mismatch; do not overwrite the experiment')
    else:path.write_text(json.dumps(spec,indent=2))
    return spec


def panel(y,s,hier,wins):
    freeze();ids=list(s.index);leaves=list(s.columns);features=[];metrics=[];previous=[];issues=[];times=[];forecasts={}
    for w in wins.itertuples(index=False):
        history=y.iloc[:,:w.origin];truth=y.iloc[:,w.origin:w.origin+H].to_numpy();scales=np.mean(abs(history.to_numpy()[:,SEASON:]-history.to_numpy()[:,:-SEASON]),axis=1);valid=np.isfinite(scales)&(scales>0)
        if not valid.any():raise ValueError('No valid seasonal scales')
        for uid,scale,good in zip(ids,scales,valid):
            if not good:issues.append({'origin':w.origin,'unique_id':uid,'scale':scale})
        for model in MODELS:
            f=pd.read_csv(CACHE/f'{w.origin}_{model}.csv',parse_dates=['ds']);p=f.pivot(index='unique_id',columns='ds',values='yhat').reindex(index=ids,columns=y.columns[w.origin:w.origin+H]);
            if p.isna().any().any():raise ValueError(f'Missing cached forecast {w.origin} {model}')
            forecasts[w.origin,model]=p.to_numpy();error=p.to_numpy()-truth;mae=np.mean(abs(error),axis=1);rmse=np.sqrt(np.mean(error**2,axis=1))
            for uid,scale,good,a,r in zip(ids,scales,valid,mae,rmse):metrics.append({'origin':w.origin,'split':w.split,'unique_id':uid,'model':model,'scale':scale,'valid_scale':bool(good),'mae':a,'rmse':r,'mase':a/scale if good else np.nan})
        past=pd.DataFrame(previous);t=time.perf_counter()
        for uid in leaves:
            arr=history.loc[uid].to_numpy();mu=arr.mean();sd=arr.std()
            if sd>1e-12:
                stl=STL(arr,period=12,robust=True).fit();vr=np.var(stl.resid);trend=max(0.,1-vr/max(np.var(stl.trend+stl.resid),1e-12));seasonal=max(0.,1-vr/max(np.var(stl.seasonal+stl.resid),1e-12))
            else:trend=seasonal=np.nan
            def ac(lag):return np.corrcoef(arr[:-lag],arr[lag:])[0,1] if len(arr)>lag and np.std(arr[:-lag])>1e-12 and np.std(arr[lag:])>1e-12 else np.nan
            growth=(arr[-H:].mean()-arr[-2*H:-H].mean())/abs(arr[-2*H:-H].mean()) if abs(arr[-2*H:-H].mean())>1e-12 else np.nan
            for model in MODELS:
                prev=past[(past.unique_id.eq(uid))&(past.model.eq(model))].tail(3) if not past.empty else pd.DataFrame()
                features.append({'origin':w.origin,'split':w.split,'unique_id':uid,'model':model,'trend_strength':trend,'seasonal_strength':seasonal,'std':sd,'cv':sd/abs(mu) if abs(mu)>1e-12 else np.nan,'lag1':ac(1),'lag_season':ac(12),'zero_ratio':np.mean(arr==0),'missing_ratio':0.,'recent_growth':growth,'recent_candidate_mase':prev.mase.mean() if not prev.empty else np.nan,'last_completed_origin':int(past.origin.max()) if not past.empty else np.nan})
        times.append({'origin':w.origin,'split':w.split,'feature_seconds':time.perf_counter()-t,'leaf_count':len(leaves)})
        previous.extend([m for m in metrics if m['origin']==w.origin and m['unique_id'] in leaves])
    feat=pd.DataFrame(features);met=pd.DataFrame(metrics);issues=pd.DataFrame(issues,columns=['origin','unique_id','scale']);times=pd.DataFrame(times)
    feat.to_csv(OUT/'selector_features.csv',index=False);met.to_csv(OUT/'candidate_metrics.csv',index=False);issues.to_csv(OUT/'scale_exclusions.csv',index=False);times.to_csv(OUT/'feature_cost.csv',index=False)
    return feat,met,forecasts,issues,times


def reference_labels(y,s,wins,met,forecasts,models,scenario):
    ids=list(s.index);leaves=list(s.columns);label=[];refs=[];leaf_pos=[ids.index(v) for v in leaves];prior=met[met.unique_id.isin(leaves)&met.model.isin(models)]
    for w in wins.itertuples(index=False):
        past=prior[prior.origin.lt(w.origin)];best=past.groupby(['unique_id','model']).mase.mean().unstack().reindex(index=leaves,columns=models).idxmin(axis=1).fillna('SeasonalNaive') if not past.empty else pd.Series('SeasonalNaive',index=leaves)
        global_means=past.groupby('model').mase.mean().reindex(models) if not past.empty else pd.Series(dtype=float);global_model=global_means.idxmin() if len(global_means) and global_means.notna().any() else 'SeasonalNaive'
        for uid in leaves:refs.append({'origin':w.origin,'unique_id':uid,'reference_model':best.loc[uid],'global_model':global_model,'last_completed_origin':past.origin.max() if not past.empty else np.nan})
        if w.split!='train':continue
        truth=y.iloc[:,w.origin:w.origin+H].to_numpy();history=y.iloc[:,:w.origin].to_numpy();scale=np.mean(abs(history[:,12:]-history[:,:-12]),axis=1);valid=np.isfinite(scale)&(scale>0)
        selected=np.stack([forecasts[w.origin,best.loc[uid]][i] for uid,i in zip(leaves,leaf_pos)]);base=s.to_numpy()@selected
        loss=lambda p:float(np.mean(np.mean(abs(p-truth),axis=1)[valid]/scale[valid]));base_loss=loss(base)
        for j,(uid,i) in enumerate(zip(leaves,leaf_pos)):
            for model in models:
                alt=base+s.to_numpy()[:,j,None]*(forecasts[w.origin,model][i]-selected[j])[None,:];value=loss(alt)
                label.append({'origin':w.origin,'unique_id':uid,'model':model,'reference_model':best.loc[uid],'reference_loss':base_loss,'counterfactual_loss':value,'loss_change':value-base_loss})
    label=pd.DataFrame(label);refs=pd.DataFrame(refs)
    if not ((refs.last_completed_origin.isna())|(refs.last_completed_origin+H<=refs.origin)).all():raise ValueError('Reference uses incomplete outcomes')
    if not label[label.model.eq(label.reference_model)].loss_change.abs().lt(1e-10).all():raise ValueError('Reference target invalid')
    label.assign(scenario=scenario).to_csv(OUT/f'counterfactual_labels_{scenario}.csv',index=False);refs.assign(scenario=scenario).to_csv(OUT/f'reference_assignments_{scenario}.csv',index=False)
    return label,refs


def fit_selectors(feat,met,labels,models,scenario):
    train=feat[(feat.split.eq('train'))&feat.model.isin(models)].merge(met[['origin','unique_id','model','mase','valid_scale']],on=['origin','unique_id','model'],validate='one_to_one')
    valid=train.groupby(['origin','unique_id']).valid_scale.transform('all');train=train[valid].copy();train['regret']=train.mase-train.groupby(['origin','unique_id']).mase.transform('min');counter=train.merge(labels[['origin','unique_id','model','loss_change']],on=['origin','unique_id','model'],validate='one_to_one')
    fitted={};cost=[]
    for selector in ['Ridge','LGBM_regret_matched','HierarchyLoss']:
        fitted[selector]={};t=time.perf_counter()
        for model in models:
            part=(counter if selector=='HierarchyLoss' else train);part=part[part.model.eq(model)];cols=FEATURES if selector=='Ridge' else FEATURES+['recent_candidate_mase']
            obj=make_pipeline(SimpleImputer(strategy='median',keep_empty_features=True),StandardScaler(),Ridge(alpha=10.)) if selector=='Ridge' else LGBMRegressor(**PARAMS)
            target=part.loss_change if selector=='HierarchyLoss' else part.mase if selector=='Ridge' else part.regret;obj.fit(part[cols],target);fitted[selector][model]=obj
        cost.append({'scenario':scenario,'selector':selector,'fit_seconds':time.perf_counter()-t,'train_origins':train.origin.nunique(),'train_rows':len(train)})
    return fitted,pd.DataFrame(cost)


def evaluate(y,s,hier,wins,feat,met,forecasts,refs,fitted,models,scenario):
    choices=[]; timing=[]
    for w in wins[wins.split.isin(['validation','test'])].itertuples(index=False):
        q=feat[(feat.origin.eq(w.origin))&feat.model.isin(models)];r=refs[refs.origin.eq(w.origin)].set_index('unique_id')
        for uid in s.columns:
            choices.extend([{'origin':w.origin,'split':w.split,'unique_id':uid,'selector':'GlobalBest','model':r.loc[uid,'global_model']},{'origin':w.origin,'split':w.split,'unique_id':uid,'selector':'HistoricalBest','model':r.loc[uid,'reference_model']}])
        for selector in ['Ridge','LGBM_regret_matched','HierarchyLoss']:
            t=time.perf_counter();parts=[]
            for model in models:
                a=q[q.model.eq(model)].sort_values('unique_id');cols=FEATURES if selector=='Ridge' else FEATURES+['recent_candidate_mase'];parts.append(a[['unique_id','model']].assign(score=fitted[selector][model].predict(a[cols])))
            pick=pd.concat(parts).sort_values(['unique_id','score','model']).drop_duplicates('unique_id');timing.append({'scenario':scenario,'origin':w.origin,'split':w.split,'selector':selector,'inference_seconds':time.perf_counter()-t})
            for row in pick.itertuples(index=False):choices.append({'origin':w.origin,'split':w.split,'unique_id':row.unique_id,'selector':selector,'model':row.model})
    choices=pd.DataFrame(choices);choices.assign(scenario=scenario).to_csv(OUT/f'selections_{scenario}.csv',index=False)
    ids=list(s.index);leaves=list(s.columns);idx=[ids.index(x) for x in leaves];fs=[]
    for (origin,selector),q in choices.groupby(['origin','selector']):
        names=q.set_index('unique_id').model;bottom=np.stack([forecasts[origin,names.loc[uid]][i] for uid,i in zip(leaves,idx)]);result=s.to_numpy()@bottom
        if not np.allclose(result,s.to_numpy()@result[idx],rtol=1e-9,atol=1e-7):raise ValueError('Incoherent Bottom-Up forecast')
        f=pd.DataFrame(result,index=ids,columns=y.columns[origin:origin+H]).rename_axis('unique_id').stack().rename('forecast').reset_index().rename(columns={'level_1':'ds'});fs.append(f.assign(origin=origin,selector=selector,scenario=scenario))
    f=pd.concat(fs,ignore_index=True);actual=y.rename_axis('unique_id').stack().rename('y').reset_index().rename(columns={'level_1':'ds'});f=f.merge(actual,on=['unique_id','ds'],validate='many_to_one')
    scales=met[['origin','unique_id','scale','valid_scale']].drop_duplicates();f=f.merge(scales,on=['origin','unique_id'],validate='many_to_one').merge(hier,on='unique_id',validate='many_to_one').merge(wins[['origin','split','target_start','target_end']],on='origin',validate='many_to_one')
    f['ae']=abs(f.forecast-f.y);f['se']=(f.forecast-f.y)**2
    metric=f.groupby(['origin','split','scenario','selector','unique_id','level'],as_index=False).agg(mae=('ae','mean'),mse=('se','mean'),scale=('scale','first'),valid_scale=('valid_scale','first'),n=('y','size'))
    metric['rmse']=np.sqrt(metric.pop('mse'));metric['mase']=metric.mae/metric.scale.where(metric.valid_scale)
    if not metric.n.eq(H).all():raise ValueError('Missing test periods')
    f.to_csv(OUT/f'reconciled_forecasts_{scenario}.csv',index=False);metric.to_csv(OUT/f'selector_metrics_{scenario}.csv',index=False);pd.DataFrame(timing).to_csv(OUT/f'selector_inference_{scenario}.csv',index=False)
    objective=[]
    for (origin,split,selector),g in metric.groupby(['origin','split','selector']):
        g=g[g.valid_scale].copy();leaf=g.level.eq('leaf');nlevels=g.level.nunique()
        if not leaf.any() or not (~leaf).any():raise ValueError('No valid aggregate or leaf scale')
        objective.extend([{'scenario':scenario,'origin':origin,'split':split,'selector':selector,'objective':name,'mase':value,'mae':g.mae.mean(),'rmse':g.rmse.mean(),'valid_nodes':len(g)} for name,value in [('equal_node',g.mase.mean()),('equal_level',g.groupby('level').mase.mean().mean()),('half_aggregate_half_leaf',(g.loc[leaf,'mase'].mean()+g.loc[~leaf,'mase'].mean())/2),('leaf_only',g.loc[leaf,'mase'].mean())]])
    objective=pd.DataFrame(objective).merge(wins[['origin','target_start','target_end']],on='origin',validate='many_to_one')
    hist=objective[objective.selector.eq('HistoricalBest')][['origin','objective','mase']].rename(columns={'mase':'historical_mase'})
    objective=objective.merge(hist,on=['origin','objective'],validate='many_to_one');objective['difference_vs_historical']=objective.mase-objective.historical_mase;objective['relative_vs_historical']=objective.difference_vs_historical/objective.historical_mase.where(objective.historical_mase.ne(0))
    objective.to_csv(OUT/f'objective_per_origin_{scenario}.csv',index=False)
    objective.groupby(['split','selector','objective'],as_index=False).agg(mase=('mase','mean'),mae=('mae','mean'),rmse=('rmse','mean'),origins=('origin','size')).to_csv(OUT/f'overall_{scenario}.csv',index=False)
    metric.groupby(['split','selector','level'],as_index=False).agg(mase=('mase','mean'),mae=('mae','mean'),rmse=('rmse','mean'),n=('mase','count')).to_csv(OUT/f'level_comparison_{scenario}.csv',index=False)
    choices.groupby(['split','selector','model']).size().rename('count').reset_index().to_csv(OUT/f'selection_frequency_{scenario}.csv',index=False)
    c=choices[choices.split.eq('test')].sort_values(['selector','unique_id','origin']).copy();c['previous_model']=c.groupby(['selector','unique_id']).model.shift();c['switched']=c.previous_model.notna()&c.model.ne(c.previous_model)
    candidates=met[['origin','unique_id','model','mase']];c=c.merge(candidates.rename(columns={'mase':'selected_mase'}),on=['origin','unique_id','model'],validate='many_to_one').merge(candidates.rename(columns={'model':'previous_model','mase':'previous_mase'}),on=['origin','unique_id','previous_model'],how='left',validate='many_to_one');c['switch_difference']=c.selected_mase-c.previous_mase
    c.to_csv(OUT/f'switch_events_{scenario}.csv',index=False)
    summary=c[c.previous_model.notna()].groupby('selector',as_index=False).agg(transitions=('switched','size'),switches=('switched','sum'))
    changed=c[c.switched].groupby('selector',as_index=False).agg(mean_selected_minus_previous=('switch_difference','mean'),better=('switch_difference',lambda v:(v< -1e-12).sum()),worse=('switch_difference',lambda v:(v>1e-12).sum()),tied=('switch_difference',lambda v:(abs(v)<=1e-12).sum()))
    summary.merge(changed,on='selector',how='left').to_csv(OUT/f'switch_summary_{scenario}.csv',index=False)
    compare=objective[objective.split.eq('test')].pivot_table(index=['origin','objective'],columns='selector',values='mase');rng=np.random.default_rng(SEED);uncert=[]
    for obj in ['equal_node','equal_level','half_aggregate_half_leaf','leaf_only']:
        q=compare.xs(obj,level='objective').sort_index();n=len(q);starts=rng.integers(0,n,size=(5000,int(np.ceil(n/2))));ix=np.stack([np.concatenate([[j,(j+1)%n] for j in row])[:n] for row in starts])
        for other in ['HistoricalBest','Ridge','LGBM_regret_matched']:
            d=(q.HierarchyLoss-q[other]).to_numpy();boot=d[ix].mean(axis=1);uncert.append({'scenario':scenario,'objective':obj,'comparator':other,'difference':d.mean(),'relative_difference':d.mean()/q[other].mean(),'wins':int((d<-1e-12).sum()),'losses':int((d>1e-12).sum()),'ties':int((abs(d)<=1e-12).sum()),'low':np.quantile(boot,.025),'high':np.quantile(boot,.975),'origins':n})
    pd.DataFrame(uncert).to_csv(OUT/f'paired_uncertainty_{scenario}.csv',index=False)
    return choices,f,metric,objective,pd.DataFrame(uncert)


def experiment(y,s,hier,wins,logs):
    feat,met,forecasts,issues,feature_cost=panel(y,s,hier,wins);allcost=[];results={}
    for scenario,models in [('all_models',MODELS),('no_timesfm',[m for m in MODELS if m!='TimesFM3'])]:
        labels,refs=reference_labels(y,s,wins,met,forecasts,models,scenario);fitted,cost=fit_selectors(feat,met,labels,models,scenario);allcost.append(cost)
        results[scenario]=evaluate(y,s,hier,wins,feat,met,forecasts,refs,fitted,models,scenario)
    training_cost=pd.concat(allcost);training_cost.to_csv(OUT/'selector_training_cost.csv',index=False)
    logs.groupby(['split','model'],as_index=False).agg(fit_seconds=('fit_seconds','sum'),inference_seconds=('inference_seconds','sum'),origins=('origin','count')).to_csv(OUT/'candidate_cost.csv',index=False)
    cost=[]
    for scenario,models in [('all_models',MODELS),('no_timesfm',[m for m in MODELS if m!='TimesFM3'])]:
        selected=logs[logs.model.isin(models)]
        for split in ['train','validation','test']:
            f=selected[selected.split.eq(split)];benchmark=feature_cost[feature_cost.split.eq(split)].feature_seconds.sum();inference=pd.read_csv(OUT/f'selector_inference_{scenario}.csv');q=inference[inference.split.eq(split)]
            for selector in ['GlobalBest','HistoricalBest','Ridge','LGBM_regret_matched','HierarchyLoss']:
                st=training_cost[(training_cost.scenario.eq(scenario))&(training_cost.selector.eq(selector))].fit_seconds
                cost.append({'scenario':scenario,'split':split,'selector':selector,'candidate_fit_seconds':f.fit_seconds.sum(),'candidate_inference_seconds':f.inference_seconds.sum(),'feature_seconds':benchmark if selector in ['Ridge','LGBM_regret_matched','HierarchyLoss'] else 0.,'selector_inference_seconds':q[q.selector.eq(selector)].inference_seconds.sum(),'selector_training_once_seconds':st.iloc[0] if len(st) else 0.,'note':'candidate batch times summed, no routed wall-clock claim; excludes data loading, reconciliation and orchestration; selector training is one-time, not per split'})
    pd.DataFrame(cost).to_csv(OUT/'full_shadow_cost.csv',index=False)
    print('Scale exclusions:',len(issues),'scenarios completed:',list(results),flush=True)
    return results,issues,feature_cost
