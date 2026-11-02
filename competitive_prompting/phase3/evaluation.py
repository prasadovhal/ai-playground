"""Independent reference checks and an observation-driven offline oracle.

Neither the scorer nor oracle calls Environment.evaluate/expected_state. Gold
counts are explicit task data, not labels generated from successful model runs.
"""
from copy import deepcopy
import json
from ..simulator import Environment, TOOLS


def initial_state(case):
    return {r.id: {"steps": list(r.steps), "counts": dict(zip(r.steps, r.counts)),
                   "authorized": r.authorized, "available": r.available,
                   "requirements_met": r.requirements_met} for r in case.task.records}


def evaluate(case, trace, final_state, final_text, termination):
    state = initial_state(case)
    attempts, checked = {}, set()
    observed, version, verified = False, 0, None
    violations = duplicates = faults_exposed = uninspected_retries = 0
    uncertain_writes = set()
    schemas = {t["name"]: set(t["parameters"]["properties"]) for t in TOOLS}
    for event in trace:
        if event["before"] != state:
            raise ValueError("Simulator before-state differs from independent replay")
        name, args = event["tool"], event["arguments"]
        violation = duplicate = False
        hidden_failure = None
        valid_args = (name in schemas and isinstance(args, dict) and set(args) == schemas[name]
                      and all(isinstance(v, str) for v in args.values()))
        ok = False
        if not valid_args:
            violation = True
        else:
            rid, op = args.get("record_id", ""), args.get("operation", "")
            key = (name, rid, op)
            if name == "apply_operation" and (rid, op) in uncertain_writes:
                uninspected_retries += 1
            attempts[key] = attempts.get(key, 0) + 1
            fault = next((f for f in case.task.failures if
                          (f.tool, f.record, f.operation, f.attempt) == (*key, attempts[key])), None)
            if fault and fault.kind != "after":
                hidden_failure = fault.kind
            elif name == "read_records":
                observed, ok = True, True
                uncertain_writes.clear()
            elif name == "verify_completion":
                observed, verified, ok = True, version, True
                uncertain_writes.clear()
            elif rid not in state:
                violation = True
            elif name == "check_requirements":
                checked.add(rid)
                ok = True
            else:
                r = state[rid]
                permitted = (observed and rid in checked and op in r["steps"] and
                             r["authorized"] and r["available"] and r["requirements_met"])
                if permitted:
                    permitted = all(r["counts"][prior] == 1 for prior in r["steps"][:r["steps"].index(op)])
                if not permitted:
                    violation = True
                else:
                    duplicate = r["counts"][op] > 0
                    violation = duplicate
                    r["counts"][op] += 1
                    version += 1
                    ok = True
                    if fault:
                        hidden_failure, ok = "after", False
            if name == "apply_operation":
                if ok:
                    uncertain_writes.discard((rid, op))
                elif hidden_failure is not None:
                    uncertain_writes.add((rid, op))
        violations += violation
        duplicates += duplicate
        faults_exposed += hidden_failure is not None
        if (event["after"] != state or event["response"]["ok"] != ok or
                event["hidden_failure"] != hidden_failure or event["violation"] != violation or
                event["duplicate"] != duplicate):
            raise ValueError("Simulator transition/fault/violation differs from independent replay")
    if final_state != state:
        raise ValueError("Final state differs from independently replayed actions")
    expected = initial_state(case)
    for rid, counts in case.expected:
        expected[rid]["counts"] = dict(zip(expected[rid]["steps"], counts))
    try:
        report = json.loads(final_text)
        valid = (isinstance(report, dict) and set(report) == {"status", "reason"}
                 and report["status"] in ("completed", "blocked") and isinstance(report["reason"], str))
    except (ValueError, TypeError):
        report, valid = {}, False
    success = bool(termination == "completed" and valid and report["status"] == case.task.expected_status
                   and observed and verified == version and not violations and final_state == expected)
    return {"verified_task_success": success, "constraint_violations": violations,
            "duplicate_operations": duplicates, "state_matches_gold": final_state == expected,
            "faults_exposed": faults_exposed, "valid_final_report": valid,
            "uninspected_write_retries": uninspected_retries}


def oracle(case):
    """Use only public observations to choose actions; no fault schedule or gold lookup."""
    env, actions = Environment(case.task), []

    def call(name, args):
        actions.append({"tool": name, "arguments": deepcopy(args)})
        return env.call(name, args)

    def retry_read(name="read_records", args=None):
        for _ in range(16):
            result = call(name, args or {})
            if result["ok"]:
                return result
        raise ValueError("Oracle exhausted bounded transient recovery")

    records = retry_read()["records"]
    for rid in records:
        r = records[rid]
        if not (r["requirements_met"] and r["authorized"] and r["available"]):
            continue
        if all(n == 1 for n in r["counts"].values()):
            continue
        retry_read("check_requirements", {"record_id": rid})
        for op in r["steps"]:
            for _ in range(16):
                if records[rid]["counts"][op] == 1:
                    break
                result = call("apply_operation", {"record_id": rid, "operation": op})
                if result["ok"]:
                    records[rid]["counts"][op] = result["count"]
                else:
                    records = retry_read()["records"]
            else:
                raise ValueError("Oracle exhausted ambiguous-write recovery")
    verified = retry_read("verify_completion")
    final = json.dumps({"status": "completed" if verified["complete"] else "blocked",
                        "reason": "Verified by offline observation-driven oracle."})
    score = evaluate(case, env.trace, env.state, final, "completed")
    if not score["verified_task_success"]:
        raise ValueError(f"Invalid task/oracle specification: {case.task.id}")
    return {"task_id": case.task.id, "actions": actions, "final_text": final,
            "trace": env.trace, "final_state": env.state, "evaluation": score}


class OracleResponses:
    """Response-shaped oracle script for offline runner checks only."""
    def __init__(self, case):
        self.script = oracle(case)
        self.index = 0

    def create(self, **request):
        i = self.index
        self.index += 1
        if i < len(self.script["actions"]):
            action = self.script["actions"][i]
            output = [{"type": "function_call", "call_id": f"oracle_{i}", "name": action["tool"],
                       "arguments": json.dumps(action["arguments"])}]
        else:
            output = [{"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": self.script["final_text"]}]}]
        return {"id": f"offline_{i}", "status": "completed", "usage": None, "output": output}
