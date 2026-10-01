import json, unittest, tempfile, uuid, shutil
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch, Mock
import numpy as np
import pandas as pd
from study_judges import make_payload, parse, probabilities, request
from run_study import plan, job_key
from analyze_study import baselines, summarize, ece, kappa
from setup_study import OUT
import analyze_study as analysis
import report_study as reporting

@contextmanager
def fixture_directory():
    parent=Path(__file__).resolve().parent
    folder=parent/('.UNIT_TEST_ONLY_'+uuid.uuid4().hex)
    folder.mkdir()
    try:yield folder
    finally:
        resolved=folder.resolve()
        if resolved.parent!=parent or not resolved.name.startswith('.UNIT_TEST_ONLY_'):raise RuntimeError('Unsafe test cleanup path')
        shutil.rmtree(resolved)

class StudyTests(unittest.TestCase):
    def test_baseline_and_oracle_metrics(self):
        data=pd.read_csv(OUT/'evaluation_dataset_300.csv')
        b=baselines(data)
        high=summarize(b[b.model=='always_maximum'])
        self.assertAlmostEqual(high['answerability_f1'],8/9)
        self.assertAlmostEqual(high['groundedness_mae'],1.4)
        self.assertAlmostEqual(high['relevance_weighted_kappa'],0)
        oracle=summarize(b[b.model=='oracle_sanity_check'])
        for t in ['answerable','groundedness','relevance']:self.assertEqual(oracle[t+'_exact'],1)
        self.assertEqual(oracle['answerability_brier'],0)

    def test_failures_in_denominator(self):
        data=pd.read_csv(OUT/'evaluation_dataset_300.csv');b=baselines(data);g=b[b.model=='oracle_sanity_check'].copy()
        g['error']=g['error'].astype(object);g.loc[g.index[0],'error']='timeout';d=summarize(g)
        self.assertEqual(d['n_failed'],1);self.assertAlmostEqual(d['answerable_agreement_all_attempted'],299/300)
        self.assertEqual(d['answerable_exact'],1)

    def test_ece_bin_boundaries_and_low_confidence(self):
        self.assertAlmostEqual(ece([1,1,1,1],[0,.5,.6,1]),.475)
        self.assertAlmostEqual(ece([0,1],[0,1]),0)
        self.assertAlmostEqual(ece([1],[.25]),.75)

    def test_decision_map_not_rounded_expectation(self):
        dist={'0':.4,'1':.3,'2':.2,'3':.1}
        raw={'answers':{'answerable':{'noul':.2},'groundedness':{'score':1.,'confidence':.01,'probabilities':dist},
                        'relevance':{'score':1.,'confidence':.01,'probabilities':dist}},'usage':{}}
        x=parse(raw,'decision')
        self.assertEqual(x['groundedness_pred'],0);self.assertEqual(x['groundedness_confidence'],.4)
        self.assertEqual(x['groundedness_api_confidence'],.01);self.assertEqual(x['answerable_confidence'],.8)

    def test_malformed_predictions_rejected(self):
        with self.assertRaises(ValueError):parse({'message':{'content':json.dumps({'answerable':True,'groundedness':3,'relevance':3})}},'generative')
        with self.assertRaises(ValueError):probabilities({'0':.5,'1':.5,'2':.5,'3':.5})

    def test_parse_failure_preserves_raw_and_reported_tokens(self):
        raw={'message':{'content':'not valid JSON'},'prompt_eval_count':20,'eval_count':5}
        response=Mock(status_code=200,text=json.dumps(raw));response.json.return_value=raw
        with patch('study_judges.requests.post',return_value=response):
            result=request({'question':'test question','context':'test context','proposed_answer':'test answer'},'qwen3.5:0.8b')
        self.assertIsNotNone(result['error']);self.assertEqual(result['input_tokens'],20);self.assertEqual(result['output_tokens'],5)
        self.assertEqual(result['raw_response'],raw)
        self.assertIsNone(result['load_duration_s'])

    def test_plan_pairing_and_evidence(self):
        data=pd.read_csv(OUT/'evaluation_dataset_300.csv');tasks,ids=plan(data);again,ids2=plan(data)
        self.assertEqual(tasks,again);self.assertEqual(ids,ids2)
        self.assertEqual(len(tasks),680)
        self.assertEqual(len(set(job_key(t,'tev1:4b') for t in tasks)),680)
        repeats=[t for t in tasks if t['experiment']=='stability']
        self.assertEqual(len(repeats),150)
        counts=pd.Series([t['case']['variant'] for t in repeats]).value_counts()
        self.assertTrue((counts==30).all())
        lookup=data.set_index('case_id')
        for t in tasks:
            c=t['case'];original=lookup.loc[c['case_id']]
            if t['experiment']=='context_noise':self.assertIn(original.context,c['context'])
            if t['experiment'] not in ['prompt_injection','context_noise']:self.assertEqual(c['context'],original.context)
            for label in ['answerable','groundedness','relevance']:self.assertEqual(c[label],original[label])

    def test_no_labels_or_variant_leak_into_request(self):
        c=pd.read_csv(OUT/'evaluation_dataset_300.csv').iloc[0].to_dict()
        for model in ['tev1:4b','qwen3.5:4b']:
            endpoint,payload=make_payload(c,model)
            s=json.dumps(payload)
            self.assertNotIn(c['case_id'],s);self.assertNotIn('gold_answer',s);self.assertNotIn('source_id',s)
            self.assertNotIn(c['variant'],s)

    def test_secondary_metrics_on_isolated_synthetic_fixture(self):
        # Test-only inputs. Never written to the actual experiment results.
        data=pd.read_csv(OUT/'evaluation_dataset_300.csv').head(5)
        allrows=[]
        for model in ['tev1:4b','qwen3.5:4b','nimble:9b','qwen3.5:9b','tev1:0.8b','qwen3.5:0.8b']:
            g=baselines(data);g=g[g.model=='oracle_sanity_check'].copy();g['model']=model
            g['judge_type']='generative' if model.startswith('qwen') else 'decision'
            g['latency_s']=1.;g['input_tokens']=100;g['output_tokens']=5
            g['error']=pd.Series([None]*len(g),index=g.index,dtype=object)
            for task in ['answerable','groundedness','relevance']:
                g[task+'_confidence']=.9
                if task!='answerable':
                    g[task+'_score']=g[task+'_pred'].astype(float)
                    g[task+'_probabilities']=[json.dumps({str(i):(.85 if i==p else .05) for i in range(4)}) for p in g[task+'_pred']]
            allrows.append(g)
        primary=pd.concat(allrows,ignore_index=True)
        with fixture_directory() as folder,patch.object(analysis,'OUT',Path(folder)):
            self.assertEqual(Path(folder).resolve().parent,Path(__file__).resolve().parent)
            target=Path(folder)
            repeats=[]
            for i in range(3):g=primary.copy();g['repeat']=i;repeats.append(g)
            pd.concat(repeats).to_csv(target/'stability_raw.csv',index=False)
            prompts=[]
            for c in ['B','C']:g=primary.copy();g['condition']=c;prompts.append(g)
            pd.concat(prompts).to_csv(target/'prompt_sensitivity_raw.csv',index=False)
            attack=primary.copy();attack['condition']='context_beginning';attack['groundedness_pred']=3
            attack.to_csv(target/'prompt_injection_raw.csv',index=False)
            noise=primary.copy();noise['condition']='250';noise.to_csv(target/'context_noise_raw.csv',index=False)
            analysis.secondary(primary);analysis.selective(primary);analysis.calibration(primary)
            stability=pd.read_csv(target/'stability_metrics.csv');self.assertTrue(stability.label_change_fraction.eq(0).all())
            sensitivity=pd.read_csv(target/'prompt_sensitivity.csv');self.assertTrue(sensitivity.agreement_with_A.eq(1).all())
            attackm=pd.read_csv(target/'prompt_injection.csv');self.assertTrue(attackm[attackm.task=='groundedness'].attack_success_rate_any_increase.eq(1).all())
            escalation=pd.read_csv(target/'selective_escalation.csv');self.assertTrue(escalation.all_three_agreement.eq(1).all())
            row=escalation[escalation.threshold==.95].iloc[0];self.assertEqual(row.coverage,0);self.assertEqual(row.estimated_mean_latency_s,2)
            bins=pd.read_csv(target/'reliability_bins.csv');self.assertEqual(bins.n.sum(),len(primary)*3)

    def test_report_cells_and_incomplete_matrix_guard(self):
        with fixture_directory() as folder,patch.object(analysis,'OUT',folder),patch.object(reporting,'OUT',folder),patch.object(reporting,'ROOT',folder):
            pd.DataFrame([{'model':'unit-test','value':.123456789,'missing':np.nan}]).to_csv(folder/'table.csv',index=False)
            report=reporting.Report();report.table('table.csv')
            text='\n\n'.join(report.parts)
            self.assertIn('| model | value | missing |\n| --- | --- | --- |\n| unit-test | 0.123457 | NA |',text)
            (folder/'RESULTS.md').write_text(text,encoding='utf-8')
            (folder/'report_provenance.json').write_text(json.dumps(report.provenance),encoding='utf-8')
            self.assertTrue(reporting.verify()['passed'])
            self.assertTrue(reporting.is_subset({'a':[{'x':1}]},{'a':[{'x':1,'y':2},{'x':3}],'b':4}))
            self.assertFalse(reporting.is_subset({'a':9},{'a':1}))
            (folder/'protocol.json').write_text(json.dumps({'planned_counts_per_model':{'primary':300}}),encoding='utf-8')
            self.assertFalse(reporting.completion())

if __name__=='__main__':unittest.main()
