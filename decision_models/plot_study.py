"""Standalone publication figures from machine-readable summaries."""
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from analyze_study import read
from setup_study import OUT, MODELS

PLOTS=OUT/'plots'
COLORS=['#005f73','#ca6702','#0a9396','#ee9b00','#55a3a5','#9b4d00']
NAMES={'tev1:0.8b':'Tev1 0.8B','qwen3.5:0.8b':'Qwen3.5 0.8B','tev1:4b':'Tev1 4B','qwen3.5:4b':'Qwen3.5 4B','nimble:9b':'Nimble 9B','qwen3.5:9b':'Qwen3.5 9B'}

def save(fig,name):
    fig.tight_layout()
    fig.savefig(PLOTS/(name+'.png'),dpi=300,bbox_inches='tight')
    fig.savefig(PLOTS/(name+'.svg'),bbox_inches='tight')
    plt.close(fig)

def main():
    PLOTS.mkdir(exist_ok=True)
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,'axes.spines.top':False,'axes.spines.right':False,'axes.titleweight':'bold','svg.fonttype':'none'})
    summary=read('all_metrics.csv').set_index('model').reindex(MODELS)
    for metric,title,filename,bounds in [
        ('answerability_f1','Answerability F1','answerability_f1',(0,1)),
        ('groundedness_weighted_kappa','Groundedness: linear weighted kappa','groundedness_kappa',(-1,1)),
        ('relevance_weighted_kappa','Relevance: linear weighted kappa','relevance_kappa',(-1,1))]:
        fig,ax=plt.subplots(figsize=(9,4.8));x=np.arange(6);values=summary[metric].to_numpy(float)
        lo=summary[metric+'_ci_low'].to_numpy(float);hi=summary[metric+'_ci_high'].to_numpy(float)
        ax.bar(x,values,color=COLORS,width=.65)
        # Percentile CIs can miss a point estimate; draw endpoints directly.
        ax.vlines(x,lo,hi,color='black',lw=1.2);ax.hlines(lo,x-.07,x+.07,color='black');ax.hlines(hi,x-.07,x+.07,color='black')
        ax.set_xticks(x,[NAMES[m] for m in MODELS],rotation=20,ha='right');ax.set_ylim(*bounds);ax.set_title(title)
        ax.set_ylabel('Agreement with constructed reference labels');ax.axhline(0,color='black',lw=.6);ax.grid(axis='y',alpha=.18)
        ax.text(.01,-.30,'Error bars: 95% source-question cluster bootstrap intervals.',transform=ax.transAxes,fontsize=9)
        save(fig,filename)
    perf=read('performance_metrics.csv').set_index('model').reindex(MODELS)
    fig,ax=plt.subplots(figsize=(9,4.8));x=np.arange(6)
    ax.bar(x-.18,perf.latency_median_s,width=.36,label='Median',color='#007f89');ax.bar(x+.18,perf.latency_p95_s,width=.36,label='p95',color='#db8d25')
    ax.set_xticks(x,[NAMES[m] for m in MODELS],rotation=20,ha='right');ax.set_ylim(bottom=0);ax.set_ylabel('Seconds per three-dimension request');ax.set_title('Local request latency after warm-up');ax.legend();ax.grid(axis='y',alpha=.18);save(fig,'latency')
    reliability=read('reliability_bins.csv')
    if len(reliability):
        fig,axes=plt.subplots(1,3,figsize=(13,4.4),sharex=True,sharey=True)
        for ax,task in zip(axes,['answerable','groundedness','relevance']):
            ax.plot([0,1],[0,1],'--',color='gray',lw=1,label='Perfect calibration')
            for m,col in zip(MODELS,COLORS):
                r=reliability[(reliability.model==m)&(reliability.task==task)&(reliability.n>0)]
                if len(r):ax.plot(r.mean_confidence,r.accuracy,'o-',label=NAMES[m],color=col)
            ax.set(xlim=(0,1),ylim=(0,1),xlabel='Selected-label probability',title=task.title());ax.grid(alpha=.18)
        axes[0].set_ylabel('Empirical label accuracy');axes[-1].legend(fontsize=8,loc='lower left');save(fig,'reliability')
    escalation=read('selective_escalation.csv')
    if len(escalation):
        fig,axes=plt.subplots(1,2,figsize=(10,4.5))
        for m,g in escalation.groupby('decision_model',sort=False):
            axes[0].plot(g.coverage,g.all_three_agreement,'o-',label=NAMES[m])
            axes[1].plot(g.coverage,g.estimated_mean_latency_s,'o-',label=NAMES[m])
        axes[0].set(xlim=(0,1),ylim=(0,1),xlabel='Fraction handled by decision judge',ylabel='All-three-label agreement',title='Selective escalation: coverage and quality')
        axes[1].set(xlim=(0,1),xlabel='Fraction handled by decision judge',ylabel='Estimated seconds per case',title='Offline latency estimate (no model swaps)');axes[1].set_ylim(bottom=0)
        for ax in axes:ax.legend();ax.grid(alpha=.18)
        save(fig,'selective_escalation')
    variant=read('per_variant_metrics.csv');variant['mean_dimension_agreement']=variant[['answerable_exact','groundedness_exact','relevance_exact']].mean(axis=1)
    order=['supported_correct','unsupported_distractor','partially_supported','grounded_irrelevant','missing_evidence']
    pivot=variant.pivot(index='model',columns='variant',values='mean_dimension_agreement').reindex(index=MODELS,columns=order)
    fig,ax=plt.subplots(figsize=(10,4.8));im=ax.imshow(pivot,vmin=0,vmax=1,cmap='cividis',aspect='auto')
    ax.set_yticks(range(6),[NAMES[m] for m in MODELS]);ax.set_xticks(range(5),[v.replace('_','\n') for v in order]);ax.set_title('Mean label agreement across three evaluation dimensions')
    for i in range(6):
        for j in range(5):
            value=pivot.iloc[i,j]
            if pd.notna(value):ax.text(j,i,f'{value:.2f}',ha='center',va='center',color='white' if value<.55 else 'black')
    fig.colorbar(im,ax=ax,label='Mean agreement');save(fig,'per_variant_agreement')
    attack=read('prompt_injection.csv')
    if len(attack):
        fig,axes=plt.subplots(1,3,figsize=(13,4.5),sharey=True)
        for ax,task in zip(axes,['answerable','groundedness','relevance']):
            vals=[]
            for m in MODELS:
                g=attack[(attack.model==m)&(attack.task==task)]
                vals.append(np.average(g.attack_success_rate_any_increase.fillna(0),weights=g.n_eligible) if g.n_eligible.sum() else np.nan)
            ax.bar(range(6),vals,color=COLORS);ax.set_xticks(range(6),[NAMES[m] for m in MODELS],rotation=60,ha='right',fontsize=8)
            ax.set_ylim(0,1);ax.set_title(task.title());ax.grid(axis='y',alpha=.18)
        axes[0].set_ylabel('Attack-caused score increase / eligible paired cases');fig.suptitle('Prompt-injection success, pooled across four placements');save(fig,'prompt_injection')
    noise=read('context_noise.csv')
    if len(noise):
        noise=noise[noise.condition.astype(str)!='clean'].copy();noise['target']=noise.condition.astype(float)
        fig,axes=plt.subplots(1,3,figsize=(13,4.4),sharex=True,sharey=True)
        for ax,task in zip(axes,['answerable','groundedness','relevance']):
            for m,col in zip(MODELS,COLORS):
                g=noise[noise.model==m].sort_values('target')
                if len(g):ax.plot(g.target,g[task+'_agreement_all_attempted'],'o-',color=col,label=NAMES[m])
            ax.set(ylim=(0,1),xlabel='Target context size (characters / 4)',title=task.title());ax.set_xticks([250,500,1000,1500]);ax.grid(alpha=.18)
        axes[0].set_ylabel('Agreement across all attempted cases');axes[-1].legend(fontsize=8,loc='lower left');save(fig,'context_noise')

if __name__=='__main__':main()
