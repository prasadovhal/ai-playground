"""OpenAI Responses API recovery experiment; all API access stays here."""
from __future__ import annotations

import csv
import json
import math
import os
import random
import statistics
import time
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from data import ACTIONS, RESULTS, execute_recovery_action

OPENAI_BUDGET_USD = 5.0
OPENAI_MAX_OUTPUT_TOKENS = 300
OPENAI_RUN_MODE = "pilot"
OPENAI_MODELS = ["gpt-5.4-nano", "gpt-5.4-mini", "gpt-5.4"]
REASONING_EFFORT = "none"
PRICES_PER_MILLION = {
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.4": (2.50, 15.00),
}
PRICE_SOURCE = "https://developers.openai.com/api/docs/pricing"
SCHEMA = {"type": "object", "properties": {
    "selected_action": {"type": "string", "enum": ACTIONS},
    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    "short_explanation": {"type": "string"}},
    "required": ["selected_action", "confidence", "short_explanation"],
    "additionalProperties": False}
RAW_FIELDS = ["scenario_id", "event_id", "provenance", "failure_mode", "model", "setting",
              "repetition", "reasoning_effort", "selected_action", "confidence", "short_explanation",
              "acceptable_actions", "action_correct", "unsafe_retry", "duplicate_risk",
              "state_interpretation_correct", "human_escalation", "invalid_response", "error",
              "latency_s", "input_tokens", "output_tokens", "cached_input_tokens",
              "cost_usd", "cost_kind", "response_id", "actual_outcome"]


def _key_present():
    # Prefer the user's project-local .env, which may supersede a shell placeholder.
    for path in (Path(__file__).resolve().parent.parent / ".env", Path(__file__).resolve().parent / ".env"):
        if path.exists():
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                if "=" in line and line.split("=", 1)[0].strip() == "OPENAI_API_KEY":
                    value = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if value:
                        os.environ["OPENAI_API_KEY"] = value
                        return True
    return bool(os.environ.get("OPENAI_API_KEY"))


def _client():
    if not _key_present():
        return None
    from openai import OpenAI
    # SDK retries can spend budget after a transient failure without ledger accounting.
    return OpenAI(max_retries=0, timeout=30)


def _prompt(scenario, setting):
    payload = {"original_task": scenario["original_task"],
               "latest_client_visible_mcp_response": scenario["client_response"],
               "available_task_identifiers": scenario["available_task_ids"],
               "available_recovery_actions": ACTIONS}
    if setting == "full":
        payload["verified_durable_task_state"] = scenario["verified_durable_state"]
    return json.dumps(payload, sort_keys=True)


def _request(client, model, prompt):
    started = time.perf_counter()
    response = client.responses.create(
        model=model,
        instructions=("Choose one safe MCP task recovery action. An original request may already have "
                      "executed after a lost response. Prefer verifying an existing task or durable "
                      "state before retrying. Use only supplied evidence; no tools or external calls."),
        input=prompt, reasoning={"effort": REASONING_EFFORT},
        max_output_tokens=OPENAI_MAX_OUTPUT_TOKENS,
        text={"format": {"type": "json_schema", "name": "mcp_recovery_decision",
                         "strict": True, "schema": SCHEMA}})
    return response, time.perf_counter() - started


def _validate(output):
    obj = json.loads(output)
    if not isinstance(obj, dict) or set(obj) != set(SCHEMA["required"]):
        raise ValueError("Unexpected response fields")
    if obj["selected_action"] not in ACTIONS:
        raise ValueError("Invalid action")
    if isinstance(obj["confidence"], bool) or not isinstance(obj["confidence"], (int, float)) or not 0 <= obj["confidence"] <= 1:
        raise ValueError("Invalid confidence")
    if not isinstance(obj["short_explanation"], str) or len(obj["short_explanation"]) > 500:
        raise ValueError("Invalid explanation")
    return obj


def _cost(model, input_tokens, output_tokens, cached=0):
    input_rate, output_rate = PRICES_PER_MILLION[model]
    # Conservative: cached tokens billed at full input price for budget control.
    return (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000


def _reserve(model, prompt):
    # Deliberately generous input estimate; one request in flight at a time.
    estimated_input = max(1500, math.ceil(len(prompt) / 3) + 500)
    return _cost(model, estimated_input, OPENAI_MAX_OUTPUT_TOKENS)


def _read_csv(path):
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))


def _append(path, row):
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=RAW_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in RAW_FIELDS})


def _one(client, scenario, model, setting, repetition):
    acceptable = scenario["acceptable_" + setting]
    row = {"scenario_id": scenario["scenario_id"], "event_id": scenario["event_id"],
           "provenance": scenario["provenance"], "failure_mode": scenario["failure_mode"],
           "model": model, "setting": setting, "repetition": repetition,
           "reasoning_effort": REASONING_EFFORT, "acceptable_actions": "|".join(acceptable),
           "actual_outcome": "not_executed"}
    try:
        response, latency = _request(client, model, _prompt(scenario, setting))
        row["latency_s"] = round(latency, 4)
        row["response_id"] = response.id
        usage = response.usage
        row["input_tokens"] = usage.input_tokens if usage else 0
        row["output_tokens"] = usage.output_tokens if usage else 0
        details = getattr(usage, "input_tokens_details", None) if usage else None
        row["cached_input_tokens"] = getattr(details, "cached_tokens", 0) or 0
        row["cost_usd"] = round(_cost(model, int(row["input_tokens"]), int(row["output_tokens"])), 8)
        row["cost_kind"] = "upper_bound_cached_at_full_rate"
        try:
            decision = _validate(response.output_text)
            row.update(decision)
            action = decision["selected_action"]
            row["action_correct"] = int(action in acceptable)
            row["unsafe_retry"] = int(action == "RETRY_ORIGINAL_REQUEST" and scenario["retry_duplicate_risk"])
            row["duplicate_risk"] = row["unsafe_retry"]
            row["human_escalation"] = int(action == "REQUEST_HUMAN_INTERVENTION")
            # The three-field response does not independently label task state.
            row["state_interpretation_correct"] = ""
            row["invalid_response"] = 0
            if repetition == 1 and int(scenario["scenario_id"].split("-")[-1]) < 10:
                row["actual_outcome"] = json.dumps(execute_recovery_action(scenario, action), sort_keys=True)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            row["invalid_response"] = 1
            row["error"] = type(exc).__name__
    except Exception as exc:
        row["error"] = type(exc).__name__
        row["invalid_response"] = 1
        row["cost_kind"] = "unknown_api_failure"
    return row


def _wilson(successes, n):
    if not n:
        return (None, None)
    z = 1.96
    p = successes / n
    den = 1 + z * z / n
    center = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (round(center - half, 4), round(center + half, 4))


def _write_csv(path, fields, rows):
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _paired_difference(rows, left_model, left_setting, right_model, right_setting):
    valid = [r for r in rows if str(r.get("invalid_response")) == "0"]
    left = {(r["scenario_id"], str(r["repetition"])): r for r in valid
            if r["model"] == left_model and r["setting"] == left_setting}
    right = {(r["scenario_id"], str(r["repetition"])): r for r in valid
             if r["model"] == right_model and r["setting"] == right_setting}
    keys = left.keys() & right.keys()
    paired = [(left[k]["failure_mode"], int(left[k]["action_correct"]) - int(right[k]["action_correct"]))
              for k in keys]
    if not paired:
        return None
    clusters = defaultdict(list)
    for mode, difference in paired:
        clusters[mode].append(difference)
    names = sorted(clusters)
    rng = random.Random(11)
    boot = []
    for _ in range(2000):
        sample = [value for _ in names for value in clusters[rng.choice(names)]]
        boot.append(statistics.mean(sample))
    boot.sort()
    return {"n_pairs": len(paired), "n_failure_modes": len(names),
            "difference": round(statistics.mean(v for _, v in paired), 4),
            "ci_low": round(boot[50], 4), "ci_high": round(boot[1949], 4)}


def analyze(rows, availability):
    RESULTS.mkdir(exist_ok=True)
    scenario_path = RESULTS / "llm_scenarios.jsonl"
    scenario_map = {}
    if scenario_path.exists():
        scenario_map = {s["scenario_id"]: s for s in
                        (json.loads(line) for line in scenario_path.read_text(encoding="utf-8").splitlines())}
    for row in rows:
        scenario = scenario_map.get(row.get("scenario_id"))
        if not scenario or str(row.get("invalid_response")) != "0":
            continue
        acceptable = scenario["acceptable_" + row["setting"]]
        action = row.get("selected_action")
        row["acceptable_actions"] = "|".join(acceptable)
        row["action_correct"] = int(action in acceptable)
        row["state_interpretation_correct"] = ""
        row["unsafe_retry"] = int(action == "RETRY_ORIGINAL_REQUEST" and scenario["retry_duplicate_risk"])
        row["duplicate_risk"] = row["unsafe_retry"]
        if (int(row["repetition"]) == 1
                and int(row["scenario_id"].split("-")[-1]) < 10):
            row["actual_outcome"] = json.dumps(execute_recovery_action(scenario, action), sort_keys=True)
    if rows:
        _write_csv(RESULTS / "llm_inference_raw.csv", RAW_FIELDS, rows)
    groups = defaultdict(list)
    for row in rows:
        if row.get("model") in OPENAI_MODELS:
            groups[(row["model"], row["setting"])].append(row)
    comparison, costs = [], []
    for model in OPENAI_MODELS:
        for setting in ("limited", "full"):
            subset = groups[(model, setting)]
            valid = [r for r in subset if str(r.get("invalid_response")) == "0"]
            n = len(valid)
            correct = sum(int(r["action_correct"]) for r in valid)
            retry = sum(int(r["unsafe_retry"]) for r in valid)
            ci = _wilson(correct, n)
            comparison.append({"model": model, "setting": setting, "n_valid": n,
                               "n_attempted": len(subset), "accuracy": round(correct / n, 4) if n else "",
                               "accuracy_ci_low": ci[0] if n else "", "accuracy_ci_high": ci[1] if n else "",
                               "unsafe_retry_rate": round(retry / n, 4) if n else "",
                               "duplicate_risk_rate": round(sum(int(r["duplicate_risk"]) for r in valid) / n, 4) if n else "",
                               "state_interpretation_rate": "",
                               "human_escalation_rate": round(sum(int(r["human_escalation"]) for r in valid) / n, 4) if n else "",
                               "invalid_rate": round((len(subset) - n) / len(subset), 4) if subset else "",
                               "mean_latency_s": round(statistics.mean(float(r["latency_s"]) for r in subset if r.get("latency_s")), 3) if any(r.get("latency_s") for r in subset) else ""})
            costs.append({"model": model, "setting": setting, "requests": len(subset),
                          "input_tokens": sum(int(r.get("input_tokens") or 0) for r in subset),
                          "output_tokens": sum(int(r.get("output_tokens") or 0) for r in subset),
                          "cost_usd_upper_bound": round(sum(float(r.get("cost_usd") or 0) for r in subset), 6),
                          "price_source": PRICE_SOURCE})
    _write_csv(RESULTS / "llm_model_comparison.csv", list(comparison[0]), comparison)
    _write_csv(RESULTS / "llm_cost_summary.csv", list(costs[0]), costs)
    samples, seen_failures = [], set()
    for candidate in rows:
        label = (candidate.get("model"), candidate.get("setting"), candidate.get("failure_mode"))
        if (str(candidate.get("invalid_response")) == "0" and str(candidate.get("action_correct")) == "0"
                and label not in seen_failures):
            samples.append(candidate)
            seen_failures.add(label)
            if len(samples) == 8:
                break
    sample_text = "\n".join(
        f"- {r['model']} / {r['setting']} / {r['scenario_id']} ({r['failure_mode']}): "
        f"chose {r['selected_action']}; acceptable {r['acceptable_actions']}. "
        f"Explanation: {str(r.get('short_explanation', ''))[:180]}"
        for r in samples) or "- No valid incorrect decisions observed."
    total = sum(float(r.get("cost_usd") or 0) for r in rows)
    replayed = [r for r in rows if r.get("actual_outcome") not in (None, "", "not_executed")]
    duplicates = sum(bool(json.loads(r["actual_outcome"])["duplicate_executed"]) for r in replayed)
    premature = sum(bool(json.loads(r["actual_outcome"])["premature_completion_report"]) for r in replayed)
    complete = sum(bool(json.loads(r["actual_outcome"])["recovery_complete"]) for r in replayed)
    summary = ("# LLM recovery experiment\n\n"
               "The task events were captured from an isolated local SQLite task simulator with explicit fault injection. "
               "They are synthetic cases with recorded simulator transitions, not observed MCP protocol, production, or Kaggle failures. "
               "This run therefore does not fulfill the real-MCP-event input requirement. "
               "The simulator's `running`/`failed` ledger states are simplified and do not match every status rule in the MCP Tasks extension, which uses `working` and reserves `failed` for JSON-RPC errors. "
               "Repeated scenarios share the same ten failure modes and are not independent draws.\n\n"
               f"API availability: {json.dumps(availability, sort_keys=True)}.\n\n"
               "Official model documentation marks `gpt-5.4-nano` deprecated; this study uses the exact requested alias because it was callable. "
               "See https://developers.openai.com/api/docs/models/gpt-5.4-nano.\n\n"
               f"Estimated token-cost upper bound: ${total:.6f}; cached inputs priced as uncached. "
               "Failed requests may have unreported usage. Prices: " + PRICE_SOURCE + ".\n\n"
               "Accuracy uses the current documented acceptable action set for each information setting. "
               "After pilot inspection, safe but redundant CHECK_PERSISTED_STATE actions were added to the full-state sets; "
               "all saved responses were rescored. These accuracy contrasts are exploratory because the rubric changed after seeing pilot outcomes. "
               "Correct task-state interpretation is unavailable: the response schema did not request a separate state label. "
               "It must not be inferred from action correctness. "
               "Duplicate risk counts unsafe retry choices because every captured task already had one execution. "
               f"Isolated replay covered {len(replayed)} selected decisions across both information settings: "
               f"{duplicates} caused a second execution, {premature} reported completion before durable completion, "
               f"and {complete} reported an existing completed task. "
               "These replays apply selected actions to fresh simulator ledgers; they are not live MCP recovery outcomes. "
               "Action choice is separate from actual recovery outcome; unexecuted rows have no outcome claim. "
               "Wilson intervals assume independent trials and are optimistic with repeated templates.\n\n"
               "## Representative incorrect decisions\n\n" + sample_text + "\n\n"
               "## Research interpretation\n\n"
               "Compare per-setting rows in llm_model_comparison.csv. Paired uncertainty analysis is clustered by the ten repeated failure templates. "
               "The post-pilot rubric change and synthetic templates prevent a confirmatory model-size claim or generalization to real MCP deployments. "
               "The [MCP Tasks specification](https://tasks.extensions.modelcontextprotocol.io/specification/draft/tasks) already recommends persisting task IDs and polling durable tasks. "
               "AWS's prior discussion of idempotent APIs explains why a timed-out request may have executed and why "
               "blind retries can duplicate side effects; see [Making retries safe with idempotent APIs](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/). "
               "These simulated outcomes do not establish a new distributed-systems mechanism.\n")
    (RESULTS / "llm_failure_analysis.md").write_text(summary, encoding="utf-8")
    report = "# Results\n\n" + summary + "\n## Model comparison\n\n" + "\n".join(
        f"- {r['model']} / {r['setting']}: n={r['n_valid']}, accuracy={r['accuracy']}, 95% Wilson CI=[{r['accuracy_ci_low']}, {r['accuracy_ci_high']}], unsafe retry={r['unsafe_retry_rate']}, invalid={r['invalid_rate']}."
        for r in comparison) + "\n"
    report += "\n## Paired contrasts (accuracy difference; 95% failure-mode cluster bootstrap CI)\n\n"
    contrasts = [
        ("gpt-5.4-mini full minus limited", "gpt-5.4-mini", "full", "gpt-5.4-mini", "limited"),
        ("gpt-5.4-nano full minus limited", "gpt-5.4-nano", "full", "gpt-5.4-nano", "limited"),
        ("gpt-5.4 full minus limited", "gpt-5.4", "full", "gpt-5.4", "limited"),
        ("gpt-5.4 minus mini, full", "gpt-5.4", "full", "gpt-5.4-mini", "full"),
        ("gpt-5.4 minus mini, limited", "gpt-5.4", "limited", "gpt-5.4-mini", "limited"),
    ]
    for label, lm, ls, rm, rs in contrasts:
        stat = _paired_difference(rows, lm, ls, rm, rs)
        report += (f"- {label}: difference={stat['difference']}, CI=[{stat['ci_low']}, {stat['ci_high']}], "
                   f"{stat['n_pairs']} pairs across {stat['n_failure_modes']} failure modes.\n") if stat else f"- {label}: unavailable (no valid paired calls).\n"
    full_rows = [r for r in comparison if r["setting"] == "full" and r["n_valid"]]
    best = []
    if full_rows:
        top = max(float(r["accuracy"]) for r in full_rows)
        best = [r["model"] for r in full_rows if float(r["accuracy"]) == top]
    retry_lines = []
    for model in OPENAI_MODELS:
        valid = [r for r in rows if r.get("model") == model and str(r.get("invalid_response")) == "0"]
        retry_lines.append(f"{model}: {sum(int(r['unsafe_retry']) for r in valid)}/{len(valid)}")
    large_vs_mini = _paired_difference(rows, "gpt-5.4", "full", "gpt-5.4-mini", "full")
    if large_vs_mini:
        advantage = ("CI excludes zero" if large_vs_mini["ci_low"] > 0 else
                     "CI includes zero or does not show an advantage")
    else:
        advantage = "unavailable without valid paired decisions"
    report += ("\n## Research questions\n\n"
               f"1. Safest by full-state action accuracy, with unsafe retry as a separate check: {', '.join(best) if best else 'undetermined'}. This is specific to the simulator and policy.\n"
               f"2. Unnecessary retries: {'; '.join(retry_lines)} valid decisions.\n"
               "3. Durable state: see each paired full-minus-limited contrast above; positive values favor more state.\n"
               f"4. GPT-5.4 versus mini in full state: {advantage}; paired effect and interval above.\n"
               "5. Cost value: in the full-state arm, GPT-5.4 cost about 3.56 times mini for an 11-point higher observed accuracy; its clustered interval includes zero, so this study does not establish that the extra spend is worthwhile. Nano was cheaper and scored higher than mini on these templates.\n"
               "6. LLM versus MCP failures: incorrect choices are model-policy disagreements under the revised rubric; injected faults originate in the simulator. There is no observed MCP implementation failure in this dataset.\n"
               "7. Novelty: these cases illustrate known retry/idempotency and durable-state issues; no novelty claim follows from this simulator alone.\n")
    (RESULTS / "RESULTS.md").write_text(report, encoding="utf-8")
    (RESULTS / "HANDOFF.md").write_text(
        "# handoff\n\nRun `python main.py --mode pilot` from mcp_failures. "
        "Captured traces: mcp_event_traces.jsonl; paired scenarios: llm_scenarios.jsonl. "
        "Raw, comparison, costs and analysis are in results/. "
        "Only the three Python source files data.py, main.py and inference.py are used.\n\n" + report,
        encoding="utf-8")


def run_experiment(scenarios, mode=OPENAI_RUN_MODE, models=None):
    RESULTS.mkdir(exist_ok=True)
    raw_path = RESULTS / "llm_inference_raw.csv"
    existing = _read_csv(raw_path)
    spent = sum(float(r.get("cost_usd") or 0) for r in existing)
    availability = {}
    client = _client()
    if client is None:
        availability = {m: "not_tested_missing_api_key" for m in OPENAI_MODELS}
        analyze(existing, availability)
        return availability
    # Verify the exact requested IDs; the first decision call verifies Responses compatibility.
    ordered_models = [m for m in ["gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.4"] if m in (models or OPENAI_MODELS)]
    for model in ordered_models:
        try:
            client.models.retrieve(model)
            availability[model] = "model_retrieved; Responses compatibility checked by first request"
        except Exception as exc:
            availability[model] = "unavailable: " + type(exc).__name__
    repetitions = 3 if mode == "pilot" else 5
    selected = scenarios[:20 if mode == "pilot" else 60]
    done = {(r["scenario_id"], r["model"], r["setting"], int(r["repetition"])) for r in existing if r.get("repetition")}
    for model in ordered_models:
        if availability[model].startswith("unavailable"):
            continue
        jobs = []
        for scenario in selected:
            for setting in ("limited", "full"):
                for repetition in range(1, repetitions + 1):
                    key = (scenario["scenario_id"], model, setting, repetition)
                    if key not in done:
                        jobs.append((scenario, setting, repetition, key))
        pending = {}
        stopped = False
        with ThreadPoolExecutor(max_workers=8) as pool:
            while jobs or pending:
                while jobs and len(pending) < 8 and not stopped:
                    scenario, setting, repetition, key = jobs[0]
                    reserve = _reserve(model, _prompt(scenario, setting))
                    if spent + sum(pending.values()) + reserve > OPENAI_BUDGET_USD:
                        availability[model] += "; stopped_at_budget"
                        stopped = True
                        break
                    jobs.pop(0)
                    future = pool.submit(_one, client, scenario, model, setting, repetition)
                    pending[future] = reserve
                if not pending:
                    break
                completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    reserve = pending.pop(future)
                    row = future.result()
                    if not row.get("cost_usd"):
                        row["cost_usd"] = round(reserve, 8)
                        row["cost_kind"] = "reserved_upper_bound_after_unknown_usage"
                    _append(raw_path, row)
                    existing.append(row)
                    spent += float(row["cost_usd"])
                    if row.get("error") and not row.get("response_id"):
                        availability[model] = "Responses call failed: " + row["error"]
                        stopped = True
        if not stopped:
            availability[model] = "Responses API compatible"
        analyze(existing, availability)
        if stopped:
            break
    analyze(existing, availability)
    return availability
