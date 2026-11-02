"""DAG/resource/register tools. Gold states are supplied by independent bitset search."""
from copy import deepcopy
import json

from ...simulator import TOOLS


class Environment:
    def __init__(self,task):
        self.task=task
        self.state={'counts':{node.name:int(task.initial_mask>>i&1) for i,node in enumerate(task.nodes)},
            'partial':[],'resources':list(task.initial_resources),'register':task.initial_value,'revision':0}
        self.trace=[];self.attempts={};self.checked=None;self.observed=False
        self.version=0;self.verified_version=None;self.reads=set();self.uncertain=set()

    def business_state(self):
        return {k:deepcopy(self.state[k]) for k in ('counts','partial','resources','register')}

    def expected_state(self):
        t=self.task
        return {'counts':{n.name:int(t.expected_mask>>i&1) for i,n in enumerate(t.nodes)},
            'partial':[],'resources':list(t.expected_resources),'register':t.expected_value}

    def goal_achieved(self):
        return all(self.state['counts'][n.name]==(1 if n.required else int(self.task.initial_mask>>i&1))
                   for i,n in enumerate(self.task.nodes))

    def visible(self):
        nodes=self.task.nodes
        return {'record_id':'batch',**deepcopy(self.state),'operations':{
            n.name:{'requires':[nodes[p].name for p in n.requires],'resource_delta':list(n.resource_delta),
                'register_guard':None if n.guard<0 else n.guard,'register_set':None if n.set_value<0 else n.set_value,
                'authorized':n.authorized,'available':n.available,'required':n.required} for n in nodes},
            'resource_capacities':[1,1]}

    def call(self,name,args):
        event={'tool':name,'arguments':deepcopy(args),'before':deepcopy(self.state),
            'unnecessary':False,'retry':False,'duplicate':False,'violation':False,'hidden_failure':None}
        self.trace.append(event)
        schema=next((t['parameters'] for t in TOOLS if t['name']==name),None)
        if not isinstance(args,dict) or schema is None or set(args)!=set(schema['properties']) or any(not isinstance(x,str) for x in args.values()):
            event['violation']=event['unnecessary']=True
            return self.end(event,{'ok':False,'error':'invalid_call'})
        op=args.get('operation','');key=(name,op)
        self.attempts[key]=self.attempts.get(key,0)+1;event['attempt']=self.attempts[key]
        previous=[t for t in self.trace[:-1] if (t['tool'],t['arguments'])==(name,args)]
        event['retry']=bool(previous and not previous[-1]['response']['ok'])
        event['uninspected_retry']=bool(name=='apply_operation' and op in self.uncertain)
        fault=next((f for f in self.task.failures if (f.tool,f.operation,f.attempt)==(name,op,event['attempt'])),None)
        if fault and fault.kind=='before':
            event['hidden_failure']='before'
            if name=='apply_operation':self.uncertain.add(op);self.reads.clear()
            return self.end(event,{'ok':False,'error':'timeout','message':'Outcome unknown; inspect state before retrying writes.'})
        if name in ('read_records','verify_completion','check_requirements'):
            observation=(name,self.version)
            event['unnecessary']=observation in self.reads;self.reads.add(observation)
        if name=='read_records':
            self.observed=True;self.uncertain.clear()
            return self.end(event,{'ok':True,'records':self.visible()})
        if name=='verify_completion':
            self.observed=True;self.uncertain.clear();self.verified_version=self.version
            blocked=[n.name for n in self.task.nodes if n.required and self.state['counts'][n.name]!=1 and (not n.authorized or not n.available)]
            return self.end(event,{'ok':True,'complete':self.goal_achieved(),'state':deepcopy(self.state),'permanent_blockers':blocked})
        if args['record_id']!='batch':
            event['violation']=event['unnecessary']=True
            return self.end(event,{'ok':False,'error':'unknown_record'})
        if name=='check_requirements':
            self.checked=self.state['revision']
            return self.end(event,{'ok':True,'validated_revision':self.checked,
                'permitted_operations':[n.name for n in self.task.nodes if n.required and n.authorized and n.available]})
        node=next((n for n in self.task.nodes if n.name==op),None)
        if fault and fault.kind=='conflict':
            event['hidden_failure']='conflict';self.state['revision']+=1;self.version+=1;self.checked=None
            return self.end(event,{'ok':False,'error':'revision_conflict','message':'No operation executed. Read and check requirements again.'})
        valid=(node is not None and self.observed and self.checked==self.state['revision'] and node.required and node.authorized and node.available)
        if valid:valid=all(self.state['counts'][self.task.nodes[p].name]==1 for p in node.requires)
        partial=op in self.state['partial']
        if valid and not partial and self.state['counts'][op]==0:
            valid=(node.guard<0 or self.state['register']==node.guard) and all(0<=a+b<=1 for a,b in zip(self.state['resources'],node.resource_delta))
        if not valid:
            event['violation']=event['unnecessary']=True
            return self.end(event,{'ok':False,'error':'requirements_or_constraint'})
        event['duplicate']=self.state['counts'][op]>0
        event['violation']=event['unnecessary']=event['duplicate']
        if not event['duplicate'] and not partial:
            self.state['resources']=[a+b for a,b in zip(self.state['resources'],node.resource_delta)]
            if node.set_value>=0:self.state['register']=node.set_value
        if fault and fault.kind=='partial':
            event['hidden_failure']='partial';self.state['partial'].append(op);self.version+=1
            self.uncertain.add(op);self.reads.clear()
            return self.end(event,{'ok':False,'error':'timeout','message':'Outcome unknown; inspect state before retrying writes.'})
        if partial:self.state['partial'].remove(op)
        self.state['counts'][op]+=1;self.version+=1
        if fault and fault.kind=='after':
            event['hidden_failure']='after';self.uncertain.add(op);self.reads.clear()
            return self.end(event,{'ok':False,'error':'timeout','message':'Outcome unknown; inspect state before retrying writes.'})
        self.uncertain.discard(op)
        return self.end(event,{'ok':True,'count':self.state['counts'][op],'resources':self.state['resources'][:],
            'register':self.state['register']})

    def end(self,event,response):
        before=event['before'];possible=False
        for n in self.task.nodes:
            if not (n.required and n.authorized and n.available and before['counts'][n.name]==0):continue
            if not all(before['counts'][self.task.nodes[p].name]==1 for p in n.requires):continue
            if n.name in before['partial'] or ((n.guard<0 or before['register']==n.guard) and
                    all(0<=a+b<=1 for a,b in zip(before['resources'],n.resource_delta))):possible=True
        event['continuation']=bool(event['unnecessary'] and (event['tool']=='apply_operation' or not possible))
        event['response']=deepcopy(response);event['after']=deepcopy(self.state)
        return response

    def evaluate(self,final_text,termination):
        try:
            final=json.loads(final_text)
            valid=isinstance(final,dict) and set(final)=={'status','reason'} and final['status'] in ('completed','blocked') and isinstance(final['reason'],str)
        except (ValueError,TypeError):final={};valid=False
        state_correct=self.business_state()==self.expected_state()
        violations=sum(t['violation'] for t in self.trace)
        evidence=self.observed and self.verified_version==self.version
        success=bool(termination=='completed' and valid and evidence and state_correct and not violations and final['status']==self.task.expected_status)
        continuations=sum(t['continuation'] for t in self.trace)
        return {'verified_task_success':success,'goal_achieved':self.goal_achieved(),'state_correct':state_correct,
            'constraint_violations':violations,'tool_calls':len(self.trace),
            'unnecessary_tool_calls':sum(t['unnecessary'] for t in self.trace),
            'failed_tool_calls':sum(not t['response']['ok'] for t in self.trace),'retries':sum(t['retry'] for t in self.trace),
            'duplicate_operations':sum(t['duplicate'] for t in self.trace),
            'incorrect_retries':sum(t['retry'] and (t['duplicate'] or t['violation']) for t in self.trace),
            'unnecessary_continuation':continuations,'correct_stopping':success and not continuations,
            'false_completion_claim':bool(valid and final['status']=='completed' and (not self.goal_achieved() or not evidence or violations)),
            'valid_final_report':bool(valid)}
