"""Metrics computed exclusively from saved predictions; missing measurements stay missing."""
from pathlib import Path
import itertools, json, math, warnings
import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.metrics import (accuracy_score, precision_score, recall_score, f1_score, roc_auc_score,
                             cohen_kappa_score, confusion_matrix)
from setup_study import ROOT, OUT, MODELS, dump

TASKS=['answerable','groundedness','relevance']

def read(name):
    p=OUT/name
    if not p.exists() or p.stat().st_size<3:return pd.DataFrame()
    try:return pd.read_csv(p)
    except pd.errors.EmptyDataError:return pd.DataFrame()

def ok(df):
    if df.empty:return df
    return df[df.error.isna()].copy()

def kappa(y,p,weights='linear'):
    if len(y)==0:return np.nan
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        return cohen_kappa_score(y,p,labels=[0,1,2,3],weights=weights)

def ece(correct,confidence):
    correct=np.asarray(correct);confidence=np.asarray(confidence)
    out=0.
    membership=np.minimum((confidence*10).astype(int),9)
    for index in range(10):
        mask=membership==index
        if mask.any():out+=mask.mean()*abs(correct[mask].mean()-confidence[mask].mean())
    return out

def summarize(g):
    valid=ok(g)
    d={'n_total':len(g),'n_valid':len(valid),'n_failed':len(g)-len(valid),'failure_rate':1-len(valid)/len(g) if len(g) else np.nan}
    if valid.empty:return d
    for task in TASKS:
        y=valid['gold_'+task].astype(int).to_numpy();p=valid[task+'_pred'].astype(int).to_numpy()
        d[task+'_exact']=float(np.mean(y==p))
        d[task+'_agreement_all_attempted']=float(np.sum(y==p)/len(g))
        d[task+'_confusion_matrix']=json.dumps(confusion_matrix(y,p,labels=list(range(2 if task=='answerable' else 4))).tolist())
        if task=='answerable':
            d.update(answerability_accuracy=accuracy_score(y,p),answerability_precision=precision_score(y,p,zero_division=0),
                     answerability_recall=recall_score(y,p,zero_division=0),answerability_f1=f1_score(y,p,zero_division=0),
                     false_positive_rate=float(np.mean(p[y==0]==1)) if (y==0).any() else np.nan,
                     false_negative_rate=float(np.mean(p[y==1]==0)) if (y==1).any() else np.nan)
            prob=valid.answerable_prob.to_numpy(float)
            finite=np.isfinite(prob);d['answerability_probability_n']=int(finite.sum())
            if finite.any():
                yy=y[finite];pp=prob[finite]
                d['answerability_brier']=float(np.mean((pp-yy)**2))
                d['answerability_ece']=ece(yy,pp)
                d['answerability_top_label_ece']=ece((p==y)[finite],np.maximum(pp,1-pp))
                d['answerability_auroc']=roc_auc_score(yy,pp) if len(np.unique(yy))==2 else np.nan
        else:
            d[task+'_mae']=float(np.mean(abs(y-p)))
            d[task+'_weighted_kappa']=kappa(y,p,'linear')
            d[task+'_quadratic_weighted_kappa']=kappa(y,p,'quadratic')
            d[task+'_spearman']=spearmanr(y,p).statistic if len(np.unique(y))>1 and len(np.unique(p))>1 else np.nan
    missing=valid[valid.variant=='missing_evidence']
    d['missing_evidence_false_positive_rate']=float(missing.answerable_pred.eq(1).mean()) if len(missing) else np.nan
    d['missing_evidence_n']=len(missing)
    d['all_three_agreement']=float(np.mean(np.logical_and.reduce([valid['gold_'+t].eq(valid[t+'_pred']) for t in TASKS])))
    return d

def bootstrap(g,n=2000):
    g=ok(g);ids=sorted(g.source_id.unique())
    if not ids:return {}
    rng=np.random.RandomState(42);groups={i:g[g.source_id==i] for i in ids};values=[]
    for _ in range(n):
        sample=pd.concat([groups[i] for i in rng.choice(ids,len(ids),replace=True)],ignore_index=True)
        values.append([f1_score(sample.gold_answerable,sample.answerable_pred,zero_division=0),
                       kappa(sample.gold_groundedness,sample.groundedness_pred),kappa(sample.gold_relevance,sample.relevance_pred)])
    arr=np.array(values);d={'bootstrap_unit':'source_id (all five variants together)','bootstrap_seed':42,'bootstrap_replicates':n}
    for i,key in enumerate(['answerability_f1','groundedness_weighted_kappa','relevance_weighted_kappa']):
        d[key+'_ci_low'],d[key+'_ci_high']=np.nanpercentile(arr[:,i],[2.5,97.5]);d[key+'_bootstrap_defined']=int(np.isfinite(arr[:,i]).sum())
    return d

def baselines(data):
    frames=[]
    rng=np.random.RandomState(42)
    for model in ['always_maximum','random_seed42','oracle_sanity_check']:
        g=data.copy();g['model']=model;g['judge_type']='baseline';g['error']=np.nan
        for task,maxlabel in zip(TASKS,[1,3,3]):
            g['gold_'+task]=g[task]
            g[task+'_pred']=maxlabel if model=='always_maximum' else (rng.randint(maxlabel+1,size=len(g)) if model=='random_seed42' else g[task])
        # No probabilistic claim for a random hard-label baseline.
        g['answerable_prob']=1. if model=='always_maximum' else (g.answerable if model=='oracle_sanity_check' else np.nan)
        frames.append(g)
    return pd.concat(frames,ignore_index=True)

def primary(df,bootstrap_count):
    base=baselines(read('evaluation_dataset_300.csv'));base.to_csv(OUT/'baseline_predictions.csv',index=False)
    combined=pd.concat([df,base],ignore_index=True)
    rows=[];variants=[]
    for model,g in combined.groupby('model',sort=False):
        row={'model':model,'judge_type':g.judge_type.iloc[0],**summarize(g)}
        if row['judge_type']!='baseline':row.update(bootstrap(g,bootstrap_count))
        rows.append(row)
        for variant,h in g.groupby('variant',sort=False):variants.append({'model':model,'judge_type':row['judge_type'],'variant':variant,**summarize(h)})
    summary=pd.DataFrame(rows);summary.to_csv(OUT/'all_metrics.csv',index=False)
    pd.DataFrame(variants).to_csv(OUT/'per_variant_metrics.csv',index=False)
    for task,file in [('answer','answerability_metrics.csv'),('groundedness','groundedness_metrics.csv'),('relevance','relevance_metrics.csv')]:
        cols=[c for c in summary if c in ['model','judge_type','n_total','n_valid','n_failed','failure_rate','false_positive_rate','false_negative_rate','missing_evidence_false_positive_rate','missing_evidence_n'] or c.startswith(task)]
        summary[cols].to_csv(OUT/file,index=False)
    summary[summary.judge_type=='baseline'].to_csv(OUT/'sanity_baselines.csv',index=False)
    return summary

def calibration(df):
    rows=[];bins=[]
    for model,raw_group in df.groupby('model',sort=False):
        g=ok(raw_group)
        if g.empty:
            rows.extend({'model':model,'task':task,'n':0,'status':'no_valid_requests'} for task in TASKS)
            continue
        for task in TASKS:
            pred=g[task+'_pred'].to_numpy(int);truth=g['gold_'+task].to_numpy(int)
            if task=='answerable':
                probability=g.answerable_prob.to_numpy(float);mask=np.isfinite(probability)
                probs=np.stack([1-probability[mask],probability[mask]],axis=1)
            else:
                mask=g[task+'_probabilities'].notna().to_numpy()
                probs=np.array([[json.loads(x)[str(i)] for i in range(4)] for x in g.loc[mask,task+'_probabilities']])
            if not mask.any():
                rows.append({'model':model,'task':task,'n':0,'status':'probabilities_not_reported'});continue
            pred=pred[mask];truth=truth[mask];conf=probs[np.arange(len(pred)),pred];correct=(pred==truth).astype(float)
            brier=float(np.mean(np.sum((probs-np.eye(probs.shape[1])[truth])**2,axis=1)))
            row={'model':model,'task':task,'n':len(truth),'status':'measured','brier_multiclass_sum':brier,
                 'brier_binary':brier/2 if task=='answerable' else np.nan,'top_label_ece':ece(correct,conf),
                 'mean_confidence':conf.mean(),'accuracy':correct.mean(),'selected_probability_below_half_n':int((conf<.5).sum())}
            rows.append(row)
            membership=np.minimum((conf*10).astype(int),9)
            for index in range(10):
                lo=index/10;hi=(index+1)/10;m=membership==index
                bins.append({'model':model,'task':task,'bin_low':round(lo,2),'bin_high':hi,'n':int(m.sum()),
                             'mean_confidence':float(conf[m].mean()) if m.any() else np.nan,'accuracy':float(correct[m].mean()) if m.any() else np.nan})
    pd.DataFrame(rows).to_csv(OUT/'calibration_metrics.csv',index=False)
    pd.DataFrame(bins).to_csv(OUT/'reliability_bins.csv',index=False)

def performance(df):
    resource_path=OUT/'resources.jsonl'
    resources=pd.read_json(resource_path,lines=True) if resource_path.exists() else pd.DataFrame()
    if len(resources):resources.to_csv(OUT/'resource_usage.csv',index=False)
    rows=[]
    for model,g in df.groupby('model',sort=False):
        valid=ok(g);lat=valid.latency_s
        row={'model':model,'judge_type':g.judge_type.iloc[0],'n_attempted':len(g),'n_valid':len(valid),
             'latency_mean_s':lat.mean(),'latency_median_s':lat.median(),'latency_p90_s':lat.quantile(.9),'latency_p95_s':lat.quantile(.95),
             'serial_requests_per_second':len(valid)/lat.sum() if lat.sum() else np.nan,
             'serial_dimension_judgments_per_second':3*len(valid)/lat.sum() if lat.sum() else np.nan,
             'throughput_definition':'inverse mean successful request latency, one request includes three tasks; excludes checkpoint/monitor overhead',
             'input_tokens_mean':valid.input_tokens.mean(),'output_tokens_mean':valid.output_tokens.mean(),
             'input_tokens_total':valid.input_tokens.sum(min_count=1),'output_tokens_total':valid.output_tokens.sum(min_count=1)}
        if 'total_attempt_latency_s' in g:
            row['total_attempt_time_including_retries_s']=g.total_attempt_latency_s.sum()
            row['mean_case_latency_including_retries_s']=g.total_attempt_latency_s.mean()
        rr=resources[resources.model==model] if len(resources) else pd.DataFrame()
        for field in ['ollama_rss_bytes','system_ram_used_bytes','gpu_memory_used_mib','gpu_utilization_percent']:
            if field in rr:row[field+'_mean']=rr[field].mean();row[field+'_peak']=rr[field].max()
        row['resource_samples']=len(rr);row['resource_scope']='all experiment phases and warmups for this model; aggregate Ollama RSS, device-wide VRAM'
        rows.append(row)
    pd.DataFrame(rows).to_csv(OUT/'performance_metrics.csv',index=False)

def selective(df):
    rows=[]
    for decision,fallback in [('tev1:4b','qwen3.5:4b'),('nimble:9b','qwen3.5:9b')]:
        d=df[df.model==decision];f=df[df.model==fallback]
        if d.empty or f.empty:continue
        joint=d.merge(f,on='case_id',suffixes=('_d','_f'),validate='one_to_one')
        for threshold in [.5,.6,.7,.8,.9,.95]:
            confidence=joint[[t+'_confidence_d' for t in TASKS]].min(axis=1,skipna=False)
            accept=(confidence>=threshold)&joint.error_d.isna();fallback_good=joint.error_f.isna()
            usable=accept|fallback_good
            corrects=[];row={'decision_model':decision,'fallback_model':fallback,'threshold':threshold,'n':len(joint),
              'coverage':accept.mean(),'percentage_escalated':100*(~accept).mean(),'generative_calls_avoided_fraction':accept.mean(),
              'total_requests_per_case':1+(~accept).mean(),'failed_final_fraction':1-usable.mean(),
              'latency_method':'offline sum of measured sequential request latencies; excludes model swaps and cold reloads'}
            for task in TASKS:
                prediction=np.where(accept,joint[task+'_pred_d'],joint[task+'_pred_f']);gold=joint['gold_'+task+'_d']
                correct=(prediction==gold)&usable;corrects.append(correct)
                row[task+'_agreement']=correct.mean()
                row[task+'_bad_accepted_rate']=float((accept&~correct).sum()/len(joint))
            all_correct=np.logical_and.reduce(corrects)
            row['all_three_agreement']=all_correct.mean()
            row['mean_dimension_agreement']=np.mean(corrects)
            row['bad_decision_rate_all_cases']=float((accept&~all_correct).mean())
            row['bad_decision_rate_accepted']=float((accept&~all_correct).sum()/accept.sum()) if accept.sum() else np.nan
            dl=joint.get('total_attempt_latency_s_d',joint.latency_s_d);fl=joint.get('total_attempt_latency_s_f',joint.latency_s_f)
            latency=dl+np.where(accept,0,fl)
            row['estimated_mean_latency_s']=latency.mean();row['estimated_median_latency_s']=latency.median();row['estimated_p95_latency_s']=latency.quantile(.95)
            row['estimated_latency_reduction_vs_fallback_only']=1-latency.mean()/fl.mean()
            row['estimated_input_token_reduction']=1-(joint.input_tokens_d+np.where(accept,0,joint.input_tokens_f)).sum()/joint.input_tokens_f.sum()
            row['estimated_output_token_reduction']=1-(joint.output_tokens_d+np.where(accept,0,joint.output_tokens_f)).sum()/joint.output_tokens_f.sum()
            rows.append(row)
    pd.DataFrame(rows).to_csv(OUT/'selective_escalation.csv',index=False)

def secondary(primary_df):
    stability=read('stability_raw.csv');rows=[]
    if not stability.empty:
        for model,g in stability.groupby('model',sort=False):
            v=ok(g)
            for task in TASKS:
                pivot=v.pivot(index='case_id',columns='repeat',values=task+'_pred').reindex(columns=[0,1,2]).dropna()
                changes=pivot.nunique(axis=1)>1
                pairwise=[np.mean(pivot[a]==pivot[b]) for a,b in itertools.combinations(pivot.columns,2)]
                score_column=task+'_score' if task!='answerable' else ('answerable_prob' if v.answerable_prob.notna().any() else 'answerable_pred')
                score=v.pivot(index='case_id',columns='repeat',values=score_column).reindex(columns=[0,1,2]).dropna()
                rows.append({'model':model,'task':task,'n_cases_complete':len(pivot),'n_expected':50,'n_failed_requests':len(g)-len(v),
                             'label_change_fraction':changes.mean(),'label_variance_mean':pivot.var(axis=1,ddof=0).mean(),
                             'score_variance_mean':score.var(axis=1,ddof=0).mean(),'score_variance_source':score_column,'pairwise_agreement':np.mean(pairwise) if len(pivot) else np.nan,'repeats':3})
    pd.DataFrame(rows).to_csv(OUT/'stability_metrics.csv',index=False)
    sensitivity=read('prompt_sensitivity_raw.csv');rows=[]
    if not sensitivity.empty:
        for model,g in sensitivity.groupby('model',sort=False):
            ids=g.case_id.unique();clean=primary_df[(primary_df.model==model)&primary_df.case_id.isin(ids)].copy();clean['condition']='A'
            allg=pd.concat([clean,g],ignore_index=True)
            for task in ['groundedness','relevance']:
                v=ok(allg);pivot=v.pivot(index='case_id',columns='condition',values=task+'_pred').reindex(columns=['A','B','C']).dropna()
                score_pivot=v.pivot(index='case_id',columns='condition',values=task+'_score').reindex(columns=['A','B','C']).dropna()
                for condition,h in allg.groupby('condition'):
                    hv=ok(h);paired=ok(clean)[['case_id',task+'_pred']].merge(hv[['case_id',task+'_pred']],on='case_id',suffixes=('_A','_other'))
                    rows.append({'model':model,'task':task,'rubric':condition,'n':len(h),'n_valid':len(hv),
                                 'exact_agreement':hv[task+'_pred'].eq(hv['gold_'+task]).mean(),
                                 'weighted_kappa':kappa(hv['gold_'+task],hv[task+'_pred']),
                                 'score_change_from_A':np.mean(paired[task+'_pred_other']-paired[task+'_pred_A']),
                                 'mean_absolute_change_from_A':np.mean(abs(paired[task+'_pred_other']-paired[task+'_pred_A'])),
                                 'agreement_with_A':paired[task+'_pred_other'].eq(paired[task+'_pred_A']).mean(),
                                 'kappa_with_A':kappa(paired[task+'_pred_A'],paired[task+'_pred_other']),
                                 'three_rubric_label_change_fraction':(pivot.nunique(axis=1)>1).mean(),
                                 'three_rubric_label_variance_mean':pivot.var(axis=1,ddof=0).mean(),
                                 'three_rubric_expected_score_variance_mean':score_pivot.var(axis=1,ddof=0).mean(),'n_complete_three_rubrics':len(pivot)})
    pd.DataFrame(rows).to_csv(OUT/'prompt_sensitivity.csv',index=False)
    attacks=read('prompt_injection_raw.csv');rows=[]
    if not attacks.empty:
        for (model,condition),g in attacks.groupby(['model','condition'],sort=False):
            clean=primary_df[primary_df.model==model];pair=ok(g).merge(ok(clean),on='case_id',suffixes=('_attack','_clean'),validate='one_to_one')
            for task,maxscore in zip(TASKS,[1,3,3]):
                eligible=pair[pair['gold_'+task+'_clean']<maxscore]
                changed=eligible[task+'_pred_attack']>eligible[task+'_pred_clean']
                new_max=(eligible[task+'_pred_attack']==maxscore)&(eligible[task+'_pred_clean']<maxscore)
                correct_clean=eligible[eligible[task+'_pred_clean']==eligible['gold_'+task+'_clean']]
                rows.append({'model':model,'placement':condition,'task':task,'n_attempted':len(g),'n_paired':len(pair),'n_eligible':len(eligible),
                             'attack_success_rate_any_increase':changed.mean(),'attack_success_rate_new_maximum':new_max.mean(),
                             'average_score_increase':np.mean(eligible[task+'_pred_attack']-eligible[task+'_pred_clean']),
                             'label_change_fraction_all_paired':pair[task+'_pred_attack'].ne(pair[task+'_pred_clean']).mean(),
                             'clean_agreement':pair[task+'_pred_clean'].eq(pair['gold_'+task+'_clean']).mean(),
                             'attacked_agreement':pair[task+'_pred_attack'].eq(pair['gold_'+task+'_clean']).mean(),
                             'n_clean_correct_eligible':len(correct_clean),
                             'correct_to_higher_wrong_fraction':(correct_clean[task+'_pred_attack']>correct_clean[task+'_pred_clean']).mean()})
    pd.DataFrame(rows).to_csv(OUT/'prompt_injection.csv',index=False)
    noise=read('context_noise_raw.csv');rows=[]
    if not noise.empty:
        for model,g in noise.groupby('model',sort=False):
            ids=g.case_id.unique();clean=primary_df[(primary_df.model==model)&primary_df.case_id.isin(ids)].copy();clean['condition']='clean'
            combined=pd.concat([clean,g],ignore_index=True)
            for condition,h in combined.groupby('condition',sort=False):
                rows.append({'model':model,'condition':str(condition),'context_characters_mean':h.context.str.len().mean(),
                             'estimated_context_tokens_mean':h.context.str.len().mean()/4,**summarize(h),
                             'latency_mean_s':ok(h).latency_s.mean(),'latency_median_s':ok(h).latency_s.median(),
                             'latency_all_final_attempts_mean_s':h.latency_s.mean(),
                             'latency_including_retries_mean_s':h.total_attempt_latency_s.mean() if 'total_attempt_latency_s' in h else np.nan,
                             'input_tokens_mean':ok(h).input_tokens.mean(),
                             'truncation_status':'Observed System One errors explicitly reject oversized inputs without truncation; generative truncation not independently verified'})
    pd.DataFrame(rows).to_csv(OUT/'context_noise.csv',index=False)

def failures(df):
    good=ok(df);cases=[];candidates=[]
    for case_id,g in good.groupby('case_id',sort=True):
        if len(g)<6:continue
        correct={r.model:all(getattr(r,t+'_pred')==getattr(r,'gold_'+t) for t in TASKS) for r in g.itertuples()}
        dec=[m for m in correct if m.startswith(('tev1','nimble'))];gen=[m for m in correct if m.startswith('qwen')]
        reasons=[]
        if any(correct[m] for m in dec) and any(not correct[m] for m in gen):reasons.append('decision_succeeds_generative_fails')
        if any(correct[m] for m in gen) and any(not correct[m] for m in dec):reasons.append('generative_succeeds_decision_fails')
        if not any(correct.values()):reasons.append('every_judge_fails_at_least_one_dimension')
        dg=g[g.model.isin(dec)]
        if any(dg[t+'_pred'].nunique()>1 for t in TASKS):reasons.append('decision_sizes_disagree')
        high=[]
        for r in dg.itertuples():
            for t in TASKS:
                if getattr(r,t+'_confidence')>=.9 and getattr(r,t+'_pred')!=getattr(r,'gold_'+t):high.append(r.model+':'+t)
        if high:reasons.append('high_confidence_wrong')
        if not reasons:continue
        first=g.iloc[0]
        rec={'case_id':case_id,'source_id':first.source_id,'variant':first.variant,'question':first.question,'context':first.context,
             'context_summary':first.context[:400],'evaluated_answer':first.proposed_answer,
             'reference_labels':json.dumps({t:int(first['gold_'+t]) for t in TASKS}),
             'model_predictions':json.dumps({r.model:{t:int(getattr(r,t+'_pred')) for t in TASKS} for r in g.itertuples()}),
             'reason':' | '.join(reasons),'high_confidence_wrong':json.dumps(high)}
        candidates.append(rec)
    if not candidates:
        pd.DataFrame(columns=['case_id','reason']).to_csv(OUT/'failure_examples.csv',index=False);return
    frame=pd.DataFrame(candidates);frame.to_csv(OUT/'all_failure_candidates.csv',index=False)
    # Round-robin categories, stable ID ordering, includes both model-family directions.
    selected=[];used=set()
    categories=['decision_succeeds_generative_fails','generative_succeeds_decision_fails','every_judge_fails_at_least_one_dimension','decision_sizes_disagree','high_confidence_wrong']
    pools=[frame[frame.reason.str.contains(c)].to_dict('records') for c in categories]
    while len(selected)<25 and any(pools):
        for pool in pools:
            while pool and pool[0]['case_id'] in used:pool.pop(0)
            if pool and len(selected)<25:
                r=pool.pop(0);selected.append(r);used.add(r['case_id'])
    pd.DataFrame(selected).to_csv(OUT/'failure_examples.csv',index=False)

def article_tables():
    metrics=read('all_metrics.csv');perf=read('performance_metrics.csv');variant=read('per_variant_metrics.csv')
    audit=read('construction_audit.csv')
    pd.DataFrame([{'selected_source_questions':len(audit),'distractors_sharing_complete_sentences':int((audit.shared_sentence_count>0).sum()),
                   'semantic_ground_truth_validated':False,'labels_changed_after_observing_predictions':False}]).to_csv(OUT/'construction_audit_summary.csv',index=False)
    article=[]
    for filename,cols in [('all_metrics.csv',['answerability_f1','groundedness_weighted_kappa','relevance_weighted_kappa','missing_evidence_false_positive_rate']),
                          ('performance_metrics.csv',['latency_median_s','latency_p95_s','serial_requests_per_second','output_tokens_mean'])]:
        frame=read(filename)
        for row in frame[frame.model.isin(MODELS)].to_dict('records'):
            for metric in cols:article.append({'model':row['model'],'metric':metric,'value':row.get(metric),'source_file':filename})
    pd.DataFrame(article).to_csv(OUT/'article_numbers.csv',index=False)
    findings=[]
    def add(observation,filename,frame,metric,direction='max'):
        if frame.empty or metric not in frame or frame[metric].dropna().empty:return
        idx=frame[metric].idxmax() if direction=='max' else frame[metric].idxmin();row=frame.loc[idx]
        findings.append({'observation':observation,'model':row.get('model',row.get('decision_model','')),'metric':metric,'value':row[metric],
                         'condition':str(row.get('task',row.get('variant',row.get('threshold','primary')))),'source_file':filename,
                         'interpretation':'Observed point value; not a causal or statistically significant superiority claim.'})
    base=metrics[metrics.model=='always_maximum'];models=metrics[metrics.model.isin(MODELS)]
    add('Always-answerable baseline F1 from class imbalance','all_metrics.csv',base,'answerability_f1')
    add('Highest measured answerability F1 point estimate','all_metrics.csv',models,'answerability_f1')
    add('Lowest measured answerability F1 point estimate','all_metrics.csv',models,'answerability_f1','min')
    add('Largest missing-evidence false-positive rate','all_metrics.csv',models,'missing_evidence_false_positive_rate')
    add('Largest selected-label calibration gap','calibration_metrics.csv',read('calibration_metrics.csv'),'top_label_ece')
    add('Largest fraction changing labels across repeats','stability_metrics.csv',read('stability_metrics.csv'),'label_change_fraction')
    add('Largest attack-caused score-increase rate for any placement/task','prompt_injection.csv',read('prompt_injection.csv'),'attack_success_rate_any_increase')
    add('Largest primary median request latency','performance_metrics.csv',perf,'latency_median_s')
    esc=read('selective_escalation.csv')
    if not esc.empty:add('Largest coverage at the predeclared high-confidence threshold','selective_escalation.csv',esc[esc.threshold==.9],'coverage')
    pd.DataFrame(findings).to_csv(OUT/'key_findings.csv',index=False)

def main(bootstrap_count=2000):
    df=read('raw_predictions.csv')
    if df.empty:raise RuntimeError('No predictions recorded.')
    summary=primary(df,bootstrap_count);calibration(df);performance(df);selective(df);secondary(df);failures(df);article_tables()
    completed_primary_models=[model for model in MODELS if len(df[df.model==model])==300]
    dump(OUT/'analysis_status.json',{'status':'analysis_run_report_pending',
                                     'completed_primary_models':completed_primary_models,
                                     'final_report_generated':(ROOT/'RESULTS.md').exists(),
                                     'note':'Metrics remain preliminary until the complete six-model request matrix and final integrity checks pass.'})
    print(summary[['model','n_valid','answerability_f1','groundedness_weighted_kappa','relevance_weighted_kappa']].to_string(index=False))

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--bootstrap',type=int,default=2000);a=p.parse_args();main(a.bootstrap)
