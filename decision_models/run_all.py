"""Unattended local orchestration, resumable inference, final verified handoff."""
import argparse, json, os, subprocess, sys, time
from pathlib import Path
import psutil
from setup_study import ROOT, OUT, MODELS, dump

def run_command(args):
    print('Running:', ' '.join(args),flush=True)
    result=subprocess.run(args,cwd=ROOT)
    if result.returncode:raise RuntimeError('Command failed with '+str(result.returncode)+': '+' '.join(args))

def state(stage,**kw):
    dump(OUT/'orchestration_status.json',{'stage':stage,'pid':os.getpid(),'timestamp':time.time(),**kw})
    print(stage,kw,flush=True)
    (ROOT/'RESULTS_STATUS.md').write_text('# Current experiment status\n\nStage: `'+stage+'`.\n\nSee `results/orchestration_status.json` and `results/progress.json` for live progress.\n\nThe original supplied status is preserved in `ORIGINAL_RESULTS_STATUS.md`. A final handoff is generated only after the full request matrix has recorded outcomes.\n',encoding='utf-8')

def main(wait_pids):
    # Exclusive orchestration lock. The OS releases it when the process exits.
    handle=(OUT/'orchestration.lock').open('a+b')
    handle.seek(0);handle.write(b'0');handle.flush();handle.seek(0)
    if os.name=='nt':
        import msvcrt
        msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
    else:
        import fcntl
        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
    if os.name=='nt':
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
    state('waiting_for_existing_authorized_jobs',wait_pids=wait_pids)
    watched=[]
    for pid in wait_pids:
        try:watched.append(psutil.Process(pid))
        except psutil.NoSuchProcess:pass
    while any(p.is_running() and p.status()!=psutil.STATUS_ZOMBIE for p in watched):time.sleep(5)
    py=sys.executable
    try:
        state('checking_models')
        run_command([py,'setup_study.py','--pull'])
        run_command([py,'setup_study.py','--environment'])
        run_command([py,'-m','unittest','test_study','-v'])
        state('running_experiments')
        failed=[]
        for model in MODELS:
            result=subprocess.run([py,'run_study.py','--models',model,'--retry-failed'],cwd=ROOT)
            if result.returncode:failed.append(model)
        for model in failed:
            print('Retrying interrupted model after other models:',model,flush=True)
            run_command([py,'run_study.py','--models',model,'--retry-failed'])
        state('capturing_final_environment')
        run_command([py,'setup_study.py','--environment'])
        state('analyzing')
        run_command([py,'analyze_study.py'])
        state('plotting')
        run_command([py,'plot_study.py'])
        state('writing_and_verifying_report')
        run_command([py,'report_study.py'])
        run_command([py,'check_results.py'])
        checker=Path('C:/Users/prasad_ovhal/.codex/skills/humanwriter/scripts/draft_checker.py')
        if checker.exists():
            # Check generated prose only. Quoted source science text and numeric tables must remain exact.
            checked=subprocess.run([py,str(checker),'--file',str(OUT/'report_narrative.txt')],capture_output=True,text=True,encoding='utf-8',errors='replace')
            (OUT/'writing_check.txt').write_text(checked.stdout+'\n'+checked.stderr,encoding='utf-8')
            if checked.returncode:raise RuntimeError('Report generated and number-checked; writing checker needs review. See results/writing_check.txt')
        state('complete',report=str(ROOT/'RESULTS.md'))
    except Exception as e:
        state('needs_inspection',error=repr(e))
        raise
    finally:
        if os.name=='nt':ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--wait-pids',type=int,nargs='*',default=[]);a=p.parse_args();main(a.wait_pids)
