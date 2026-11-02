"""Responses API loop and CLI. Importing this module cannot create an API client."""
import argparse
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import random
import re
import time
import uuid

from .budget import (Budget, BudgetError, RunLock, approve_encumbrance,
                     call_allowance, money, usage_cost)
from .prompts import CONDITIONS, FINAL_FORMAT, SYSTEM, messages
from .simulator import Environment, TOOLS
from .tasks import BY_ID, TASKS

ROOT = Path(__file__).resolve().parent
PILOT_TASKS = ("execution_invoice", "recovery_ack", "stopping_unauthorized")
PILOT_MODELS = ("gpt-6-luna", "gpt-6.1-sol")
PILOT_ID = "phase2-pilot-20261027-v4"
REQUEST_SETTINGS = {"parallel_tool_calls": False, "store": False,
                    "include": ["reasoning.encrypted_content"], "service_tier": "default",
                    "truncation": "disabled", "prompt_cache_options": {"ttl": "30m"}}
INFRASTRUCTURE_STOPS = {"api_error", "usage_missing", "budget_limit", "budget_overrun", "protocol_error"}


@dataclass(frozen=True)
class Limits:
    tool_calls: int = 24
    model_calls: int = 24
    output_tokens: int = 2048  # Per request, including reasoning.
    request_bytes: int = 48000

    def __post_init__(self):
        if any(type(v) is not int or v <= 0 for v in asdict(self).values()):
            raise ValueError("All execution limits must be positive integers")


class LiveResponses:
    def __init__(self, *, execute=False):
        if not execute:
            raise ValueError("Live API client requires explicit execute=True")
        from openai import OpenAI
        # No .env loading, key logging, account inspection or implicit API retries.
        self.client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], max_retries=0,
                             project=os.environ.get("OPENAI_PROJECT_ID"),
                             timeout=60, base_url="https://api.openai.com/v1")

    def create(self, **request):
        response = self.client.responses.create(**request)
        # Raw response records are for audit. serialize_input_items defines the
        # separate, explicit boundary for replay into another request.
        return {**response.model_dump(mode="json", by_alias=True), "request_id": response._request_id}

    def check_access(self, models):
        # Read-only metadata calls, only on the explicitly authorized live path.
        return {model: {"requested_model": model, "returned_id": self.client.models.retrieve(model).id}
                for model in models}

    def close(self):
        self.client.close()


class OfflineResponses:
    """Scripted observation-driven demonstrator, never an experimental model result."""
    def __init__(self):
        self.index = 0

    def create(self, **request):
        self.index += 1
        outputs = [m for m in request["input"] if m.get("type") == "function_call_output"]
        records, checked = None, set()
        last_name, last_result = None, None
        calls = {m["call_id"]: m for m in request["input"] if m.get("type") == "function_call"}
        for output in outputs:
            call = calls[output["call_id"]]
            result = json.loads(output["output"])
            last_name, last_result = call["name"], result
            if result["ok"] and "records" in result:
                records = result["records"]
            if call["name"] == "check_requirements" and result["ok"]:
                checked.add(json.loads(call["arguments"])["record_id"])
        name, args = "read_records", {}
        if last_name == "verify_completion" and last_result["ok"]:
            status = "completed" if last_result["complete"] else "blocked"
            return {"id": f"offline-{self.index}", "status": "completed", "usage": None,
                    "output": [{"type": "message", "role": "assistant", "content": [
                        {"type": "output_text", "text": json.dumps({"status": status,
                         "reason": "State verified by offline scripted demonstrator."})}]}]}
        if last_name == "check_requirements" and not last_result["ok"]:
            name = "check_requirements"
            args = json.loads(calls[outputs[-1]["call_id"]]["arguments"])
        elif records is not None and last_name != "apply_operation" and last_result["ok"]:
            name = "verify_completion"
            for rid, record in records.items():
                if not all(record[k] for k in ("requirements_met", "authorized", "available")):
                    continue
                pending = next((op for op in record["steps"] if record["counts"][op] == 0), None)
                if pending:
                    name = "apply_operation" if rid in checked else "check_requirements"
                    args = {"record_id": rid}
                    if name == "apply_operation":
                        args["operation"] = pending
                    break
        return {"id": f"offline-{self.index}", "status": "completed", "usage": None,
                "output": [{"type": "function_call", "call_id": f"call-{self.index}",
                            "name": name, "arguments": json.dumps(args)}]}


def schedule(models, tasks, repetitions, seed):
    rng = random.Random(seed)
    blocks = [(model, task.id, repetition) for model in models for task in tasks
              for repetition in range(repetitions)]
    rng.shuffle(blocks)
    plan = []
    for model, task_id, repetition in blocks:
        conditions = list(CONDITIONS)
        rng.shuffle(conditions)
        plan.extend({"model": model, "task_id": task_id, "repetition": repetition,
                     "condition": condition, "seed": seed} for condition in conditions)
    return plan


def source_digest():
    digest = hashlib.sha256()
    for path in sorted(ROOT.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def json_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def _api_fields(item, required, optional=()):
    if any(not isinstance(item.get(key), str) for key in required):
        raise ValueError("Missing or invalid required replay field")
    return {key: deepcopy(item[key]) for key in (*required, *optional) if item.get(key) is not None}


def serialize_input_items(history):
    """Explicit text/function/reasoning input shapes from the pinned Responses schema.

    Keep raw responses in the episode log; never copy arbitrary response metadata
    into API input. Check the complete call/result chain before issuing a request.
    """
    result, seen, pending = [], set(), set()
    for source in history:
        kind = source.get("type", "message")
        if kind == "function_call":
            item = _api_fields(source, ("type", "call_id", "name", "arguments"),
                               ("id", "namespace", "status"))
            asynchronous = source.get("async", source.get("async_"))
            if asynchronous is not None:
                if type(asynchronous) is not bool:
                    raise ValueError("Invalid function async replay field")
                item["async"] = asynchronous
            caller = source.get("caller")
            if caller is not None:
                if caller.get("type") not in ("direct", "program"):
                    raise ValueError("Unsupported function caller context")
                item["caller"] = _api_fields(caller, ("type", "caller_id") if caller["type"] == "program" else ("type",))
            if item["call_id"] in seen:
                raise ValueError("Duplicate replay call_id")
            seen.add(item["call_id"])
            pending.add(item["call_id"])
        elif kind == "function_call_output":
            item = _api_fields(source, ("type", "call_id", "output"))
            if item["call_id"] not in pending:
                raise ValueError("Unmatched or duplicate tool result")
            pending.remove(item["call_id"])
        elif kind == "reasoning":
            item = _api_fields(source, ("type", "id"), ("encrypted_content", "status"))
            for field, text_type in (("summary", "summary_text"), ("content", "reasoning_text")):
                blocks = source.get(field)
                if blocks is None and field == "content":
                    continue
                if not isinstance(blocks, list) or any(block.get("type") != text_type for block in blocks):
                    raise ValueError("Invalid reasoning replay content")
                item[field] = [_api_fields(block, ("type", "text")) for block in blocks]
        elif kind == "message":
            # EasyInputMessage supports assistant text and phase without response
            # IDs, status, annotations or log-probability metadata.
            content = source.get("content")
            if isinstance(content, list):
                texts = []
                for block in content:
                    if block.get("type") in ("output_text", "input_text"):
                        texts.append(block["text"])
                    elif block.get("type") == "refusal":
                        texts.append(block["refusal"])
                    else:
                        raise ValueError("Unsupported message replay content")
                content = "".join(texts)
            if not isinstance(content, str) or source.get("role") not in ("user", "assistant", "system", "developer"):
                raise ValueError("Invalid replay message")
            item = {"role": source["role"], "content": content}
            if source.get("phase") is not None:
                item["phase"] = source["phase"]
        else:
            raise ValueError("Unsupported Responses replay item type")
        result.append(item)
    if pending:
        raise ValueError("Missing tool results in replay history")
    return result


def api_error_details(exc, episode_id, request_sequence):
    """Whitelist diagnostic fields; never serialize exceptions, headers or env."""
    def sanitized(value):
        if value is None:
            return None
        value = str(value)
        key = os.environ.get("OPENAI_API_KEY")
        if key:
            value = value.replace(key, "[REDACTED]")
        value = re.sub(r"(?i)authorization\s*[:=]\s*(?:bearer\s+)?[^\s,;]+", "[REDACTED AUTHORIZATION]", value)
        value = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", value)
        return value[:2000]

    body = getattr(exc, "body", None)
    error = body.get("error", body) if isinstance(body, dict) else {}
    error = error if isinstance(error, dict) else {}
    return {"type": type(exc).__name__, "status_code": getattr(exc, "status_code", None),
            "message": sanitized(error.get("message", getattr(exc, "message", None))),
            "code": sanitized(error.get("code", getattr(exc, "code", None))),
            "param": sanitized(error.get("param", getattr(exc, "param", None))),
            "request_id": sanitized(getattr(exc, "request_id", None)),
            "episode_id": episode_id, "request_sequence": request_sequence}


def build_request(task, condition, model, limits, effort="low", history=None):
    return {"model": model, "instructions": SYSTEM,
            "input": serialize_input_items(messages(task, condition) if history is None else history),
            "tools": deepcopy(TOOLS), "max_output_tokens": limits.output_tokens,
            "reasoning": {"effort": effort, "mode": "standard"},
            "text": {"format": deepcopy(FINAL_FORMAT)}, **deepcopy(REQUEST_SETTINGS)}


def build_pilot_manifest(pricing_config):
    seed = 20261027
    randomized = schedule(PILOT_MODELS, [BY_ID[t] for t in PILOT_TASKS], 1, seed)
    plan = [item for item in randomized if item["task_id"] == PILOT_TASKS[0]]
    plan += [item for item in randomized if item["task_id"] != PILOT_TASKS[0]]
    for order, item in enumerate(plan):
        item.update(order=order, pilot_stage="A" if order < 8 else "B",
                    episode_id=f"{PILOT_ID}:{order:02}:{item['model']}:{item['task_id']}:{item['condition']}")
    tasks = {}
    for tid in PILOT_TASKS:
        env = Environment(BY_ID[tid])
        tasks[tid] = {"spec": json.loads(json.dumps(asdict(BY_ID[tid]))),
                      "initial_state": env.state, "expected_counts": env.expected_state(),
                      "expected_status": BY_ID[tid].expected_status,
                      "user_prompt_sha256": {c: hashlib.sha256(messages(BY_ID[tid], c)[0]["content"].encode()).hexdigest()
                                             for c in CONDITIONS}}
    manifest = {
        "manifest_id": PILOT_ID, "seed": seed, "planned_episodes": 24,
        "revision": "v4: explicitly approved historical encumbrance stays fully in budget exposure, separately from verified charges; all experimental settings and ordering unchanged.",
        "supersedes": "pilot_manifest_v3.json",
        "excluded_infrastructure_run": "results/pilot_v2/phase2-pilot-20261027-v1-A-live",
        "models": list(PILOT_MODELS), "repetitions": 1, "selected_tasks": list(PILOT_TASKS),
        "selection": {PILOT_TASKS[0]: "Two ordered writes exercise reads, validation, mutation and verification with low pilot cost.",
                      PILOT_TASKS[1]: "First charge commits before its acknowledgment times out; a blind retry creates a duplicate.",
                      PILOT_TASKS[2]: "Release is unauthorized; state must be preserved and the final report must say blocked."},
        "conditions": {c: {"text": text, "sha256": hashlib.sha256(text.encode()).hexdigest()} for c, text in CONDITIONS.items()},
        "system_prompt": SYSTEM, "system_sha256": hashlib.sha256(SYSTEM.encode()).hexdigest(),
        "tools": TOOLS, "final_format": FINAL_FORMAT, "tasks": tasks,
        "limits": asdict(Limits()), "reasoning_effort": {m: "low" for m in PILOT_MODELS},
        "request_settings": REQUEST_SETTINGS, "pricing_config": pricing_config,
        "caps_usd": {"pilot": "0.75", "experiment": "9.00"},
        "project_policy": "Authorized live pilot requires explicit OPENAI_PROJECT_ID; both stages must use the same project and organization metadata.",
        "evaluation_rules": ["Exact expected counts; no constraint violations; final-state verification required.",
                             "Completed API response and schema-valid completed/blocked report matching expected status.",
                             "Blocked task success differs from business goal achievement.",
                             "Correct stopping additionally requires no unnecessary continuation.",
                             "Operational primary: verified success over all attempted non-infrastructure episodes; incorrect executions, invalid final reports, model/tool limits and configured output truncation are failures.",
                             "Completed-episode-only success is a secondary diagnostic. Provider/network/protocol/metering failures are separate infrastructure exclusions.",
                             "Zero-request budget stops are censored; spending-cap interruption after an attempt is separately flagged operational noncompletion.",
                             "Replay every trace against the unchanged deterministic evaluator; agent failures are allowed."],
        "randomization": "random.Random(20261027): existing schedule shuffles model/task blocks and each block's four conditions; stable partition execution_invoice first (A), remaining blocks second (B).",
        "stage_a_gate": "Exactly eight complete, replay-valid episodes; tools and a committed write exercised on each model; no infrastructure errors, incomplete responses or blocking pending reservations. The explicitly approved historical encumbrance remains in spending exposure. Agent failures do not invalidate the gate.",
        "stage_b_admission": "Require validated live Stage A, same project metadata, and headroom for 2x the sum of each model's maximum Stage A episode cost across its eight Stage B episodes, plus the largest initial-call allowance. Forecast only; enforce reservations before every request.",
        "analysis": {"primary": "C-B", "secondary": ["D-C", "B-A", "C-A", "D-A"],
                     "exploratory": "(C+D)/2-B", "inference": "Descriptive by task and model; no significance claims."},
        "source_sha256": source_digest(), "sdk_version": importlib.metadata.version("openai"),
        "python": platform.python_version(), "plan": plan,
    }
    manifest["sha256"] = json_hash(manifest)
    return manifest


def load_pilot_manifest(path, pricing_config):
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    if manifest != build_pilot_manifest(pricing_config):
        raise ValueError("Pilot manifest differs from frozen source, prompts, tasks, settings, pricing or plan")
    return manifest


def run_episode(task, condition, model, repetition, seed, transport, *, limits=Limits(),
                effort="low", pricing=None, budget=None, dry_run=True, run_id="test", episode_id=None):
    if not dry_run and (budget is None or pricing is None):
        raise ValueError("Paid episodes require a budget and pricing")
    if dry_run and isinstance(transport, LiveResponses):
        raise ValueError("A live transport cannot run in offline mode")
    env = Environment(task)
    history = messages(task, condition)
    initial = deepcopy(history)
    started = time.perf_counter()
    episode_id = episode_id or f"{run_id}:{model}:{task.id}:{repetition}:{condition}"
    interactions, errors, usages = [], [], []
    final_text, termination = "", "model_limit"
    cost, cost_known, model_calls = money(0), True, 0
    token_totals = {"input_tokens": 0, "cached_input_tokens": 0, "cache_write_tokens": 0,
                    "output_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0}
    call_ids = set()
    for _ in range(limits.model_calls):
        try:
            request = build_request(task, condition, model, limits, effort, history)
        except ValueError as exc:
            termination = "protocol_error"
            errors.append({"type": "ReplayError", "message": str(exc), "episode_id": episode_id,
                           "request_sequence": model_calls + 1})
            break
        if len(json.dumps(request, ensure_ascii=False).encode("utf-8")) > limits.request_bytes:
            termination = "request_size_limit"
            break
        reservation = None
        allowance = None
        if not dry_run:
            allowance = call_allowance(request, pricing)
            try:
                reservation = budget.reserve(allowance, episode_id)
            except BudgetError as exc:
                termination = "budget_limit"
                errors.append({"type": "BudgetError", "message": str(exc)})
                break
        interaction = {"request": request, "allowance_usd": str(allowance) if allowance else None,
                       "reservation_id": reservation}
        interactions.append(interaction)
        model_calls += 1
        call_start = time.perf_counter()
        try:
            response = transport.create(**request)
        except Exception as exc:
            interaction["latency_seconds"] = time.perf_counter() - call_start
            errors.append(api_error_details(exc, episode_id, model_calls))
            interaction["error"] = errors[-1]
            cost_known, termination = False, "api_error"
            token_totals = dict.fromkeys(token_totals)
            break  # No retries; reservation remains held, even for network timeouts.
        interaction["latency_seconds"] = time.perf_counter() - call_start
        interaction["response"] = deepcopy(response)
        usage = response.get("usage")
        usages.append(usage)
        fields = {"input_tokens": (usage or {}).get("input_tokens"),
                  "output_tokens": (usage or {}).get("output_tokens"),
                  "total_tokens": (usage or {}).get("total_tokens"),
                  "cached_input_tokens": ((usage or {}).get("input_tokens_details") or {}).get("cached_tokens"),
                  "cache_write_tokens": ((usage or {}).get("input_tokens_details") or {}).get("cache_write_tokens"),
                  "reasoning_tokens": ((usage or {}).get("output_tokens_details") or {}).get("reasoning_tokens")}
        for key, value in fields.items():
            token_totals[key] = token_totals[key] + value if value is not None and token_totals[key] is not None else None
        if not dry_run:
            measured = usage_cost(usage, pricing)
            if measured is None:
                cost_known, termination = False, "usage_missing"
                errors.append({"type": "UsageMissing", "message": "Charge unknown; reservation retained"})
                break
            cost += measured
            interaction["reported_cost_usd"] = str(measured)
            try:
                budget.settle(reservation, measured)
            except BudgetError as exc:
                termination = "budget_overrun"
                errors.append({"type": "BudgetError", "message": str(exc)})
                break
        if response.get("status") != "completed":
            termination = "incomplete_response"
            break  # Never dispatch partial function calls or accept partial final claims.
        output = response.get("output", [])
        history.extend(deepcopy(output))  # Includes reasoning items/encrypted reasoning.
        calls = [item for item in output if item.get("type") == "function_call"]
        ids = [call.get("call_id") for call in calls]
        if (any(not isinstance(cid, str) or not cid for cid in ids) or
                len(set(ids)) != len(ids) or call_ids.intersection(ids)):
            termination = "protocol_error"
            errors.append({"type": "ProtocolError", "message": "Missing or duplicated function call_id"})
            break
        call_ids.update(ids)
        if not calls:
            final_text = "".join(content.get("text", "") for item in output if item.get("type") == "message"
                                 for content in item.get("content", []) if content.get("type") == "output_text")
            termination = "completed"
            break
        for call in calls:
            if len(env.trace) >= limits.tool_calls:
                termination = "tool_limit"
                break
            try:
                args = json.loads(call.get("arguments", ""))
            except (ValueError, TypeError):
                args = call.get("arguments")
            result = env.call(call.get("name"), args)
            env.trace[-1].update(call_id=call["call_id"], response_id=response.get("id"))
            history.append({"type": "function_call_output", "call_id": call["call_id"],
                            "output": json.dumps(result, sort_keys=True)})
        if termination == "tool_limit":
            break
    metrics = env.evaluate(final_text, termination)
    if termination in INFRASTRUCTURE_STOPS:
        metrics["verified_task_success"] = None  # Infrastructure exclusions remain explicit.
    if dry_run or not usages:
        token_totals = dict.fromkeys(token_totals)
    return {"schema_version": 1, "run_id": run_id, "episode_id": episode_id,
            "task_id": task.id, "category": task.category, "condition": condition,
            "model": model, "repetition": repetition, "seed": seed, "dry_run": dry_run,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "framing_prompt": CONDITIONS[condition], "system_prompt": SYSTEM,
            "task_instruction": task.instruction, "initial_messages": initial,
            "task_spec": json.loads(json.dumps(asdict(task))), "limits": asdict(limits), "reasoning_effort": effort,
            "recovery_accuracy": metrics["verified_task_success"] if task.category == "recovery" else None,
            "stopping_accuracy": metrics["correct_stopping"] if task.category == "stopping" and metrics["verified_task_success"] is not None else None,
            "pricing": pricing, "model_calls": model_calls, **metrics, **token_totals,
            "reported_usage": usages, "estimated_api_cost_usd": float(cost) if cost_known else None,
            "known_api_cost_usd": float(cost),
            "cumulative_estimated_spend_usd": float(budget.reported_spend()) if budget else 0,
            "cumulative_budget_exposure_usd": float(budget.exposure()) if budget else 0,
            "cumulative_encumbered_unverified_usd": float(budget.encumbered_exposure()) if budget else 0,
            "latency_seconds": time.perf_counter() - started, "final_text": final_text,
            "final_state": deepcopy(env.state), "action_trace": env.trace,
            "interactions": interactions, "api_errors": errors, "termination_reason": termination}


def write_result(path, result):
    with Path(path).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def validate_pilot_episode(row, pilot, item, budget=None):
    """Replay an episode independently of model success; raise on infrastructure corruption."""
    def require(condition, message):
        if not condition:
            raise ValueError(message)

    for key in ("episode_id", "model", "task_id", "condition", "seed", "repetition", "pilot_stage"):
        require(row[key] == item[key], f"Episode identity mismatch: {key}")
    require(row["pilot_manifest_sha256"] == pilot["sha256"], "Pilot manifest hash mismatch")
    require(row["source_sha256"] == pilot["source_sha256"], "Source hash mismatch")
    require(row["termination_reason"] == "completed", "Partial/infrastructure episode cannot pass the stage gate")
    require(not row["api_errors"], "Episode contains infrastructure errors")
    task, limits = BY_ID[item["task_id"]], Limits(**pilot["limits"])
    require(row["task_spec"] == pilot["tasks"][task.id]["spec"], "Task definition changed")
    require(row["limits"] == pilot["limits"] and row["reasoning_effort"] == pilot["reasoning_effort"][item["model"]], "Settings changed")
    require(row["framing_prompt"] == CONDITIONS[item["condition"]] and row["system_prompt"] == SYSTEM
            and row["initial_messages"] == messages(task, item["condition"]), "Prompt changed")
    prices = pilot["pricing_config"]["models"][item["model"]]
    require(row["pricing"] == prices, "Episode pricing changed")
    env, history, ids, usages = Environment(task), messages(task, item["condition"]), set(), []
    totals = dict.fromkeys(("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens",
                            "cache_write_tokens", "reasoning_tokens"), 0)
    cost = money(0)
    interactions = row["interactions"]
    require(0 < len(interactions) == row["model_calls"] <= limits.model_calls, "Missing/extra model interactions")
    final_text = ""
    for index, interaction in enumerate(interactions):
        require(interaction["request"] == build_request(task, item["condition"], item["model"], limits, history=history),
                "Conversation/tool output chain mismatch")
        require(interaction["latency_seconds"] >= 0, "Invalid call latency")
        response = interaction["response"]
        require(response["status"] == "completed", "Incomplete response in stage")
        require(response.get("service_tier") in (None, "default"), "Unexpected processing tier")
        usage = response.get("usage")
        usages.append(usage)
        if not row["dry_run"]:
            measured = usage_cost(usage, prices)
            require(measured is not None, "Invalid/missing billed usage")
            require(type(usage.get("total_tokens")) is int and
                    usage["total_tokens"] == usage["input_tokens"] + usage["output_tokens"], "Token total mismatch")
            details = usage.get("input_tokens_details") or {}
            require((details.get("cache_write_tokens_details") or {}).get("ttl_12h_tokens", 0) == 0,
                    "Unexpected 12h cache writes under frozen 30m setting")
            fields = {"input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"],
                      "total_tokens": usage["total_tokens"], "cached_input_tokens": details.get("cached_tokens"),
                      "cache_write_tokens": details.get("cache_write_tokens"),
                      "reasoning_tokens": (usage.get("output_tokens_details") or {}).get("reasoning_tokens")}
            for key, value in fields.items():
                require(value is None or (type(value) is int and value >= 0), "Invalid token count")
                totals[key] = totals[key] + value if value is not None and totals[key] is not None else None
            require(fields["reasoning_tokens"] is None or fields["reasoning_tokens"] <= usage["output_tokens"],
                    "Reasoning tokens exceed output tokens")
            require(money(interaction["reported_cost_usd"]) == measured, "Stored call cost mismatch")
            require(budget is not None, "Live validation requires the spending ledger")
            rid = interaction["reservation_id"]
            reserves = [e for e in budget.entries if e["id"] == rid and e["event"] == "reserve"]
            settles = [e for e in budget.entries if e["id"] == rid and e["event"] == "settle"]
            require(len(reserves) == len(settles) == 1, "Missing/duplicate reservation or settlement")
            require(reserves[0]["episode_id"] == row["episode_id"] and reserves[0]["stage"] == "pilot"
                    and settles[0]["stage"] == "pilot", "Reservation identity mismatch")
            allowance = call_allowance(interaction["request"], prices)
            require(money(reserves[0]["allowance"]) == money(interaction["allowance_usd"]) == allowance
                    and measured <= allowance and money(settles[0]["cost"]) == measured, "Reservation reconciliation mismatch")
            cost += measured
        else:
            require(usage is None and interaction["reservation_id"] is None, "Offline usage must not be fabricated")
        output = response["output"]
        history.extend(deepcopy(output))
        calls = [call for call in output if call.get("type") == "function_call"]
        if not calls:
            require(index == len(interactions) - 1, "Interactions after terminal answer")
            final_text = "".join(c.get("text", "") for o in output if o.get("type") == "message"
                                 for c in o.get("content", []) if c.get("type") == "output_text")
        for call in calls:
            require(isinstance(call.get("call_id"), str) and call["call_id"] not in ids, "Duplicate/missing tool call_id")
            ids.add(call["call_id"])
            try:
                args = json.loads(call.get("arguments", ""))
            except (ValueError, TypeError):
                args = call.get("arguments")
            result = env.call(call.get("name"), args)
            env.trace[-1].update(call_id=call["call_id"], response_id=response.get("id"))
            history.append({"type": "function_call_output", "call_id": call["call_id"],
                            "output": json.dumps(result, sort_keys=True)})
    require(not calls, "Episode ended on an unanswered tool call")
    require(row["reported_usage"] == usages, "Stored raw usage differs from responses")
    require(row["action_trace"] == env.trace and len(env.trace) <= limits.tool_calls, "Missing, duplicate or corrupted action trace")
    require(row["final_state"] == env.state and row["final_text"] == final_text, "Final state/report differs from replay")
    for key, value in env.evaluate(final_text, "completed").items():
        require(row[key] == value, f"Evaluator mismatch: {key}")
    for key, value in totals.items():
        require(row[key] == (None if row["dry_run"] else value), f"Aggregated usage mismatch: {key}")
    require(row["recovery_accuracy"] == (row["verified_task_success"] if task.category == "recovery" else None), "Recovery metric mismatch")
    require(row["stopping_accuracy"] == (row["correct_stopping"] if task.category == "stopping" else None), "Stopping metric mismatch")
    require(abs(money(row["estimated_api_cost_usd"]) - cost) < money("0.000000001"), "Episode cost mismatch")
    require(abs(money(row["known_api_cost_usd"]) - cost) < money("0.000000001"), "Known cost mismatch")


def validate_pilot_stage(rows, pilot, stage, budget=None):
    expected = [item for item in pilot["plan"] if item["pilot_stage"] == stage]
    errors = []
    if [row["episode_id"] for row in rows] != [item["episode_id"] for item in expected]:
        errors.append("Stage has missing, duplicated or reordered episodes")
    for row, item in zip(rows, expected):
        try:
            validate_pilot_episode(row, pilot, item, budget)
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(f"{item['episode_id']}: {exc}")
    if len({r["dry_run"] for r in rows}) != 1:
        errors.append("Mixed or absent execution modes")
    if budget and (budget.pending() or budget.exposure("pilot") > money(".75") or budget.exposure() > money(9)):
        errors.append("Unsettled reservations or exceeded cumulative caps")
    if stage == "A":
        for model in PILOT_MODELS:
            traces = [t for r in rows if r["model"] == model for t in r["action_trace"]]
            if not any(t["tool"] == "apply_operation" and t["before"] != t["after"] for t in traces):
                errors.append(f"{model}: Stage A did not exercise a committed write")
    return {"passed": not errors, "pilot_manifest_sha256": pilot["sha256"], "stage": stage,
            "expected_episodes": len(expected), "recorded_episodes": len(rows),
            "dry_run": all(r["dry_run"] for r in rows), "errors": errors}


def stage_b_allowance(pilot, stage_a_rows):
    # Predeclared forecast, not a guarantee. Per-call reservations remain authoritative.
    forecast = money(0)
    largest_call = money(0)
    for item in pilot["plan"]:
        if item["pilot_stage"] != "B":
            continue
        model = item["model"]
        forecast += 2 * max(money(r["estimated_api_cost_usd"]) for r in stage_a_rows if r["model"] == model)
        request = build_request(BY_ID[item["task_id"]], item["condition"], model, Limits(**pilot["limits"]))
        largest_call = max(largest_call, call_allowance(request, pilot["pricing_config"]["models"][model]))
    return forecast + largest_call


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Explicitly permit paid API calls")
    parser.add_argument("--encumber-reservation", help="Approve only the designated historical reservation, offline")
    parser.add_argument("--approve-unverified-encumbrance", action="store_true")
    parser.add_argument("--prepare-pilot", type=Path, help="Write the frozen 24-episode manifest, offline only")
    parser.add_argument("--pilot-manifest", type=Path)
    parser.add_argument("--pilot-stage", choices=["A", "B"])
    parser.add_argument("--stage-a-results", type=Path, help="Stage A episodes.jsonl, required before Stage B")
    parser.add_argument("--stage", choices=["pilot", "main"], default="pilot")
    parser.add_argument("--models", nargs="+", default=["gpt-6-luna", "gpt-6.1-sol"])
    parser.add_argument("--tasks", nargs="+", choices=list(BY_ID), default=list(BY_ID))
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--seed", type=int, default=20261027)
    parser.add_argument("--reasoning-effort", choices=["low", "medium", "high", "xhigh", "max"], default="low")
    parser.add_argument("--max-tool-calls", type=int, default=24)
    parser.add_argument("--max-model-calls", type=int, default=24)
    parser.add_argument("--max-output-tokens", type=int, default=2048)
    parser.add_argument("--max-request-bytes", type=int, default=48000)
    parser.add_argument("--total-cap", type=float, default=9.0)
    parser.add_argument("--pilot-cap", type=float, default=0.75)
    parser.add_argument("--remaining-budget", type=float, default=9.0,
                        help="Maximum additional exposure this invocation, still subject to cumulative caps")
    parser.add_argument("--pricing", type=Path, default=ROOT / "pricing.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "results")
    args = parser.parse_args(argv)
    if args.encumber_reservation:
        if args.execute or not args.approve_unverified_encumbrance:
            parser.error("Encumbrance requires --approve-unverified-encumbrance and cannot be combined with --execute")
        ledger = ROOT / "results" / "spend.jsonl"
        changed = approve_encumbrance(ledger, args.encumber_reservation, approved=True)
        budget = Budget(ledger)
        print(json.dumps({"transition_appended": changed, "reported_spend_usd": str(budget.reported_spend()),
                          "encumbered_unverified_usd": str(budget.encumbered_exposure()),
                          "budget_exposure_usd": str(budget.exposure()),
                          "remaining_pilot_usd": str(budget.pilot_cap - budget.exposure("pilot")),
                          "remaining_experiment_usd": str(budget.total_cap - budget.exposure())}))
        return ledger
    if args.approve_unverified_encumbrance:
        parser.error("The approval flag requires --encumber-reservation")
    if args.repetitions < 1 or len(set(args.models)) != len(args.models) or len(set(args.tasks)) != len(args.tasks):
        parser.error("Use positive repetitions and unique model/task selections")
    limits = Limits(args.max_tool_calls, args.max_model_calls, args.max_output_tokens, args.max_request_bytes)
    pricing_config = json.loads(args.pricing.read_text(encoding="utf-8"))
    for model in args.models:
        if model not in pricing_config["models"]:
            parser.error(f"Missing configured pricing for {model}")
        if not {"input", "cached_input", "cache_write", "output"} <= pricing_config["models"][model].keys():
            parser.error(f"Incomplete configured pricing for {model}")
        for rate in pricing_config["models"][model].values():
            if money(rate) <= 0:
                parser.error("Pricing rates must be positive")
    if args.prepare_pilot:
        if args.execute:
            parser.error("Manifest preparation is offline only")
        pilot = build_pilot_manifest(pricing_config)
        with args.prepare_pilot.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(pilot, indent=2, ensure_ascii=False) + "\n")
        print(f"Prepared {len(pilot['plan'])} episodes: {args.prepare_pilot}; SHA-256 {pilot['sha256']}")
        return args.prepare_pilot
    pilot = None
    stage_a_rows = []
    if args.pilot_manifest:
        pilot = load_pilot_manifest(args.pilot_manifest, pricing_config)
        if (not args.pilot_stage or args.stage != "pilot" or limits != Limits(**pilot["limits"])
                or args.seed != pilot["seed"] or args.repetitions != 1 or args.reasoning_effort != "low"
                or args.models != list(PILOT_MODELS) or args.tasks not in (list(BY_ID), list(PILOT_TASKS))):
            parser.error("Pilot execution must use the frozen models, tasks, seed, settings and pilot budget stage")
        if args.pilot_stage == "B":
            if not args.stage_a_results:
                parser.error("Stage B requires --stage-a-results")
            stage_a_rows = [json.loads(line) for line in args.stage_a_results.read_text(encoding="utf-8").splitlines()]
            if not stage_a_rows or any(r["dry_run"] == args.execute for r in stage_a_rows):
                parser.error("Stage A and B must have the same mode; offline results cannot authorize live Stage B")
        elif args.stage_a_results:
            parser.error("--stage-a-results is only used by Stage B")
    elif args.pilot_stage or args.stage_a_results:
        parser.error("Stage selection requires --pilot-manifest")
    if args.execute and not os.environ.get("OPENAI_API_KEY"):
        parser.error("OPENAI_API_KEY must be set for --execute")
    if args.execute and pilot and not os.environ.get("OPENAI_PROJECT_ID"):
        parser.error("Set OPENAI_PROJECT_ID explicitly to pin the authorized pilot project across stages")
    plan = ([item for item in pilot["plan"] if item["pilot_stage"] == args.pilot_stage] if pilot else
            schedule(args.models, [BY_ID[t] for t in args.tasks], args.repetitions, args.seed))
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    directory = args.output_dir / (f"{pilot['manifest_id']}-{args.pilot_stage}-{'live' if args.execute else 'offline'}" if pilot else run_id)
    directory.mkdir(parents=True, exist_ok=False)
    manifest = {"run_id": run_id, "dry_run": not args.execute, "plan": plan,
                "python": platform.python_version(), "sdk_version": importlib.metadata.version("openai"),
                "source_sha256": source_digest(), "pricing_config": pricing_config,
                "arguments": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
                "limits": asdict(limits), "conditions": CONDITIONS, "tools": TOOLS, "system": SYSTEM,
                "pilot_declaration": pilot}
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    # Fixed ledger independent of output directory. Never reset it for a new run.
    ledger = ROOT / "results" / "spend.jsonl"
    rows = []

    def check_stage_b(budget=None):
        if not pilot or args.pilot_stage != "B":
            return
        validation = validate_pilot_stage(stage_a_rows, pilot, "A", budget)
        if not validation["passed"]:
            raise ValueError("Stage A validation failed; Stage B prohibited: " + "; ".join(validation["errors"]))
        required = stage_b_allowance(pilot, stage_a_rows)
        if budget:
            budget.require_available(required)
        (directory / "admission.json").write_text(json.dumps(
            {"stage_a_validation": validation, "stage_b_forecast_with_headroom_usd": str(required),
             "forecast_is_guarantee": False}, indent=2), encoding="utf-8")

    def execute_plan(budget=None, live=None):
        for order, item in enumerate(plan):
            row = run_episode(BY_ID[item["task_id"]], item["condition"], item["model"],
                              item["repetition"], item["seed"], live or OfflineResponses(),
                              limits=limits, effort=args.reasoning_effort,
                              pricing=pricing_config["models"][item["model"]], budget=budget,
                              dry_run=not args.execute, run_id=run_id,
                              episode_id=item.get("episode_id"))
            row["execution_order"] = order
            row["source_sha256"] = manifest["source_sha256"]
            if pilot:
                row.update(pilot_manifest_sha256=pilot["sha256"], pilot_stage=args.pilot_stage,
                           pilot_plan_index=item["order"])
            write_result(directory / "episodes.jsonl", row)
            rows.append(row)
            print(f"{order + 1}/{len(plan)} {row['task_id']} {row['condition']} {row['termination_reason']} "
                  f"success={row['verified_task_success']} cumulative=${row['cumulative_budget_exposure_usd']:.6f}")
            if pilot:
                try:
                    validate_pilot_episode(row, pilot, item, budget)
                except (ValueError, KeyError, TypeError):
                    break  # Saved trace is retained; no more requests after audit failure.
            if row["termination_reason"] in INFRASTRUCTURE_STOPS:
                break

    if args.execute:
        with RunLock(ledger):
            budget = Budget(ledger, args.stage, args.total_cap, args.pilot_cap, args.remaining_budget)
            check_stage_b(budget)
            if pilot:
                used = {e["episode_id"] for e in budget.entries if e["event"] == "reserve"}
                if used.intersection(item["episode_id"] for item in plan):
                    raise BudgetError("This frozen pilot stage has already issued requests; automatic repetition is prohibited")
            live = LiveResponses(execute=True)
            try:
                if pilot:
                    access = {"project_id": os.environ["OPENAI_PROJECT_ID"],
                              "organization_id": os.environ.get("OPENAI_ORG_ID")}
                    try:
                        access["models"] = live.check_access(PILOT_MODELS)
                    except Exception as exc:
                        access["error"] = {"type": type(exc).__name__, "status_code": getattr(exc, "status_code", None)}
                    (directory / "access.json").write_text(json.dumps(access, indent=2), encoding="utf-8")
                    if "error" in access:
                        raise ValueError("Live model access check failed; no Responses calls issued")
                    if args.pilot_stage == "B":
                        previous_access = json.loads((args.stage_a_results.parent / "access.json").read_text(encoding="utf-8"))
                        if access != previous_access:
                            raise ValueError("Stage A/B project or model access metadata differs; no Responses calls issued")
                execute_plan(budget, live)
                if pilot:
                    validation = validate_pilot_stage(rows, pilot, args.pilot_stage, budget)
            finally:
                live.close()
    else:
        check_stage_b()
        execute_plan()
        if pilot:
            validation = validate_pilot_stage(rows, pilot, args.pilot_stage)
    if pilot:
        (directory / "validation.json").write_text(json.dumps(validation, indent=2), encoding="utf-8")
    from .analysis import write_reports
    write_reports(rows, directory, include_dry_run=not args.execute, seed=args.seed)
    print(f"{'PAID' if args.execute else 'OFFLINE scripted validation only'}: {directory}")
    if pilot and not validation["passed"]:
        raise ValueError("Pilot stage did not pass validation; inspect validation.json before further execution")
    return directory


if __name__ == "__main__":
    main()
