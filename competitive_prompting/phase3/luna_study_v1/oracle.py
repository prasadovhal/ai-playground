"""Observation-driven offline oracle checked against independent state-space gold."""
import json
from .core import reference
from .environment import Environment


def oracle(case):
    t=case.task
    gold,_,status=reference(t.nodes,(t.initial_mask,t.initial_resources,t.initial_value))
    assert gold==(t.expected_mask,t.expected_resources,t.expected_value) and status==t.expected_status
    env=Environment(t);actions=[]
    def call(name,args=None):
        actions.append({'tool':name,'arguments':args or {}})
        return env.call(name,args or {})
    def read():
        for _ in range(8):
            r=call('read_records')
            if r['ok']:return r['records']
        raise ValueError('Oracle read recovery exhausted')
    visible=read()
    checked=False
    for _ in range(40):
        # Choose only from visible state and rules, never consult hidden faults.
        pending=[n for n,meta in visible['operations'].items() if meta['required'] and meta['authorized'] and meta['available'] and visible['counts'][n]==0]
        candidates=[]
        for op in pending:
            n=visible['operations'][op]
            if not all(visible['counts'][p]==1 for p in n['requires']):continue
            if op not in visible['partial']:
                if n['register_guard'] is not None and visible['register']!=n['register_guard']:continue
                if not all(0<=a+b<=1 for a,b in zip(visible['resources'],n['resource_delta'])):continue
            candidates.append(op)
        if not candidates:break
        if not checked:
            r=call('check_requirements',{'record_id':'batch'})
            if not r['ok']:continue
            checked=True
        op=candidates[0]
        r=call('apply_operation',{'record_id':'batch','operation':op})
        if r['ok']:
            visible['counts'][op]=r['count'];visible['resources']=r['resources'];visible['register']=r['register']
            if op in visible['partial']:visible['partial'].remove(op)
        else:
            visible=read()
            if r.get('error')=='revision_conflict':checked=False
    else:raise ValueError('Oracle exhausted workflow limit')
    for _ in range(8):
        result=call('verify_completion')
        if result['ok']:break
    else:raise ValueError('Oracle verification exhausted')
    final=json.dumps({'status':'completed' if result['complete'] else 'blocked','reason':'Offline state verified.'})
    score=env.evaluate(final,'completed')
    if not score['verified_task_success']:raise ValueError('Oracle failed independent program gold: '+t.id)
    return {'task_id':t.id,'actions':actions,'trace':env.trace,'final_state':env.state,'final_text':final,'evaluation':score}


class OracleResponses:
    def __init__(self,case):self.script=oracle(case);self.index=0
    def create(self,**request):
        i=self.index;self.index+=1
        if i<len(self.script['actions']):
            a=self.script['actions'][i]
            output=[{'type':'function_call','call_id':f'offline_{i}','name':a['tool'],'arguments':json.dumps(a['arguments'])}]
        else:output=[{'type':'message','role':'assistant','content':[{'type':'output_text','text':self.script['final_text']}]}]
        return {'id':f'offline_{i}','status':'completed','usage':None,'output':output}
