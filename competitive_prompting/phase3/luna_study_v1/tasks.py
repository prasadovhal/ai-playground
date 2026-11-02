"""500 distinct DAG programs, grouped into 100 structural-profile families.

No reused unlabeled dependency topology, including node renamings/permutations.
Families are dependence assumptions, not proven independent generators. The same
interpreter is shared; population-level generalization remains limited.
"""
from collections import Counter,defaultdict
from dataclasses import replace
from functools import lru_cache
import hashlib
from itertools import permutations,product
import json
import random

from .core import Node,Task,Case,Fault,reference

SEED=20261030
VERSION='luna-dag-v1'


def graph_profile(edges):
    n=len(edges);out=[sum(i in parents for parents in edges) for i in range(n)]
    depths=[]
    for parents in edges:depths.append(0 if not parents else 1+max(depths[p] for p in parents))
    return (len([x for x in edges if not x]),out.count(0),max(depths),sum(map(len,edges)),
        tuple(sorted(map(len,edges))),tuple(sorted(out)))


def canonical_graph(edges):
    """Exact graph-isomorphism key via invariant color classes + permutations."""
    n=len(edges);children=[tuple(j for j,p in enumerate(edges) if i in p) for i in range(n)]
    colors=[(len(edges[i]),len(children[i])) for i in range(n)]
    def rank(values):
        lookup={x:i for i,x in enumerate(sorted(set(values)))}
        return [lookup[x] for x in values]
    colors=rank(colors)
    for _ in range(n):
        new=rank([(colors[i],tuple(sorted(colors[p] for p in edges[i])),tuple(sorted(colors[c] for c in children[i]))) for i in range(n)])
        if len(set(new))==len(set(colors)):colors=new;break
        colors=new
    groups=[[i for i in range(n) if colors[i]==c] for c in sorted(set(colors))]
    best=None
    for pieces in product(*(permutations(g) for g in groups)):
        order=sum((tuple(p) for p in pieces),())
        bits=''.join('1' if order[a] in edges[order[b]] else '0' for a in range(n) for b in range(n))
        if best is None or bits<best:best=bits
    return hashlib.sha256(best.encode()).hexdigest()


def graph_catalog():
    rng=random.Random(SEED);groups=defaultdict(list);seen=set()
    for _ in range(200000):
        probability=rng.uniform(.22,.65)
        edges=tuple(tuple(j for j in range(i) if rng.random()<probability) for i in range(7))
        # Connected dependency programs with genuine fork/join decisions.
        profile=graph_profile(edges)
        if profile[0]<2 or profile[1]<1 or not any(len(e)>1 for e in edges):continue
        reached={0}
        while True:
            expanded=reached|{j for i in reached for j in edges[i]}|{i for i,p in enumerate(edges) if any(j in reached for j in p)}
            if expanded==reached:break
            reached=expanded
        if len(reached)!=7:continue
        key=canonical_graph(edges)
        if key in seen:continue
        seen.add(key)
        if len(groups[profile])<5:groups[profile].append((edges,key))
        ready=[p for p,g in groups.items() if len(g)==5]
        if len(ready)>=100:
            return [(p,groups[p]) for p in sorted(ready)[:100]]
    raise ValueError('Unable to obtain 100 complete novel structural families')


INSTRUCTION='''Execute this constraint program through the tools. The record ID is batch.
Operations follow explicit dependency lists, NOT their display order. Complete all
permitted reachable required operations, including when other required operations
are permanently blocked. Preserve every completed operation and excluded operation.
Resource levels have capacity one: producers can require a consumer to run before
another producer. Register guards/effects are also prerequisites. Check requirements
before writes. After an ambiguous timeout inspect the state: count=1 means committed;
partial=true/count=0 means effects already applied and the same operation must be
resumed once to finish (without reapplying effects). After a revision conflict,
re-read and revalidate before writing. Do not repeat completed operations or invent
authorization. Verify the business goal, finish all allowed progress, and report
completed or blocked truthfully. Permanent blockers list only unfinished goals.
'''


@lru_cache(maxsize=1)
def cases():
    result=[];rng=random.Random(SEED+1)
    for family,(profile,graphs) in enumerate(graph_catalog()):
        category=('execution','recovery','stopping')[family%3]
        for variant,(edges,key) in enumerate(graphs):
            # Operation identities are task-neutral; topology, guards and effects
            # supply the problem. They are not renamed business templates.
            nodes=[Node(f'op{i+1}',p) for i,p in enumerate(edges)]
            mode=(family+variant)%4
            if mode in (1,3):
                # Resource ordering must be inferred as well as graph ordering.
                a,b=sorted(rng.sample(range(7),2))
                nodes[a]=replace(nodes[a],resource_delta=(1,0))
                nodes[b]=replace(nodes[b],resource_delta=(-1,0))
            if mode in (2,3):
                a,b=sorted(rng.sample(range(7),2))
                nodes[a]=replace(nodes[a],guard=0,set_value=1)
                nodes[b]=replace(nodes[b],guard=1,set_value=0)
            # Remove the topological-order cue from operation IDs/display order.
            order=list(range(7));rng.shuffle(order)
            inverse={old:new for new,old in enumerate(order)}
            nodes=[replace(nodes[old],name=f'op{new+1}',requires=tuple(inverse[p] for p in nodes[old].requires))
                   for new,old in enumerate(order)]
            # Find a valid complete witness before choosing pre-existing work.
            state,path,status=reference(tuple(nodes),(0,(0,0),0))
            if status!='completed':raise ValueError('Generated program cannot finish')
            prefix=variant%3
            initial=(0,(0,0),0)
            for i in path[:prefix]:
                node=nodes[i]
                initial=(initial[0]|1<<i,tuple(a+b for a,b in zip(initial[1],node.resource_delta)),
                    node.set_value if node.set_value>=0 else initial[2])
            if category=='stopping':
                if variant==0:
                    initial=state
                    nodes[0]=replace(nodes[0],authorized=False)
                else:
                    pending=[i for i in range(7) if not initial[0]>>i&1]
                    blocked=pending[(family+variant)%len(pending)]
                    nodes[blocked]=replace(nodes[blocked],authorized=variant%2==0,available=variant%2==1)
            elif variant==4:
                # A completed restricted operation must not be repeated.
                if initial[0]:
                    i=next(i for i in range(7) if initial[0]>>i&1)
                    nodes[i]=replace(nodes[i],authorized=False)
                # Excluded leaf work, only when other goals remain feasible.
                for i in reversed(range(7)):
                    if initial[0]>>i&1 or any(i in n.requires for n in nodes):continue
                    trial=list(nodes);trial[i]=replace(trial[i],required=False)
                    try:
                        _,_,trial_status=reference(tuple(trial),initial)
                    except ValueError:continue
                    if trial_status=='completed':nodes=trial;break
            final,witness,status=reference(tuple(nodes),initial)
            failures=[]
            if category=='recovery':
                first=nodes[witness[0]].name
                kind=('before','after','partial','conflict','after')[variant]
                failures.append(Fault('apply_operation',first,1,kind))
                if variant==0:failures.append(Fault('apply_operation',first,2,'before'))
                if variant==1:failures.append(Fault('read_records',attempt=2))
                if variant==2:failures.append(Fault('verify_completion'))
                if variant==3:failures.append(Fault('check_requirements',attempt=2))
                if variant==4:failures.append(Fault('read_records'))
            task=Task(f'luna1_{category}_f{family+1:03d}_v{variant+1}',category,INSTRUCTION,
                tuple(nodes),initial[0],initial[1],initial[2],tuple(failures),
                final[0],final[1],final[2],status)
            result.append(Case(task,f'structural-profile-{family+1:03d}',key,
                ('dependency_DAG','capacity_ordering','register_guards','combined_constraints')[mode]))
    assert len(result)==500 and len({c.graph_hash for c in result})==500
    return tuple(result)


def by_id():return {c.task.id:c for c in cases()}
