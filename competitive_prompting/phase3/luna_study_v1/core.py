"""Immutable program specifications and independent finite-state reference search."""
from dataclasses import dataclass
from collections import deque


@dataclass(frozen=True)
class Node:
    name: str
    requires: tuple[int,...]
    resource_delta: tuple[int,...] = (0,0)
    guard: int = -1
    set_value: int = -1
    authorized: bool = True
    available: bool = True
    required: bool = True


@dataclass(frozen=True)
class Fault:
    tool: str
    operation: str = ''
    attempt: int = 1
    kind: str = 'before'


@dataclass(frozen=True)
class Task:
    id: str
    category: str
    instruction: str
    nodes: tuple[Node,...]
    initial_mask: int
    initial_resources: tuple[int,int]
    initial_value: int
    failures: tuple[Fault,...]
    expected_mask: int
    expected_resources: tuple[int,int]
    expected_value: int
    expected_status: str


@dataclass(frozen=True)
class Case:
    task: Task
    family: str
    graph_hash: str
    mechanism: str


def successors(nodes,state):
    """Reference bitset transition semantics, independent of simulated tool code."""
    mask,resources,value=state
    for i,node in enumerate(nodes):
        if mask>>i&1 or not node.authorized or not node.available or not node.required:continue
        if any(not mask>>p&1 for p in node.requires):continue
        if node.guard>=0 and value!=node.guard:continue
        next_resources=tuple(a+b for a,b in zip(resources,node.resource_delta))
        if any(v<0 or v>1 for v in next_resources):continue
        yield i,(mask|1<<i,next_resources,node.set_value if node.set_value>=0 else value)


def reference(nodes,initial):
    """Enumerate all legal orderings; unique reachable terminal business state required.

    No tool environment, failure schedule, prompt or model result is consulted.
    """
    queue=deque([initial]);paths={initial:[]};terminal={}
    while queue:
        state=queue.popleft();next_steps=list(successors(nodes,state))
        if not next_steps:terminal[state]=paths[state]
        for i,next_state in next_steps:
            if next_state not in paths:
                paths[next_state]=paths[state]+[i];queue.append(next_state)
    if len(terminal)!=1:raise ValueError('Ambiguous/inconsistent terminal specification')
    state,path=next(iter(terminal.items()))
    complete=all(not node.required or bool(state[0]>>i&1) for i,node in enumerate(nodes))
    return state,path,'completed' if complete else 'blocked'
