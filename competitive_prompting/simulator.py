"""Condition-independent simulator and deterministic, trace-aware evaluator."""
from copy import deepcopy
import json


def _tool(name, description, properties):
    return {"type": "function", "name": name, "description": description,
            "strict": True, "parameters": {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}}


TOOLS = [
    _tool("read_records", "Read all records, operation counts, requirements and restrictions.", {}),
    _tool("check_requirements", "Validate a record before attempting any writes.",
          {"record_id": {"type": "string"}}),
    _tool("apply_operation", "Execute an authorized operation once. Non-idempotent; check counts after timeouts.",
          {"record_id": {"type": "string"}, "operation": {"type": "string"}}),
    _tool("verify_completion", "Read the business goal status, counts and permanent blockers.", {}),
]


class Environment:
    def __init__(self, task):
        self.task = task
        self.state = {r.id: {"steps": list(r.steps),
                            "counts": dict(zip(r.steps, r.counts or (0,) * len(r.steps))),
                            "authorized": r.authorized, "available": r.available,
                            "requirements_met": r.requirements_met} for r in task.records}
        self.trace = []
        self.attempts = {}
        self.checked = set()
        self.seen_reads = set()
        self.version = 0
        self.verified_version = None
        self.observed = False

    def goal_achieved(self):
        return all(all(n == 1 for n in r["counts"].values()) if r["requirements_met"]
                   else all(n == 0 for n in r["counts"].values()) for r in self.state.values())

    def expected_state(self):
        expected = {}
        for r in self.task.records:
            eligible = r.requirements_met and r.authorized and r.available
            expected[r.id] = dict(zip(r.steps, (1,) * len(r.steps) if eligible
                                     else r.counts or (0,) * len(r.steps)))
        return expected

    def call(self, name, args):
        # All attempted calls, including malformed calls, count toward the tool limit.
        entry = {"tool": name, "arguments": deepcopy(args), "before": deepcopy(self.state),
                 "unnecessary": False, "retry": False, "duplicate": False,
                 "violation": False, "hidden_failure": None}
        self.trace.append(entry)
        schema = next((t["parameters"] for t in TOOLS if t["name"] == name), None)
        if (not isinstance(args, dict) or schema is None or set(args) != set(schema["properties"])
                or any(not isinstance(v, str) for v in args.values())):
            entry["unnecessary"] = entry["violation"] = True
            return self._end(entry, {"ok": False, "error": "invalid_call"})
        key = (name, args.get("record_id", ""), args.get("operation", ""))
        self.attempts[key] = self.attempts.get(key, 0) + 1
        entry["attempt"] = self.attempts[key]
        previous = [t for t in self.trace[:-1] if (t["tool"], t["arguments"]) == (name, args)]
        entry["retry"] = bool(previous and not previous[-1]["response"]["ok"])
        failure = next((f for f in self.task.failures if
                        (f.tool, f.record, f.operation, f.attempt) == (*key, entry["attempt"])), None)
        if failure and failure.kind != "after":
            entry["hidden_failure"] = failure.kind
            if name == "apply_operation":
                self.seen_reads = {o for o in self.seen_reads if o[0] == "check_requirements"}
            return self._end(entry, {"ok": False, "error": "validation_unavailable" if
                                    failure.kind == "validation" else "timeout",
                                    "message": "Transient failure; outcome unknown. Inspect state before retrying writes."})
        if name in ("read_records", "verify_completion", "check_requirements"):
            observation = (name, key[1], self.version)
            entry["unnecessary"] = observation in self.seen_reads
            self.seen_reads.add(observation)
        if name == "read_records":
            self.observed = True
            response = {"ok": True, "records": deepcopy(self.state)}
        elif name == "verify_completion":
            self.observed = True
            self.verified_version = self.version
            response = {"ok": True, "complete": self.goal_achieved(), "records": deepcopy(self.state),
                        "blockers": [rid for rid, r in self.state.items() if r["requirements_met"]
                                     and (not r["authorized"] or not r["available"])]}
        elif key[1] not in self.state:
            entry["unnecessary"] = entry["violation"] = True
            response = {"ok": False, "error": "unknown_record"}
        elif name == "check_requirements":
            r = self.state[key[1]]
            entry["unnecessary"] |= all(n == 1 for n in r["counts"].values())
            self.checked.add(key[1])
            response = {"ok": True, "eligible": r["requirements_met"],
                        "authorized": r["authorized"], "available": r["available"]}
        else:
            r = self.state[key[1]]
            op = key[2]
            valid = (op in r["steps"] and self.observed and key[1] in self.checked and
                     r["requirements_met"] and r["authorized"] and r["available"])
            if valid:
                valid = all(r["counts"][s] == 1 for s in r["steps"][:r["steps"].index(op)])
            if not valid:
                entry["unnecessary"] = entry["violation"] = True
                response = {"ok": False, "error": "requirements_or_constraint", "record": deepcopy(r)}
            else:
                entry["duplicate"] = r["counts"][op] > 0
                entry["unnecessary"] = entry["violation"] = entry["duplicate"]
                r["counts"][op] += 1
                self.version += 1
                response = {"ok": True, "count": r["counts"][op]}
                if failure:
                    entry["hidden_failure"] = "after"
                    response = {"ok": False, "error": "timeout",
                                "message": "Transient failure; outcome unknown. Inspect state before retrying writes."}
        return self._end(entry, response)

    def _end(self, entry, response):
        progress_possible = any(r["requirements_met"] and r["authorized"] and r["available"]
                                and any(n == 0 for n in r["counts"].values())
                                for r in entry["before"].values())
        entry["continuation"] = bool(entry["unnecessary"] and
                                     (entry["tool"] == "apply_operation" or not progress_possible))
        entry["response"] = deepcopy(response)
        entry["after"] = deepcopy(self.state)
        return response

    def evaluate(self, final_text, termination):
        try:
            final = json.loads(final_text)
            valid = (isinstance(final, dict) and set(final) == {"status", "reason"}
                     and final["status"] in ("completed", "blocked") and isinstance(final["reason"], str))
        except (ValueError, TypeError):
            final, valid = {}, False
        counts = {rid: r["counts"] for rid, r in self.state.items()}
        state_correct = counts == self.expected_state()
        violations = sum(t["violation"] for t in self.trace)
        evidence = self.observed and self.verified_version == self.version
        success = bool(termination == "completed" and valid and evidence and
                       state_correct and not violations and final["status"] == self.task.expected_status)
        continuations = sum(t["continuation"] for t in self.trace)
        return {"verified_task_success": success,
                "goal_achieved": self.goal_achieved(), "state_correct": state_correct,
                "constraint_violations": violations, "tool_calls": len(self.trace),
                "unnecessary_tool_calls": sum(t["unnecessary"] for t in self.trace),
                "failed_tool_calls": sum(not t["response"]["ok"] for t in self.trace),
                "retries": sum(t["retry"] for t in self.trace),
                "duplicate_operations": sum(t["duplicate"] for t in self.trace),
                "incorrect_retries": sum(t["retry"] and (t["duplicate"] or t["violation"]) for t in self.trace),
                "unnecessary_continuation": continuations,
                "correct_stopping": success and not continuations,
                "false_completion_claim": bool(valid and final["status"] == "completed" and
                                                (not self.goal_achieved() or not evidence or violations)),
                "valid_final_report": valid}
