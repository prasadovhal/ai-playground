"""Archive supplied files, capture environment, and validate controlled data."""
from pathlib import Path
import argparse, hashlib, importlib.metadata, json, platform, shutil, subprocess, sys, time
import numpy as np
import pandas as pd
import psutil
import requests
import prepare_dataset as prep

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'results'
MODELS = ['tev1:0.8b', 'qwen3.5:0.8b', 'tev1:4b', 'qwen3.5:4b', 'nimble:9b', 'qwen3.5:9b']
BASE = 'http://localhost:11434'

def dump(path, obj):
    Path(path).write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str), encoding='utf-8')

def command(args):
    r = subprocess.run(args, capture_output=True, text=True, errors='replace')
    return {'returncode': r.returncode, 'stdout': r.stdout, 'stderr': r.stderr}

def environment():
    path = OUT / 'environment.json'
    old = json.loads(path.read_text()) if path.exists() else {}
    tags = requests.get(BASE+'/api/tags', timeout=30).json()
    probe = requests.post(BASE+'/v1/systemone', json={}, timeout=30)
    cpu = platform.processor()
    if sys.platform == 'win32':
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'HARDWARE\DESCRIPTION\System\CentralProcessor\0') as k:
            cpu = winreg.QueryValueEx(k, 'ProcessorNameString')[0]
    env = dict(captured_at=time.strftime('%Y-%m-%dT%H:%M:%S%z'), os=platform.platform(),
               cpu=cpu, physical_cores=psutil.cpu_count(logical=False), logical_cores=psutil.cpu_count(),
               ram_bytes=psutil.virtual_memory().total, python=sys.version, python_executable=sys.executable,
               ollama_version=requests.get(BASE+'/api/version', timeout=30).json(),
               ollama_cli_version=command(['ollama','--version']), ollama_list=command(['ollama','list']),
               gpu=command(['nvidia-smi','--query-gpu=name,memory.total,driver_version','--format=csv,noheader']),
               systemone_probe={'status':probe.status_code, 'body':probe.text},
               installed_models=tags, initial_models=old.get('initial_models',tags),
               requested_models=MODELS, packages={p:importlib.metadata.version(p) for p in
                   ['numpy','pandas','requests','scipy','scikit-learn','matplotlib','psutil']})
    env['model_details'] = {}
    dump(OUT/'python_packages.json',sorted([{'name':d.metadata.get('Name','unknown'),'version':d.version} for d in importlib.metadata.distributions()],key=lambda d:d['name'].lower()))
    installed = {m['name'] for m in tags['models']}
    for m in MODELS:
        if m in installed:
            r=requests.post(BASE+'/api/show',json={'model':m},timeout=30)
            env['model_details'][m]=r.json()
    dump(path, env)
    return env

def prepare(source):
    train=pd.read_csv(source)
    assert len(train)==200 and train.id.is_unique and set(train.id)==set(range(200))
    assert train[['prompt','A','B','C','D','E','answer']].notna().all().all()
    original=prep.build_eval_set(train)
    original.to_csv(OUT/'original_evaluation_dataset_300.csv',index=False)
    original_ids=prep.SAMPLED_IDS.copy()
    ids=np.random.RandomState(42).choice(sorted(train.id.tolist()),60,replace=False).tolist()
    prep.SAMPLED_IDS=ids
    data=prep.build_eval_set(train)
    repeated=prep.build_eval_set(train)
    data.to_csv(OUT/'evaluation_dataset_300.csv',index=False)
    data.to_json(OUT/'evaluation_dataset_300.jsonl',orient='records',lines=True,force_ascii=False)
    train.set_index('id').loc[ids].reset_index().to_csv(OUT/'selected_kaggle_records.csv',index=False)
    if Path(source).resolve()!=(OUT/'source_train.csv').resolve():shutil.copy2(source,OUT/'source_train.csv')
    dump(OUT/'selected_kaggle_ids.json',{'seed':42,'algorithm':'numpy.random.RandomState(42).choice(sorted IDs,60,replace=False)',
                                      'ids':ids,'original_fixed_ids':original_ids})
    checks={'exactly_300':len(data)==300,'60_per_variant':data.variant.value_counts().eq(60).all(),
            '60_base_questions':data.source_id.nunique()==60,'unique_case_ids':data.case_id.is_unique,
            'nonmissing_fields':data[['question','context','proposed_answer']].notna().all().all(),
            'nonempty_fields':data[['question','context','proposed_answer']].map(lambda x:bool(str(x).strip())).all().all(),
            'valid_labels':data.answerable.isin([0,1]).all() and data.groundedness.isin(range(4)).all() and data.relevance.isin(range(4)).all(),
            'deterministic_rerun':data.equals(repeated),
            'seed42_sample_repeats':ids==np.random.RandomState(42).choice(sorted(train.id.tolist()),60,replace=False).tolist(),
            'source_options_match':all(r.gold_answer==str(train.set_index('id').loc[r.source_id,r.gold_option]).strip() for r in data.itertuples())}
    checks={k:bool(v) for k,v in checks.items()}
    assert all(checks.values()), checks
    # Diagnostic flags are NOT replacement ground truth or semantic validation.
    audit=[]
    for r in data[data.variant=='unsupported_distractor'].itertuples():
        correct_sentences=set(s.strip().lower() for s in r.gold_answer.split('.') if s.strip())
        wrong_sentences=set(s.strip().lower() for s in r.distractor.split('.') if s.strip())
        overlap=sorted(correct_sentences & wrong_sentences)
        audit.append({'source_id':r.source_id,'question':r.question,'correct_option':r.gold_answer,'wrong_option':r.distractor,
                      'shared_complete_sentences':json.dumps(overlap),'shared_sentence_count':len(overlap),
                      'status':'potential_groundedness_label_ambiguity' if overlap else 'no_exact_sentence_overlap_not_semantic_clearance'})
    pd.DataFrame(audit).to_csv(OUT/'construction_audit.csv',index=False)
    validation={'checks':checks,'structural_validation_passed':True,'semantic_validity_guaranteed':False,
                'source':str(source),'source_sha256':hashlib.sha256(Path(source).read_bytes()).hexdigest(),
                'dataset_sha256':hashlib.sha256((OUT/'evaluation_dataset_300.csv').read_bytes()).hexdigest(),
                'seed':42,'source_rows':len(train),'case_count':len(data),'variant_counts':data.variant.value_counts().to_dict(),
                'label_distributions':{c:data[c].value_counts().sort_index().to_dict() for c in ['answerable','groundedness','relevance']},
                'limitations':['Construction labels retained, not human-verified RAG labels.',
                  'Wrong multiple-choice options can include supported subclaims; groundedness 0 is not guaranteed.',
                  'Concatenated correct and wrong options do not guarantee that MOST claims are supported.',
                  'Offset-17 evidence is not guaranteed semantically unrelated.',
                  'Bare answer fragments and omitted multiple-choice options may make context sufficiency ambiguous.',
                  'Context markers reveal intended relevance, and exact copying creates lexical shortcuts.',
                  'The original fixed ID list had no seed derivation; archived and replaced with explicit seed-42 sampling.',
                  'No level-1 groundedness and no level-1/2 relevance references; cannot validate full ordinal scale.']}
    dump(OUT/'dataset_validation.json',validation)
    print(json.dumps(validation,indent=2),flush=True)

def pull():
    for model in MODELS:
        if model in {x['name'] for x in requests.get(BASE+'/api/tags').json()['models']}: continue
        print('Pulling',model,flush=True)
        t=time.perf_counter()
        with requests.post(BASE+'/api/pull',json={'model':model,'stream':True},stream=True,timeout=(30,600)) as r:
            r.raise_for_status()
            log=OUT/('pull_'+model.replace(':','_')+'.jsonl')
            with log.open('a',encoding='utf-8') as f:
                for line in r.iter_lines():
                    if not line:continue
                    obj=json.loads(line); f.write(json.dumps(obj)+'\n');f.flush()
                    if 'error' in obj:raise RuntimeError(obj)
        print('Downloaded',model,'seconds',round(time.perf_counter()-t,1),flush=True)
    environment()

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source');p.add_argument('--pull',action='store_true');p.add_argument('--environment',action='store_true');a=p.parse_args()
    OUT.mkdir(exist_ok=True)
    if a.source:environment();prepare(a.source)
    if a.pull:pull()
    if a.environment:environment()
