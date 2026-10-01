"""Cross-file integrity checks, run on the real recorded request matrix."""
import hashlib, json
import numpy as np
import pandas as pd
from setup_study import OUT, MODELS, dump
from report_study import completion, verify
from analyze_study import read, summarize

def main():
    checks={};checks['complete_request_matrix']=completion()
    data=read('evaluation_dataset_300.csv').set_index('case_id')
    predictions=read('raw_predictions.csv')
    checks['unique_primary_keys']=not predictions.duplicated(['model','case_id']).any()
    checks['all_requested_models']=set(predictions.model)==set(MODELS)
    checks['all_models_have_all_cases']=all(set(g.case_id)==set(data.index) for _,g in predictions.groupby('model'))
    reference_match=True;text_match=True
    for row in predictions.itertuples():
        c=data.loc[row.case_id]
        reference_match &= all(getattr(row,'gold_'+t)==c[t] for t in ['answerable','groundedness','relevance'])
        text_match &= all(getattr(row,t)==c[t] for t in ['question','context','proposed_answer'])
    checks['reference_labels_unchanged']=bool(reference_match);checks['primary_inputs_match_dataset']=bool(text_match)
    summary=read('all_metrics.csv').set_index('model');metrics_match=True
    for model,g in predictions.groupby('model'):
        actual=summarize(g)
        for key,value in actual.items():
            if isinstance(value,(float,int,np.number)) and pd.notna(value):
                metrics_match &= bool(np.isclose(float(summary.loc[model,key]),float(value),rtol=1e-10,atol=1e-10))
    checks['summary_recomputed_from_predictions']=bool(metrics_match)
    bins=read('reliability_bins.csv');cal=read('calibration_metrics.csv');bins_match=True
    for row in cal.itertuples():
        bins_match &= bins[(bins.model==row.model)&(bins.task==row.task)].n.sum()==row.n
    checks['calibration_bins_include_every_available_probability']=bool(bins_match)
    checks['report_source_cells_verified']=verify()['passed']
    checks={k:bool(v) for k,v in checks.items()}
    result={'passed':all(checks.values()),'checks':checks}
    dump(OUT/'final_integrity_check.json',result)
    if result['passed']:dump(OUT/'analysis_status.json',{'status':'complete','final_report_generated':True,'all_integrity_checks_passed':True})
    print(json.dumps(result,indent=2))
    if not result['passed']:raise RuntimeError('Final integrity checks failed')

if __name__=='__main__':main()
