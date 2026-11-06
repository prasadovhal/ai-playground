"""Frozen daily M5 protocol. Outputs and cache are isolated in experiment_4."""
from pathlib import Path
import hashlib, inspect, json, time, warnings
import numpy as np
import pandas as pd
from importlib.metadata import version
from statsmodels.tsa.seasonal import STL
from lightgbm import LGBMRegressor
from sklearn.pipeline import make_pipeline
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import Ridge

OUT=Path('experiment_4'); DATA=OUT/'data'; CACHE=OUT/'cache'
for p in [OUT,DATA,CACHE]: p.mkdir(parents=True,exist_ok=True)
STORES=['CA_1','TX_1']; DEPTS=['FOODS_1','FOODS_2','HOUSEHOLD_1','HOUSEHOLD_2','HOBBIES_1','HOBBIES_2']
MODELS=['AutoARIMA','NBEATS','SeasonalNaive','TimesFM3']; H=28; SEASON=7; SEED=42
FEATURES=['trend_strength','seasonal_strength','std','cv','lag1','lag_season','zero_ratio','missing_ratio','recent_growth']
PARAMS=dict(num_leaves=7,max_depth=3,n_estimators=150,min_child_samples=40,reg_lambda=10.,learning_rate=.03,random_state=42,n_jobs=1,verbosity=-1)
MD5={'calendar.csv':'3ffeab2991b0c8e861d008b39ea4c95c','sales_train_evaluation.csv':'b806dfc9f30a745102b708c09951f6aa'}


def inputs():
    for filename,expected in MD5.items():
        path=DATA/filename
        if not path.exists(): raise FileNotFoundError(f'Missing raw M5 data: {path}')
        digest=hashlib.md5(path.read_bytes()).hexdigest()
        if digest!=expected: raise ValueError(f'M5 checksum mismatch: {path}: {digest}')
    sales=DATA/'sales_train_evaluation.csv'; calendar=pd.read_csv(DATA/'calendar.csv')
    calendar=calendar[calendar.d.isin([f'd_{i}' for i in range(1,1942)])][['d','date']].copy()
    calendar['date']=pd.to_datetime(calendar.date); calendar['day']=calendar.d.str[2:].astype(int); calendar=calendar.sort_values('day')
    if len(calendar)!=1941 or calendar.d.duplicated().any() or calendar.date.duplicated().any() or not calendar.date.diff().dropna().eq(pd.Timedelta(days=1)).all(): raise ValueError('Invalid M5 calendar')
    meta=['item_id','dept_id','cat_id','store_id']; early=[f'd_{i}' for i in range(1,731)]
    a=pd.read_csv(sales,usecols=meta+early); a=a[a.store_id.isin(STORES)&a.dept_id.isin(DEPTS)].copy()
    a['early_volume']=a[early].sum(axis=1); a['early_nonzero_days']=a[early].gt(0).sum(axis=1)
    a=a[['item_id','dept_id','cat_id','store_id','early_volume','early_nonzero_days']]
    if a.duplicated(['item_id','store_id']).any(): raise ValueError('Duplicate store-item rows')
    # Fixed, pre-selection eligibility: at least 100 units and 52 nonzero days in each store.
    q=a.pivot(index=['dept_id','cat_id','item_id'],columns='store_id',values=['early_volume','early_nonzero_days'])
    q=q.dropna(); q=q[(q[('early_volume','CA_1')]>=100)&(q[('early_volume','TX_1')]>=100)&(q[('early_nonzero_days','CA_1')]>=52)&(q[('early_nonzero_days','TX_1')]>=52)].copy()
    q['combined_volume']=q[('early_volume','CA_1')]+q[('early_volume','TX_1')]
    q=q.reset_index(); q.columns=['_'.join(c).strip('_') if isinstance(c,tuple) else c for c in q.columns]
    q=q.sort_values(['dept_id','combined_volume','item_id'],ascending=[True,False,True]); chosen=q.groupby('dept_id',sort=True).head(3)
    if len(chosen)!=18 or set(chosen.dept_id)!=set(DEPTS) or not chosen.groupby('dept_id').size().eq(3).all(): raise ValueError('Insufficient eligible products')
    chosen.to_csv(OUT/'selected_products.csv',index=False)
    days=[f'd_{i}' for i in range(1,1942)]; parts=[]
    for chunk in pd.read_csv(sales,usecols=meta+days,chunksize=500):
        parts.append(chunk[chunk.store_id.isin(STORES)&chunk.item_id.isin(chosen.item_id)])
    selected=pd.concat(parts,ignore_index=True).sort_values(['store_id','item_id'])
    if len(selected)!=36 or selected.duplicated(['store_id','item_id']).any(): raise ValueError('Expected 36 store-product observations')
    expected=chosen.set_index('item_id')[['dept_id','cat_id']]
    for row in selected.itertuples(index=False):
        if row.dept_id!=expected.loc[row.item_id,'dept_id'] or row.cat_id!=expected.loc[row.item_id,'cat_id']: raise ValueError('Item mapping mismatch')
    if selected[days].isna().any().any() or (selected[days].to_numpy()<0).any(): raise ValueError('Missing/negative sales')
    selected['leaf_id']='leaf|'+selected.store_id+'|'+selected.item_id
    leaves=selected.leaf_id.tolist(); n=len(leaves)
    rows=[]; levels=[]
    def add(level,key,mask): rows.append((level+'|'+key,np.asarray(mask,dtype=int))); levels.append(level)
    add('total','selected',np.ones(n))
    for store in STORES: add('store',store,selected.store_id.eq(store))
    for cat in sorted(selected.cat_id.unique()): add('category',cat,selected.cat_id.eq(cat))
    for dept in sorted(DEPTS): add('department',dept,selected.dept_id.eq(dept))
    for store in STORES:
        for cat in sorted(selected.cat_id.unique()): add('store_category',store+'|'+cat,selected.store_id.eq(store)&selected.cat_id.eq(cat))
    for store in STORES:
        for dept in sorted(DEPTS): add('store_department',store+'|'+dept,selected.store_id.eq(store)&selected.dept_id.eq(dept))
    for item in sorted(chosen.item_id): add('product',item,selected.item_id.eq(item))
    for leaf in leaves: add('leaf',leaf.split('|',1)[1],selected.leaf_id.eq(leaf))
    ids=[r[0] for r in rows]; matrix=np.stack([r[1] for r in rows]); s=pd.DataFrame(matrix,index=ids,columns=leaves)
    if len(ids)!=84 or len(set(ids))!=84 or len(set(map(tuple,matrix)))!=84 or not np.array_equal(s.loc[leaves].to_numpy(),np.eye(36)): raise ValueError('Invalid hierarchy or duplicate aggregation masks')
    leaf_values=selected[days].to_numpy(dtype=float); values=matrix@leaf_values
    if len(set(map(tuple,values)))!=84: raise ValueError('Distinct hierarchy nodes have duplicate actual series')
    dates=calendar.date.to_numpy(); y=pd.DataFrame(values,index=ids,columns=dates)
    if not np.array_equal(y.to_numpy(),s.to_numpy()@y.loc[leaves].to_numpy()): raise ValueError('Aggregation mismatch')
    if y.isna().any().any(): raise ValueError('Missing observations')
    s.rename_axis('unique_id').to_csv(OUT/'summing_matrix.csv'); pd.DataFrame({'unique_id':ids,'level':levels}).to_csv(OUT/'hierarchy.csv',index=False)
    selected[meta+['leaf_id']].to_csv(OUT/'leaf_mapping.csv',index=False)
    y.rename_axis('unique_id').to_csv(OUT/'actual_series.csv')
    windows=[]
    for start in range(730,1941-H+1,H):
        first=pd.Timestamp(dates[start]); last=pd.Timestamp(dates[start+H-1]); split=None
        if last<=pd.Timestamp('2014-12-31'): split='train'
        elif first>=pd.Timestamp('2015-01-01') and last<=pd.Timestamp('2015-06-30'): split='validation'
        elif first>=pd.Timestamp('2015-07-01'): split='test'
        windows.append({'origin':start,'history_end':pd.Timestamp(dates[start-1]),'target_start':first,'target_end':last,'split':split or 'excluded_boundary'})
    windows=pd.DataFrame(windows); windows.to_csv(OUT/'origins.csv',index=False)
    print('Selected products:',len(chosen),'leaves:',len(leaves),'series:',len(ids),'dates:',dates[0],dates[-1],flush=True)
    print('Origins:',windows.split.value_counts().to_dict(),flush=True)
    return y,s,pd.DataFrame({'unique_id':ids,'level':levels}),windows


_timesfm=None
def predict(model,train,dates):
    global _timesfm
    import torch
    np.random.seed(SEED); torch.manual_seed(SEED)
    t=time.perf_counter()
    if model in ['AutoARIMA','SeasonalNaive']:
        from statsforecast import StatsForecast
        from statsforecast.models import AutoARIMA,SeasonalNaive
        obj=StatsForecast(models=[AutoARIMA(season_length=7,approximation=True,max_p=2,max_q=2,max_P=1,max_Q=1) if model=='AutoARIMA' else SeasonalNaive(season_length=7)],freq='D',n_jobs=4 if model=='AutoARIMA' else 1)
        obj.fit(train); fit=time.perf_counter()-t; t=time.perf_counter(); result=obj.predict(h=H).reset_index()[['unique_id','ds',model]].rename(columns={model:'yhat'})
    elif model=='NBEATS':
        from neuralforecast import NeuralForecast
        from neuralforecast.models import NBEATS
        obj=NeuralForecast(models=[NBEATS(h=H,input_size=2*H,max_steps=100,mlp_units=[[128,128]]*3,scaler_type='robust',random_seed=SEED,enable_progress_bar=False,logger=False,enable_model_summary=False)],freq='D')
        obj.fit(df=train); fit=time.perf_counter()-t; t=time.perf_counter(); result=obj.predict().reset_index()[['unique_id','ds','NBEATS']].rename(columns={'NBEATS':'yhat'})
    else:
        from timesfm3 import TimesFM3Evaluator,ModelConfig
        if _timesfm is None: _timesfm=TimesFM3Evaluator(ModelConfig(checkpoint_path='google/timesfm-3.0-pytorch',per_core_batch_size=4,device='cuda' if torch.cuda.is_available() else 'cpu'))
        fit=time.perf_counter()-t; t=time.perf_counter(); groups=list(train.groupby('unique_id',sort=True))
        # Pretrained inference uses the complete expanding history at each origin.
        contexts=[g.y.to_numpy(dtype=np.float32) for _,g in groups]
        outputs=list(_timesfm.predict_batch(contexts,horizon=H,return_quantiles=False,use_symmetric_averaging=False))
        result=pd.concat([pd.DataFrame({'unique_id':uid,'ds':dates,'yhat':np.asarray(out.forecast)}) for (uid,_),out in zip(groups,outputs)],ignore_index=True)
    return result,fit,time.perf_counter()-t


def cache_forecasts(y,s,windows):
    # Cache identity covers selected data, fixed forecast rules and key package versions.
    sig=hashlib.sha256(pd.util.hash_pandas_object(y,index=True).values.tobytes()+inspect.getsource(predict).encode()+json.dumps({'H':H,'season':SEASON,'seed':SEED,'context':'full_expanding','steps':100,'train_scope':'36_leaves_and_bottom_up','versions':{p:version(p) for p in ['statsforecast','neuralforecast','timesfm','torch','numpy','pandas']}},sort_keys=True).encode()).hexdigest()
    logs=[]
    for w in windows[windows.split.ne('excluded_boundary')].itertuples(index=False):
        dates=y.columns[w.origin:w.origin+H]; train=y.loc[s.columns].iloc[:,:w.origin].rename_axis('unique_id').stack().rename('y').reset_index().rename(columns={'level_1':'ds'})
        for model in MODELS:
            path=CACHE/f'{w.origin}_{model}.csv'; meta=path.with_suffix('.json')
            try:
                ok=False
                if path.exists() and meta.exists():
                    info=json.loads(meta.read_text()); ok=info.get('signature')==sig and info.get('sha256')==hashlib.sha256(path.read_bytes()).hexdigest()
                if ok: f=pd.read_csv(path,parse_dates=['ds']); source='cache'
                else:
                    with warnings.catch_warnings(record=True) as caught:
                        warnings.simplefilter('default'); leaf,fit,infer=predict(model,train,dates)
                    leaf_matrix=leaf.pivot(index='unique_id',columns='ds',values='yhat').reindex(index=s.columns,columns=dates)
                    if leaf_matrix.isna().any().any(): raise ValueError('Incomplete base leaf predictions')
                    f=pd.DataFrame(s.to_numpy()@leaf_matrix.to_numpy(),index=s.index,columns=dates).rename_axis('unique_id').stack().rename('yhat').reset_index().rename(columns={'level_1':'ds'})
                    info={'signature':sig,'fit_seconds':fit,'inference_seconds':infer,'warnings':sorted(set(str(t.message) for t in caught))}; source='computed'
                got=pd.MultiIndex.from_frame(f[['unique_id','ds']]); expected=pd.MultiIndex.from_product([s.index,dates],names=['unique_id','ds'])
                if got.has_duplicates or set(got)!=set(expected) or not np.isfinite(f.yhat).all(): raise ValueError('Missing, duplicate or nonfinite predictions')
                if not ok: f.to_csv(path,index=False); info['sha256']=hashlib.sha256(path.read_bytes()).hexdigest(); meta.write_text(json.dumps(info,indent=2))
                logs.append({'origin':w.origin,'split':w.split,'model':model,'status':'ok','source':source,**info}); print(w.origin,w.split,model,source,'ok',flush=True)
            except Exception as exc:
                logs.append({'origin':w.origin,'split':w.split,'model':model,'status':'failed','error':repr(exc)}); print(w.origin,model,'FAILED',repr(exc),flush=True)
            pd.DataFrame(logs).to_csv(OUT/'forecast_status.csv',index=False)
    if any(v['status']!='ok' for v in logs): raise RuntimeError('Forecast failures; inspect forecast_status.csv; comparisons halted')
    return pd.DataFrame(logs)


def freeze_protocol():
    source_sha=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    spec={'source_sha256':source_sha,'selection':'first 730 days; >=100 units and >=52 positive days in each store; combined sales descending, item ID ascending','hierarchy':'84 selected-subset nodes, 36 leaves; bottom-up','origins':'anchored 28-day windows from d_731; boundary-crossing windows excluded','partitions':'training through 2014-12-31; validation January–June 2015; final test July 2015 onward','weekly_mase_lag':7,'horizon':28,'candidate_rules':'36 leaves; AutoARIMA approximation p/q<=2 P/Q<=1 season 7; NBEATS input 56 steps 100; TimesFM 3 pretrained full expanding context; SeasonalNaive season 7','selector_rules':'Experiment 2B/3 designs retrained on M5 training origins only; no tuning; matched FEATURES plus recent candidate MASE for regret and hierarchy loss','reference':'historical per-leaf MASE over earlier completed windows; SeasonalNaive before the first','online_updates':'global and historical choices, and last-three-window candidate-performance features use completed prior windows, including prior validation/test windows; learned parameters remain frozen','uncertainty':'paired origin-level circular moving-block bootstrap; length 2 origins; 5000 samples; fixed seed 42','zero_scales':'omit the identical invalid series-origin from all selector metrics and hierarchy-loss objective; record exclusions','warnings':'AutoARIMA worker optimizer convergence warnings observed in preflight; worker warning counts are not captured','versions':{p:version(p) for p in ['statsforecast','neuralforecast','timesfm','lightgbm','scikit-learn','numpy','pandas','torch']}}
    path=OUT/'protocol_lock.json'
    if path.exists():
        prior=json.loads(path.read_text())
        if prior!=spec: raise RuntimeError('Frozen protocol differs from code or package versions; do not overwrite the final experiment')
    else: path.write_text(json.dumps(spec,indent=2))
    return spec


def make_panel(y,s,hierarchy,windows):
    if windows.split.eq('test').any(): freeze_protocol()
    levels=hierarchy.set_index('unique_id').level; ids=list(s.index); leaves=list(s.columns)
    previous=[]; features=[]; metrics=[]; targets=[]; references=[]; scale_log=[]
    for w in windows[windows.split.ne('excluded_boundary')].itertuples(index=False):
        history=y.iloc[:,:w.origin]; truth=y.iloc[:,w.origin:w.origin+H].to_numpy()
        scales=np.mean(np.abs(history.to_numpy()[:,SEASON:]-history.to_numpy()[:,:-SEASON]),axis=1)
        valid=np.isfinite(scales)&(scales>0)
        for uid,scale,good in zip(ids,scales,valid):
            if not good: scale_log.append({'origin':w.origin,'unique_id':uid,'scale':scale,'reason':'nonfinite or zero seasonal scale'})
        if not valid.any(): raise ValueError(f'No valid scales at origin {w.origin}')
        past=pd.DataFrame(previous)
        if past.empty:
            ref=pd.Series('SeasonalNaive',index=leaves); global_best='SeasonalNaive'; last_completed=np.nan
        else:
            earlier=past[past.unique_id.isin(leaves)]
            ref=earlier.groupby(['unique_id','model']).mase.mean().unstack().reindex(index=leaves,columns=MODELS).idxmin(axis=1).fillna('SeasonalNaive')
            model_means=earlier.groupby('model').mase.mean().reindex(MODELS); global_best=model_means.idxmin() if model_means.notna().any() else 'SeasonalNaive'
            last_completed=int(past.origin.max())
        for uid in leaves: references.append({'origin':w.origin,'unique_id':uid,'reference_model':ref.loc[uid],'global_model':global_best,'last_completed_origin':last_completed})
        predictions={}
        for model in MODELS:
            f=pd.read_csv(CACHE/f'{w.origin}_{model}.csv',parse_dates=['ds']); predictions[model]=f.pivot(index='unique_id',columns='ds',values='yhat').reindex(index=ids,columns=y.columns[w.origin:w.origin+H]).to_numpy()
            if not np.isfinite(predictions[model]).all(): raise ValueError('Incomplete model predictions')
            err=predictions[model]-truth; mae=np.mean(abs(err),axis=1); rmse=np.sqrt(np.mean(err**2,axis=1))
            for i,uid in enumerate(ids): metrics.append({'origin':w.origin,'split':w.split,'unique_id':uid,'model':model,'scale':scales[i],'valid_scale':bool(valid[i]),'mae':mae[i],'rmse':rmse[i],'mase':mae[i]/scales[i] if valid[i] else np.nan})
        leaf_positions=[ids.index(uid) for uid in leaves]
        selected=np.stack([predictions[ref.loc[uid]][i] for uid,i in zip(leaves,leaf_positions)])
        baseline=s.to_numpy()@selected
        def loss(z): return np.mean(np.mean(abs(z-truth),axis=1)[valid]/scales[valid])
        baseline_loss=loss(baseline)
        for j,(uid,i) in enumerate(zip(leaves,leaf_positions)):
            arr=history.loc[uid].to_numpy(); mu=arr.mean(); sd=arr.std()
            if sd>1e-12:
                stl=STL(arr,period=7,robust=True).fit(); vr=np.var(stl.resid)
                trend=max(0.,1-vr/max(np.var(stl.trend+stl.resid),1e-12)); seasonal=max(0.,1-vr/max(np.var(stl.seasonal+stl.resid),1e-12))
            else: trend=seasonal=np.nan
            def ac(lag):
                return np.corrcoef(arr[:-lag],arr[lag:])[0,1] if len(arr)>lag and np.std(arr[:-lag])>1e-12 and np.std(arr[lag:])>1e-12 else np.nan
            growth=(arr[-H:].mean()-arr[-2*H:-H].mean())/abs(arr[-2*H:-H].mean()) if abs(arr[-2*H:-H].mean())>1e-12 else np.nan
            shared={'origin':w.origin,'split':w.split,'unique_id':uid,'trend_strength':trend,'seasonal_strength':seasonal,'std':sd,'cv':sd/abs(mu) if abs(mu)>1e-12 else np.nan,'lag1':ac(1),'lag_season':ac(7),'zero_ratio':np.mean(arr==0),'missing_ratio':0.,'recent_growth':growth}
            for model in MODELS:
                prev=past[(past.unique_id.eq(uid))&(past.model.eq(model))].tail(3) if not past.empty else pd.DataFrame()
                rec=prev.mase.mean() if not prev.empty else np.nan
                features.append({**shared,'model':model,'recent_candidate_mase':rec,'last_completed_origin':last_completed})
                if w.split=='train':
                    changed=baseline+s.to_numpy()[:,j,None]*(predictions[model][i]-selected[j])[None,:]
                    targets.append({'origin':w.origin,'unique_id':uid,'model':model,'reference_model':ref.loc[uid],'reference_loss':baseline_loss,'counterfactual_loss':loss(changed),'loss_change':loss(changed)-baseline_loss})
        # Append only after all features/reference choices are computed, preventing current-window leakage.
        previous.extend([m for m in metrics if m['origin']==w.origin and m['unique_id'] in leaves])
    met=pd.DataFrame(metrics); feat=pd.DataFrame(features); target=pd.DataFrame(targets); reference=pd.DataFrame(references)
    issues=pd.DataFrame(scale_log,columns=['origin','unique_id','scale','reason']); issues.to_csv(OUT/'scale_exclusions.csv',index=False)
    if not reference.last_completed_origin.dropna().lt(reference.loc[reference.last_completed_origin.notna(),'origin']).all(): raise ValueError('Future performance leaked into reference')
    if not target[target.model.eq(target.reference_model)].loss_change.abs().lt(1e-9).all(): raise ValueError('Reference counterfactual is not zero')
    met.to_csv(OUT/'forecast_metrics.csv',index=False); feat.to_csv(OUT/'selector_features.csv',index=False); target.to_csv(OUT/'counterfactual_labels.csv',index=False); reference.to_csv(OUT/'reference_assignments.csv',index=False)
    return feat,met,target,reference,issues


def fit_selectors(feat,met,targets):
    train=feat[feat.split.eq('train')].merge(met[['origin','unique_id','model','mase','valid_scale']],on=['origin','unique_id','model'],validate='one_to_one')
    # Invalid leaf-scale rows cannot provide comparable training labels; retain common valid groups only.
    valid=train.groupby(['origin','unique_id']).valid_scale.transform('all'); train=train[valid].copy()
    train['regret']=train.mase-train.groupby(['origin','unique_id']).mase.transform('min')
    counter=train.merge(targets[['origin','unique_id','model','loss_change']],on=['origin','unique_id','model'],validate='one_to_one')
    models={}; times=[]
    for selector in ['Ridge','LGBM_regret_matched','HierarchyLoss']:
        models[selector]={}; start=time.perf_counter()
        for model in MODELS:
            part=(counter if selector=='HierarchyLoss' else train); part=part[part.model.eq(model)]
            cols=FEATURES if selector=='Ridge' else FEATURES+['recent_candidate_mase']
            obj=make_pipeline(SimpleImputer(strategy='median',keep_empty_features=True),StandardScaler(),Ridge(alpha=10.)) if selector=='Ridge' else LGBMRegressor(**PARAMS)
            target=part.loss_change if selector=='HierarchyLoss' else part.mase if selector=='Ridge' else part.regret
            obj.fit(part[cols],target); models[selector][model]=obj
        times.append({'selector':selector,'training_seconds':time.perf_counter()-start,'training_origins':train.origin.nunique(),'training_rows':len(train)})
    pd.DataFrame(times).to_csv(OUT/'selector_training_cost.csv',index=False)
    return models


def evaluate(y,s,hierarchy,windows,feat,met,reference,models,forecast_logs):
    freeze_protocol()
    choices=[]; timings=[]
    for w in windows[windows.split.isin(['validation','test'])].itertuples(index=False):
        q=feat[feat.origin.eq(w.origin)]; r=reference[reference.origin.eq(w.origin)].set_index('unique_id')
        for uid in s.columns:
            choices.extend([{'origin':w.origin,'split':w.split,'unique_id':uid,'selector':'GlobalBest','model':r.loc[uid,'global_model']},{'origin':w.origin,'split':w.split,'unique_id':uid,'selector':'HistoricalBest','model':r.loc[uid,'reference_model']}])
        for selector in ['Ridge','LGBM_regret_matched','HierarchyLoss']:
            start=time.perf_counter(); candidates=[]
            for model in MODELS:
                a=q[q.model.eq(model)].sort_values('unique_id'); cols=FEATURES if selector=='Ridge' else FEATURES+['recent_candidate_mase']
                candidates.append(a[['unique_id','model']].assign(score=models[selector][model].predict(a[cols])))
            scores=pd.concat(candidates).sort_values(['unique_id','score','model']); picked=scores.drop_duplicates('unique_id')
            timings.append({'origin':w.origin,'selector':selector,'inference_seconds':time.perf_counter()-start})
            for row in picked.itertuples(index=False): choices.append({'origin':w.origin,'split':w.split,'unique_id':row.unique_id,'selector':selector,'model':row.model})
    choices=pd.DataFrame(choices); choices.to_csv(OUT/'selector_predictions.csv',index=False)
    forecasts=[]
    for (origin,selector),q in choices.groupby(['origin','selector']):
        w=windows[windows.origin.eq(origin)].iloc[0]; dates=y.columns[origin:origin+H]; bottom=[]
        for uid in s.columns:
            model=q[q.unique_id.eq(uid)].model.iloc[0]; c=pd.read_csv(CACHE/f'{origin}_{model}.csv',parse_dates=['ds']); bottom.append(c[c.unique_id.eq(uid)].sort_values('ds').yhat.to_numpy())
        bottom=np.stack(bottom); result=s.to_numpy()@bottom
        if not np.allclose(result,s.to_numpy()@result[[list(s.index).index(uid) for uid in s.columns]]): raise ValueError('Incoherent forecasts')
        forecast=pd.DataFrame(result,index=s.index,columns=dates).rename_axis('unique_id').stack().rename('forecast').reset_index().rename(columns={'level_1':'ds'})
        forecasts.append(forecast.assign(origin=origin,selector=selector,split=w['split']))
    forecasts=pd.concat(forecasts,ignore_index=True)
    actual=y.rename_axis('unique_id').stack().rename('y').reset_index().rename(columns={'level_1':'ds'})
    forecasts=forecasts.merge(actual,on=['unique_id','ds'],validate='many_to_one')
    scales=met[['origin','unique_id','scale','valid_scale']].drop_duplicates()
    forecasts=forecasts.merge(scales,on=['origin','unique_id'],validate='many_to_one').merge(hierarchy,on='unique_id',validate='many_to_one')
    forecasts['ae']=abs(forecasts.forecast-forecasts.y); forecasts['se']=(forecasts.forecast-forecasts.y)**2
    m=forecasts.groupby(['origin','split','selector','unique_id','level'],as_index=False).agg(mae=('ae','mean'),mse=('se','mean'),scale=('scale','first'),valid_scale=('valid_scale','first'),n=('y','size'))
    m['rmse']=np.sqrt(m.pop('mse'));m['mase']=m.mae/m.scale.where(m.valid_scale)
    if not m.n.eq(H).all(): raise ValueError('Incomplete evaluation windows')
    forecasts.to_csv(OUT/'reconciled_forecasts.csv',index=False); m.to_csv(OUT/'selector_metrics.csv',index=False)
    per_origin=m.groupby(['origin','split','selector'],as_index=False).agg(mase=('mase','mean'),mae=('mae','mean'),rmse=('rmse','mean'),valid_series=('mase','count')).merge(windows[['origin','target_start','target_end']],on='origin',validate='many_to_one')
    per_origin.to_csv(OUT/'per_origin_comparison.csv',index=False)
    per_origin.groupby(['split','selector'],as_index=False).agg(mase=('mase','mean'),mae=('mae','mean'),rmse=('rmse','mean'),origins=('origin','count')).to_csv(OUT/'overall_comparison.csv',index=False)
    m.groupby(['split','selector','level'],as_index=False).agg(mase=('mase','mean'),mae=('mae','mean'),rmse=('rmse','mean'),n=('mase','count')).to_csv(OUT/'level_comparison.csv',index=False)
    choices.groupby(['split','selector','model']).size().rename('count').reset_index().to_csv(OUT/'selection_frequency.csv',index=False)
    stable=[]
    for selector,q in choices.sort_values('origin').groupby('selector'):
        for uid,g in q.groupby('unique_id'):
            for split,k in g.groupby('split'):
                stable.append({'selector':selector,'split':split,'unique_id':uid,'transitions':max(len(k)-1,0),'changes':int(k.model.ne(k.model.shift()).iloc[1:].sum())})
    pd.DataFrame(stable).to_csv(OUT/'selection_stability.csv',index=False); pd.DataFrame(timings).to_csv(OUT/'selector_inference_cost.csv',index=False)
    # Timing measured on complete 36-leaf candidate batches; allocations are estimates only.
    costs=choices.merge(forecast_logs[['origin','model','fit_seconds','inference_seconds']],on=['origin','model'],validate='many_to_one'); costs['allocated_inference_seconds']=costs.inference_seconds/36
    costs.groupby(['origin','split','selector'],as_index=False).allocated_inference_seconds.sum().to_csv(OUT/'forecast_cost_estimates.csv',index=False)
    forecast_logs.groupby(['split','model'],as_index=False).agg(forecast_fit_seconds=('fit_seconds','sum'),forecast_inference_seconds=('inference_seconds','sum'),origins=('origin','count')).to_csv(OUT/'forecast_measured_cost.csv',index=False)
    per=m.groupby(['origin','split','selector'],as_index=False).mase.mean(); test=per[per.split.eq('test')].pivot(index='origin',columns='selector',values='mase').sort_index()
    paired=[]
    for selector in test.columns:
        delta=test[selector]-test.HistoricalBest
        for origin,value in delta.items(): paired.append({'origin':origin,'selector':selector,'comparison':'minus HistoricalBest','difference':value,'result':'win' if value < -1e-12 else 'loss' if value > 1e-12 else 'tie'})
    paired=pd.DataFrame(paired).merge(windows[['origin','target_start','target_end']],on='origin',validate='many_to_one')
    paired.to_csv(OUT/'paired_vs_historical_by_origin.csv',index=False)
    paired.groupby(['selector','result']).size().rename('count').reset_index().to_csv(OUT/'wins_vs_historical.csv',index=False)
    rng=np.random.default_rng(SEED); uncertainty=[]
    for comparator in ['HistoricalBest','Ridge','LGBM_regret_matched']:
        d=(test.HierarchyLoss-test[comparator]).dropna().to_numpy(); n=len(d)
        if n<2: raise ValueError('Too few test origins for paired comparison')
        # Circular moving-block bootstrap on origins only, not correlated hierarchy nodes.
        sampled=[]
        for _ in range(5000):
            starts=rng.integers(0,n,size=int(np.ceil(n/2))); ix=np.concatenate([np.array([j,(j+1)%n]) for j in starts])[:n]; sampled.append(d[ix].mean())
        uncertainty.append({'comparison':'HierarchyLoss minus '+comparator,'n_origins':n,'mean_difference':d.mean(),'wins':int((d<-1e-12).sum()),'losses':int((d>1e-12).sum()),'ties':int((abs(d)<=1e-12).sum()),'block_bootstrap_low':np.quantile(sampled,.025),'block_bootstrap_high':np.quantile(sampled,.975),'block_length_origins':2,'resamples':5000})
    pd.DataFrame(uncertainty).to_csv(OUT/'paired_origin_uncertainty.csv',index=False)
    return choices,forecasts,m,pd.DataFrame(uncertainty)
