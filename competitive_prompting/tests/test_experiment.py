from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from competitive_prompting.analysis import (cluster_ci, paired, summarize, write_reports,
                                           episode_outcome, output_truncated)
from competitive_prompting.budget import Budget, BudgetError, RunLock, call_allowance, usage_cost
from competitive_prompting.prompts import CONDITIONS, SYSTEM, messages
from competitive_prompting.runner import (Limits, LiveResponses, OfflineResponses, main,
                                         run_episode, schedule, write_result)
from competitive_prompting.simulator import Environment, TOOLS
from competitive_prompting.tasks import BY_ID, TASKS

PRICES = {"input": 2, "cached_input": .1, "cache_write": 2.5, "output": 10}
USAGE = {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 0},
         "output_tokens": 50, "output_tokens_details": {"reasoning_tokens": 30}, "total_tokens": 150}


def report(status="completed"):
    return json.dumps({"status": status, "reason": "Test evidence."})


def response(name=None, args=None, text=None, status="completed", usage=USAGE):
    output = [{"type": "function_call", "call_id": "c", "name": name,
               "arguments": json.dumps(args or {})}] if name else [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text or report()}]}]
    return {"id": "r", "status": status, "output": output, "usage": deepcopy(usage)}


class Script:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def create(self, **request):
        self.requests.append(deepcopy(request))
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        value = deepcopy(value)
        for item in value.get("output", []):
            if item.get("call_id") == "c":
                item["call_id"] = f"c-{len(self.requests)}"
        return value


def episode(task="stopping_done", condition="A", transport=None, **kwargs):
    return run_episode(BY_ID[task], condition, "gpt-6-luna", 0, 7,
                       transport or OfflineResponses(), **kwargs)


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.id)
def test_all_tasks_deterministic_success_and_isolation(task):
    first = run_episode(task, "A", "gpt-6-luna", 0, 7, OfflineResponses())
    second = run_episode(task, "D", "gpt-6-luna", 0, 7, OfflineResponses())
    assert first["verified_task_success"] is True
    assert first["constraint_violations"] == 0
    assert first["duplicate_operations"] == 0
    assert first["unnecessary_tool_calls"] == 0
    assert first["final_state"] == second["final_state"]
    assert first["action_trace"] == second["action_trace"]
    assert first["input_tokens"] is None
    assert first["estimated_api_cost_usd"] == 0
    env = Environment(task)
    initial = deepcopy(env.state)
    env.state[task.records[0].id]["counts"][task.records[0].steps[0]] = 999
    assert Environment(task).state == initial


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.id)
def test_every_task_rejects_unsupported_completion(task):
    result = Environment(task).evaluate(report(), "completed")
    assert result["verified_task_success"] is False
    assert result["false_completion_claim"] is True


def test_timeout_before_execution_and_recovery():
    env = Environment(BY_ID["recovery_timeout"])
    env.call("read_records", {})
    env.call("check_requirements", {"record_id": "T2"})
    args = {"record_id": "T2", "operation": "reserve"}
    assert not env.call("apply_operation", args)["ok"]
    assert env.state["T2"]["counts"]["reserve"] == 0
    env.call("read_records", {})  # Useful uncertainty resolution, not an unnecessary read.
    assert not env.trace[-1]["unnecessary"]
    assert env.call("apply_operation", args)["ok"]
    assert env.trace[-1]["retry"] and not env.trace[-1]["duplicate"]


def test_post_commit_failure_duplicate_and_incorrect_retry():
    env = Environment(BY_ID["recovery_ack"])
    env.call("read_records", {})
    env.call("check_requirements", {"record_id": "A8"})
    args = {"record_id": "A8", "operation": "charge"}
    assert env.call("apply_operation", args)["error"] == "timeout"
    assert env.state["A8"]["counts"]["charge"] == 1
    assert env.call("read_records", {})["records"]["A8"]["counts"]["charge"] == 1
    env.call("apply_operation", args)
    env.call("verify_completion", {})
    score = env.evaluate(report(), "completed")
    assert env.state["A8"]["counts"]["charge"] == 2
    assert score["duplicate_operations"] == score["incorrect_retries"] == 1
    assert score["constraint_violations"] == 1
    assert not score["verified_task_success"]


@pytest.mark.parametrize("task", ["stopping_unauthorized", "stopping_unavailable"])
def test_correct_and_incorrect_blocked_stopping(task):
    env = Environment(BY_ID[task])
    env.call("read_records", {})
    env.call("verify_completion", {})
    assert env.evaluate(report("blocked"), "completed")["verified_task_success"]
    assert not env.evaluate(report(), "completed")["correct_stopping"]
    assert env.evaluate(report(), "completed")["false_completion_claim"]
    record = BY_ID[task].records[0]
    env.call("check_requirements", {"record_id": record.id})
    env.call("apply_operation", {"record_id": record.id, "operation": record.steps[0]})
    assert env.evaluate(report("blocked"), "completed")["constraint_violations"] == 1
    assert not env.evaluate(report("blocked"), "completed")["verified_task_success"]


def test_unnecessary_continuation_and_read_response_copy():
    env = Environment(BY_ID["stopping_done"])
    result = env.call("read_records", {})
    result["records"]["S1"]["counts"]["resolve"] = 100
    assert env.state["S1"]["counts"]["resolve"] == 1
    env.call("read_records", {})
    assert env.trace[-1]["unnecessary"]
    env.call("check_requirements", {"record_id": "S1"})
    env.call("apply_operation", {"record_id": "S1", "operation": "resolve"})
    env.call("verify_completion", {})
    score = env.evaluate(report(), "completed")
    assert score["unnecessary_continuation"] == 3
    assert score["duplicate_operations"] == 1
    assert score["false_completion_claim"]


def test_prerequisites_and_preserved_ineligible_record():
    env = Environment(BY_ID["execution_batch"])
    env.call("read_records", {})
    for rid, op in [("R1", "index"), ("R3", "archive")]:
        env.call("check_requirements", {"record_id": rid})
        assert not env.call("apply_operation", {"record_id": rid, "operation": op})["ok"]
    assert env.version == 0
    assert env.evaluate(report(), "completed")["constraint_violations"] == 2


@pytest.mark.parametrize("args", [None, [], "bad", {"record_id": 3}, {"extra": "x"}])
def test_invalid_calls_recorded(args):
    env = Environment(BY_ID["stopping_done"])
    assert env.call("read_records", args)["error"] == "invalid_call"
    assert len(env.trace) == 1 and env.trace[0]["violation"]


@pytest.mark.parametrize("text", ["done", "[]", "null", '{"status":"completed"}'])
def test_invalid_final_reports_fail(text):
    env = Environment(BY_ID["stopping_done"])
    env.call("verify_completion", {})
    assert not env.evaluate(text, "completed")["verified_task_success"]


def test_prompt_isolation_and_seeded_schedule():
    requests = []
    for condition in CONDITIONS:
        transport = Script([response()])
        episode(condition=condition, transport=transport)
        request = deepcopy(transport.requests[0])
        assert request["input"][0]["content"] == CONDITIONS[condition] + "\n\n" + BY_ID["stopping_done"].instruction
        request["input"] = []
        requests.append(request)
    assert all(r == requests[0] for r in requests)
    assert requests[0]["instructions"] == SYSTEM
    a = schedule(["a", "b"], TASKS, 2, 10)
    assert a == schedule(["a", "b"], TASKS, 2, 10)
    assert a != schedule(["a", "b"], TASKS, 2, 11)
    assert len(a) == 2 * 12 * 2 * 4
    for start in range(0, len(a), 4):
        assert {r["condition"] for r in a[start:start + 4]} == set(CONDITIONS)


def test_verified_state_but_incomplete_api_response_is_never_success():
    transport = Script([response("verify_completion"), response(status="incomplete")])
    row = episode(transport=transport)
    assert row["state_correct"]
    assert row["termination_reason"] == "incomplete_response"
    assert row["verified_task_success"] is False
    assert row["final_text"] == ""


def test_partial_function_call_never_executes():
    transport = Script([response("read_records", status="incomplete")])
    row = episode(transport=transport)
    assert row["tool_calls"] == 0


def test_tool_and_model_limits_and_reasoning_history():
    call = response("read_records")
    call["output"].insert(0, {"type": "reasoning", "id": "reason", "encrypted_content": "opaque", "summary": []})
    transport = Script([call, response("read_records")])
    row = episode(transport=transport, limits=Limits(tool_calls=1, model_calls=3))
    assert row["tool_calls"] == 1 and row["termination_reason"] == "tool_limit"
    assert transport.requests[1]["input"][1]["type"] == "reasoning"
    assert transport.requests[1]["store"] is False
    row = episode(transport=Script([response("read_records")]), limits=Limits(model_calls=1))
    assert row["termination_reason"] == "model_limit" and not row["verified_task_success"]
    row = episode(limits=Limits(request_bytes=1))
    assert row["termination_reason"] == "request_size_limit" and row["model_calls"] == 0


def test_harmless_extra_calls_preserve_success_but_reduce_stopping_quality():
    env = Environment(BY_ID["stopping_done"])
    env.call("verify_completion", {})
    env.call("verify_completion", {})
    score = env.evaluate(report(), "completed")
    assert score["verified_task_success"] is True
    assert score["correct_stopping"] is False
    assert score["unnecessary_tool_calls"] == score["unnecessary_continuation"] == 1


def test_paid_incomplete_response_is_charged_without_accepting_claim(tmp_path):
    budget = Budget(tmp_path / "spend.jsonl")
    row = episode(transport=Script([response(status="incomplete")]), dry_run=False,
                  pricing=PRICES, budget=budget)
    assert row["termination_reason"] == "incomplete_response"
    assert row["verified_task_success"] is False
    assert row["estimated_api_cost_usd"] == .000662
    assert row["input_tokens"] == 100
    assert not budget.pending()


def test_paid_loop_requires_budget_and_pricing():
    with pytest.raises(ValueError, match="budget and pricing"):
        episode(dry_run=False)


def test_cost_uses_reported_tokens_including_cached_without_double_billing_reasoning():
    assert usage_cost(USAGE, PRICES) == Decimal("0.000662")
    assert usage_cost(None, PRICES) is None
    assert usage_cost({**USAGE, "input_tokens": -1}, PRICES) is None
    with_writes = {**USAGE, "input_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 10}}
    assert usage_cost(with_writes, PRICES) == Decimal(".000667")
    no_write_counter = {**USAGE, "input_tokens_details": {"cached_tokens": 20}}
    assert usage_cost(no_write_counter, PRICES) == Decimal(".000702")
    req = {"max_output_tokens": 2048, "input": "abc"}
    assert call_allowance(req, PRICES) >= Decimal(".02")
    assert call_allowance({**req, "input": "x" * 100000}, PRICES) > call_allowance(req, PRICES)


def test_budget_cumulative_caps_remaining_and_persistence(tmp_path):
    path = tmp_path / "spend.jsonl"
    b = Budget(path)
    first = b.reserve(.5, "pilot")
    b.settle(first, .5)
    with pytest.raises(BudgetError, match="Pilot"):
        Budget(path).reserve(.26, "pilot")
    b = Budget(path, stage="main", remaining=.3)
    with pytest.raises(BudgetError, match="remaining"):
        b.reserve(.31, "main")
    second = b.reserve(.3, "main")
    b.settle(second, .2)
    assert Budget(path).reported_spend() == Decimal(".7")
    with pytest.raises(BudgetError):
        Budget(path, stage="main", total_cap=99).reserve(8.31, "main")
    with pytest.raises(BudgetError):
        Budget(path, pilot_cap=99).reserve(.26, "pilot")


def test_unknown_charges_block_future_requests_and_restart(tmp_path):
    path = tmp_path / "spend.jsonl"
    b = Budget(path)
    b.reserve(.02, "episode")
    assert b.exposure() == Decimal(".02")
    with pytest.raises(BudgetError):
        b.reserve(.02, "next")
    with pytest.raises(BudgetError, match="Unsettled"):
        Budget(path)


def test_overrun_is_charged_then_halts(tmp_path):
    b = Budget(tmp_path / "spend.jsonl")
    reservation = b.reserve(.02, "episode")
    with pytest.raises(BudgetError, match="exceeded"):
        b.settle(reservation, .03)
    assert b.reported_spend() == Decimal(".03")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -.1])
def test_invalid_budget_values_fail_closed(tmp_path, value):
    with pytest.raises(ValueError):
        Budget(tmp_path / "spend.jsonl", remaining=value)


def test_lock_prevents_concurrent_paid_runs(tmp_path):
    path = tmp_path / "spend.jsonl"
    with RunLock(path):
        with pytest.raises(BudgetError, match="locked"):
            with RunLock(path):
                pass
    assert not Path(str(path) + ".lock").exists()


def test_no_request_if_allowance_exceeds_budget(tmp_path):
    transport = Script([response()])
    row = episode(transport=transport, dry_run=False, pricing=PRICES,
                  budget=Budget(tmp_path / "spend.jsonl", remaining=.001))
    assert transport.requests == []
    assert row["termination_reason"] == "budget_limit"
    assert row["verified_task_success"] is None


@pytest.mark.parametrize("value,termination", [(RuntimeError("secret must not be logged"), "api_error"),
                                               (response(usage=None), "usage_missing")])
def test_api_error_and_missing_usage_no_retries(tmp_path, value, termination):
    transport = Script([value])
    budget = Budget(tmp_path / "spend.jsonl")
    row = episode(transport=transport, dry_run=False, pricing=PRICES, budget=budget)
    assert row["termination_reason"] == termination
    assert row["verified_task_success"] is None
    assert row["estimated_api_cost_usd"] is None
    assert row["input_tokens"] is None
    assert budget.pending()
    assert len(transport.requests) == 1
    assert "secret must not be logged" not in json.dumps(row)


def test_mock_paid_usage_and_serialization(tmp_path):
    budget = Budget(tmp_path / "spend.jsonl")
    transport = Script([response("verify_completion"), response()])
    row = episode(transport=transport, dry_run=False, pricing=PRICES, budget=budget)
    assert row["verified_task_success"] is True
    assert row["input_tokens"] == 200 and row["cached_input_tokens"] == 40
    assert row["output_tokens"] == 100 and row["reasoning_tokens"] == 60
    assert row["estimated_api_cost_usd"] == .001324
    assert row["cumulative_estimated_spend_usd"] == .001324
    path = tmp_path / "episodes.jsonl"
    write_result(path, row)
    assert json.loads(path.read_text()) == row


def test_cli_default_offline_and_explicit_live_guard(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Live client was created in a dry run")
    # isinstance guard checks the class; use a class whose constructor fails.
    class Forbidden:
        __init__ = forbidden
    monkeypatch.setattr("competitive_prompting.runner.LiveResponses", Forbidden)
    directory = main(["--output-dir", str(tmp_path), "--tasks", "stopping_done"])
    rows = [json.loads(line) for line in (directory / "episodes.jsonl").read_text().splitlines()]
    assert len(rows) == 8 and all(r["dry_run"] and r["verified_task_success"] for r in rows)
    assert (directory / "summary.csv").exists() and (directory / "manifest.json").exists()


def test_live_client_requires_explicit_execution():
    with pytest.raises(ValueError):
        LiveResponses()


def analysis_rows():
    rows = []
    for task in range(12):
        for rep in range(3):
            for condition in CONDITIONS:
                rows.append({"task_id": str(task), "category": ["execution", "recovery", "stopping"][task // 4],
                    "model": "model", "condition": condition, "repetition": rep, "seed": 1,
                    "run_id": "run", "dry_run": False, "termination_reason": "completed",
                    "model_calls": 1,
                    "verified_task_success": condition in "CD", "tool_calls": 2 if condition in "CD" else 1,
                    "estimated_api_cost_usd": .01, "known_api_cost_usd": .01, "retries": 0,
                    "constraint_violations": 0, "correct_stopping": True, "false_completion_claim": False})
    return rows


def test_analysis_pairs_task_clusters_not_repetitions(tmp_path):
    rows = analysis_rows()
    comparison = next(r for r in paired(rows, 7) if r["category"] == "all" and
                      r["comparison"] == "primary_generic_vs_pressure" and r["metric"] == "verified_task_success")
    assert comparison["paired_tasks"] == 12 and comparison["paired_blocks"] == 36
    assert comparison["mean_paired_difference"] == 1
    assert comparison["ci_low"] == comparison["ci_high"] == 1
    assert cluster_ci([1, 0, 1, 0]) == (None, None)
    rows.pop()  # Incomplete block must not become an unpaired comparison.
    result = next(r for r in paired(rows, 7) if r["category"] == "all" and
                  r["comparison"] == "exploratory_combined_rivalry_vs_pressure" and r["metric"] == "verified_task_success")
    assert result["paired_blocks"] == 35 and result["paired_tasks"] == 12
    write_reports(rows, tmp_path)
    assert "mean_paired_difference" in (tmp_path / "paired.csv").read_text()


def test_analysis_exclusions_unknown_cost_and_dry_runs(tmp_path):
    rows = analysis_rows()[:4]
    rows[0].update(termination_reason="api_error", verified_task_success=None, estimated_api_cost_usd=None)
    summary = next(r for r in summarize(rows, 7) if r["condition"] == "A" and r["category"] == "all")
    assert summary["episodes_evaluable"] == 0 and summary["infrastructure_exclusions"] == 1
    assert summary["cost_per_success_usd"] is None
    for row in rows:
        row["dry_run"] = True
    write_reports(rows, tmp_path)
    assert (tmp_path / "summary.csv").read_text() == ""


def test_analysis_rejects_duplicate_and_incompatible_runs(tmp_path):
    rows = analysis_rows()
    with pytest.raises(ValueError, match="Duplicate"):
        paired(rows + [rows[0]], 7)
    rows[0]["reasoning_effort"] = "high"
    with pytest.raises(ValueError, match="configurations"):
        write_reports(rows, tmp_path)


# Phase 2 readiness and stage-gate checks. All model activity below is scripted.
from competitive_prompting.runner import (ROOT, PILOT_MODELS, build_pilot_manifest, build_request,
    load_pilot_manifest, stage_b_allowance, validate_pilot_episode, validate_pilot_stage)


@pytest.fixture
def pilot():
    return build_pilot_manifest(json.loads((ROOT / "pricing.json").read_text()))


def pilot_rows(pilot, stage="A", budget=None):
    rows = []
    for item in pilot["plan"]:
        if item["pilot_stage"] != stage:
            continue
        transport = OfflineResponses()
        if budget:
            offline = transport
            class WithUsage:
                def create(self, **request):
                    value = offline.create(**request)
                    value["usage"] = deepcopy(USAGE)
                    return value
            transport = WithUsage()
        row = run_episode(BY_ID[item["task_id"]], item["condition"], item["model"], 0,
                          item["seed"], transport, dry_run=budget is None, budget=budget,
                          pricing=pilot["pricing_config"]["models"][item["model"]],
                          episode_id=item["episode_id"])
        row.update(pilot_manifest_sha256=pilot["sha256"], source_sha256=pilot["source_sha256"],
                   pilot_stage=stage, pilot_plan_index=item["order"])
        rows.append(row)
    return rows


def test_frozen_pilot_manifest_and_randomization(pilot, tmp_path):
    assert len(pilot["plan"]) == pilot["planned_episodes"] == 24
    assert sum(p["pilot_stage"] == "A" for p in pilot["plan"]) == 8
    assert len({p["episode_id"] for p in pilot["plan"]}) == 24
    assert pilot == build_pilot_manifest(pilot["pricing_config"])
    assert pilot["seed"] == 20261027
    for model in PILOT_MODELS:
        for tid in pilot["selected_tasks"]:
            assert {p["condition"] for p in pilot["plan"] if p["model"] == model and p["task_id"] == tid} == set(CONDITIONS)
    assert {c: v["text"] for c, v in pilot["conditions"].items()} == CONDITIONS
    assert pilot["caps_usd"] == {"pilot": "0.75", "experiment": "9.00"}
    path = tmp_path / "pilot.json"
    path.write_text(json.dumps(pilot))
    assert load_pilot_manifest(path, pilot["pricing_config"]) == pilot
    pilot["plan"][0]["condition"] = "tampered"
    path.write_text(json.dumps(pilot))
    with pytest.raises(ValueError, match="differs"):
        load_pilot_manifest(path, pilot["pricing_config"])


def test_both_offline_pilot_stages_replay(pilot):
    a, b = pilot_rows(pilot), pilot_rows(pilot, "B")
    assert validate_pilot_stage(a, pilot, "A")["passed"]
    assert validate_pilot_stage(b, pilot, "B")["passed"]
    assert all(r["verified_task_success"] for r in a + b)
    assert stage_b_allowance(pilot, a) > 0


@pytest.mark.parametrize("corruption", ["usage", "cost", "state", "trace", "chain", "identity", "prompt", "score", "tokens"])
def test_stage_a_gate_rejects_corrupted_live_records(pilot, tmp_path, corruption):
    budget = Budget(tmp_path / "spend.jsonl")
    rows = pilot_rows(pilot, budget=budget)
    assert validate_pilot_stage(rows, pilot, "A", budget)["passed"]
    row = rows[0]
    if corruption == "usage":
        row["reported_usage"][0]["total_tokens"] += 1
    elif corruption == "cost":
        row["estimated_api_cost_usd"] += .1
    elif corruption == "state":
        row["final_state"]["I17"]["counts"]["pay"] = 20
    elif corruption == "trace":
        row["action_trace"].pop()
    elif corruption == "chain":
        row["interactions"][1]["request"]["input"].pop()
    elif corruption == "identity":
        row["episode_id"] = "other"
    elif corruption == "prompt":
        row["framing_prompt"] = "changed"
    elif corruption == "score":
        row["verified_task_success"] = False
    elif corruption == "tokens":
        row["total_tokens"] += 1
    assert not validate_pilot_stage(rows, pilot, "A", budget)["passed"]


def test_stage_gate_rejects_partial_duplicate_and_missing_episodes(pilot):
    rows = pilot_rows(pilot)
    assert not validate_pilot_stage(rows[:-1], pilot, "A")["passed"]
    assert not validate_pilot_stage(rows + [rows[0]], pilot, "A")["passed"]
    rows[0]["termination_reason"] = "incomplete_response"
    assert not validate_pilot_stage(rows, pilot, "A")["passed"]


def test_pilot_ledger_caps_persist_across_stage_a_and_b(pilot, tmp_path):
    path = tmp_path / "spend.jsonl"
    budget = Budget(path)
    a = pilot_rows(pilot, budget=budget)
    assert validate_pilot_stage(a, pilot, "A", budget)["passed"]
    after_a = budget.reported_spend()
    budget = Budget(path)
    b = pilot_rows(pilot, "B", budget)
    assert validate_pilot_stage(b, pilot, "B", budget)["passed"]
    assert budget.reported_spend() > after_a
    budget.require_available(stage_b_allowance(pilot, a))
    # A later invocation retains every Stage A/B charge and refuses any excess.
    with pytest.raises(BudgetError, match="Pilot"):
        Budget(path).reserve(Decimal(".750001") - budget.reported_spend(), "new")
    with pytest.raises(BudgetError):
        Budget(path, remaining=.001).require_available(stage_b_allowance(pilot, a))


@pytest.mark.parametrize("termination", ["incomplete_response", "tool_limit", "model_limit", "request_size_limit"])
def test_agent_limits_and_output_truncation_count_as_operational_failures(termination):
    rows = analysis_rows()[:4]
    rows[2]["termination_reason"] = termination
    rows[2]["verified_task_success"] = False
    if termination == "incomplete_response":
        rows[2]["interactions"] = [{"response": {"status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"}}}]
    entry = next(r for r in summarize(rows, 7) if r["category"] == "all" and r["condition"] == "C")
    assert entry["episodes_evaluable"] == 1
    assert entry["operational_failures"] == 1 and entry["success_rate"] == 0
    assert entry["completed_episode_success_rate"] is None
    comparisons = [r for r in paired(rows, 7) if r["comparison"] == "primary_generic_vs_pressure"]
    assert any(r["metric"] == "operational_task_success" for r in comparisons)
    assert not any(r["metric"] == "completed_episode_task_success" for r in comparisons)


def test_pilot_task_comparisons_and_metrics(pilot, tmp_path):
    rows = pilot_rows(pilot) + pilot_rows(pilot, "B")
    write_reports(rows, tmp_path, include_dry_run=True)
    import csv
    summary = list(csv.DictReader((tmp_path / "task_summary.csv").open()))
    contrasts = list(csv.DictReader((tmp_path / "task_paired.csv").open()))
    assert len(summary) == 24
    assert {r["task_id"] for r in contrasts} == set(pilot["selected_tasks"])
    assert {r["comparison"] for r in contrasts} >= {"primary_generic_vs_pressure", "named_vs_generic",
            "pressure_vs_neutral", "generic_vs_neutral", "exploratory_combined_rivalry_vs_pressure"}
    assert all(r["ci_low"] == r["ci_high"] == "" for r in contrasts)


def test_duplicate_function_ids_not_executed_twice():
    first = response("read_records")
    first["output"][0]["call_id"] = "duplicate"
    second = deepcopy(first)
    row = episode(transport=Script([first, second]))
    assert row["termination_reason"] == "protocol_error"
    assert row["tool_calls"] == 1 and row["verified_task_success"] is None


def test_mixed_ttl_cache_write_cost_and_invalid_split():
    usage = deepcopy(USAGE)
    usage["input_tokens_details"].update(cache_write_tokens=20,
        cache_write_tokens_details={"ttl_30m_tokens": 10, "ttl_12h_tokens": 10})
    assert usage_cost(usage, PRICES) == Decimal(".000697")
    usage["input_tokens_details"]["cache_write_tokens_details"]["ttl_12h_tokens"] = 11
    assert usage_cost(usage, PRICES) is None


def test_pilot_cli_stages_are_offline_by_default_and_manifest_is_authoritative(tmp_path, pilot, monkeypatch):
    path = tmp_path / "pilot.json"
    path.write_text(json.dumps(pilot))
    a = main(["--pilot-manifest", str(path), "--pilot-stage", "A", "--output-dir", str(tmp_path)])
    b = main(["--pilot-manifest", str(path), "--pilot-stage", "B", "--stage-a-results", str(a / "episodes.jsonl"),
              "--output-dir", str(tmp_path)])
    assert json.loads((a / "validation.json").read_text())["passed"]
    assert json.loads((b / "validation.json").read_text())["passed"]
    with pytest.raises(SystemExit):
        main(["--pilot-manifest", str(path), "--pilot-stage", "A", "--max-output-tokens", "1"])
    monkeypatch.setenv("OPENAI_API_KEY", "offline-placeholder")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "proj_offline")
    with pytest.raises(SystemExit):
        main(["--pilot-manifest", str(path), "--pilot-stage", "B", "--stage-a-results", str(a / "episodes.jsonl"), "--execute"])


def test_stage_gate_allows_verified_agent_failure(pilot):
    rows = pilot_rows(pilot)
    item = next(p for p in pilot["plan"] if p["episode_id"] == rows[0]["episode_id"])
    bad_agent = Script([response("read_records", usage=None), response("verify_completion", usage=None),
                        response(text=report("blocked"), usage=None)])
    row = run_episode(BY_ID[item["task_id"]], item["condition"], item["model"], 0, item["seed"],
                      bad_agent, episode_id=item["episode_id"],
                      pricing=pilot["pricing_config"]["models"][item["model"]])
    row.update(pilot_manifest_sha256=pilot["sha256"], source_sha256=pilot["source_sha256"], pilot_stage="A")
    rows[0] = row
    assert row["verified_task_success"] is False
    assert validate_pilot_stage(rows, pilot, "A")["passed"]


@pytest.fixture
def mock_live_pilot(tmp_path, pilot, monkeypatch):
    """Exercise the paid CLI path with in-memory responses and a temporary ledger."""
    import competitive_prompting.runner as runner
    ledger = tmp_path / "mock-spend.jsonl"
    instances = []
    class FakeLive:
        def __init__(self, *, execute):
            assert execute
            self.responses = OfflineResponses()
            self.calls = 0
            instances.append(self)

        def check_access(self, models):
            return {m: {"requested_model": m, "returned_id": m} for m in models}

        def create(self, **request):
            self.calls += 1
            result = self.responses.create(**request)
            result["usage"] = deepcopy(USAGE)
            return result

        def close(self):
            pass

    monkeypatch.setattr(runner, "LiveResponses", FakeLive)
    monkeypatch.setattr(runner, "Budget", lambda _, *a: Budget(ledger, *a))
    monkeypatch.setattr(runner, "RunLock", lambda _: RunLock(ledger))
    monkeypatch.setenv("OPENAI_API_KEY", "offline-placeholder")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "proj_offline")
    path = tmp_path / "pilot.json"
    path.write_text(json.dumps(pilot))
    common = ["--pilot-manifest", str(path), "--execute", "--remaining-budget", ".75"]
    a = main(common + ["--pilot-stage", "A", "--output-dir", str(tmp_path)])
    return path, a, ledger, instances, common


def test_mock_live_cli_runs_only_the_declared_24_episodes(mock_live_pilot, tmp_path):
    path, a, ledger, instances, common = mock_live_pilot
    b = main(common + ["--pilot-stage", "B", "--stage-a-results", str(a / "episodes.jsonl"),
                       "--output-dir", str(tmp_path)])
    assert json.loads((b / "validation.json").read_text())["passed"]
    assert sum(len(p.read_text().splitlines()) for p in (a / "episodes.jsonl", b / "episodes.jsonl")) == 24
    budget = Budget(ledger)
    assert not budget.pending() and budget.exposure("pilot") < Decimal(".75")
    before = sum(i.calls for i in instances)
    with pytest.raises(BudgetError, match="already issued"):
        main(common + ["--pilot-stage", "A", "--output-dir", str(tmp_path / "repeat")])
    assert sum(i.calls for i in instances) == before


@pytest.mark.parametrize("reason", ["corrupt", "budget", "project"])
def test_live_stage_b_stops_before_responses_if_admission_fails(mock_live_pilot, tmp_path, monkeypatch, reason):
    path, a, ledger, instances, common = mock_live_pilot
    before = sum(i.calls for i in instances)
    if reason == "corrupt":
        rows = [json.loads(line) for line in (a / "episodes.jsonl").read_text().splitlines()]
        rows[0]["action_trace"].pop()
        (a / "episodes.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    if reason == "project":
        monkeypatch.setenv("OPENAI_PROJECT_ID", "proj_different")
    args = common + ["--pilot-stage", "B", "--stage-a-results", str(a / "episodes.jsonl"),
                     "--output-dir", str(tmp_path)]
    if reason == "budget":
        args += ["--remaining-budget", ".001"]
    with pytest.raises((BudgetError, ValueError)):
        main(args)
    assert sum(i.calls for i in instances) == before


@pytest.mark.parametrize("termination,calls,success,report_valid,reason,expected", [
    ("completed", 1, True, True, None, "success"),
    ("completed", 1, False, True, None, "failure"),
    ("completed", 1, False, False, None, "failure"),
    ("model_limit", 2, False, False, None, "failure"),
    ("tool_limit", 2, False, False, None, "failure"),
    ("incomplete_response", 1, False, False, "max_output_tokens", "failure"),
    ("incomplete_response", 1, False, False, "content_filter", "infrastructure"),
    ("incomplete_response", 1, False, False, None, "infrastructure"),
    ("api_error", 1, None, False, None, "infrastructure"),
    ("usage_missing", 1, None, False, None, "infrastructure"),
    ("protocol_error", 1, None, False, None, "infrastructure"),
    ("budget_limit", 0, None, False, None, "budget_censored"),
    ("budget_limit", 2, None, False, None, "failure"),
    ("request_size_limit", 0, False, False, None, "infrastructure"),
])
def test_operational_outcome_categories(termination, calls, success, report_valid, reason, expected):
    row = analysis_rows()[0]
    row.update(termination_reason=termination, model_calls=calls, verified_task_success=success,
               valid_final_report=report_valid,
               interactions=[{"response": {"incomplete_details": {"reason": reason}}}])
    assert episode_outcome(row) == expected
    assert output_truncated(row) == (termination == "incomplete_response" and reason == "max_output_tokens")


def test_operational_denominators_and_diagnostic_rates():
    base = analysis_rows()[0]
    cases = [
        ("completed", 1, True, True), ("completed", 1, False, True), ("completed", 1, False, False),
        ("model_limit", 2, False, False), ("tool_limit", 2, False, False),
        ("incomplete_response", 1, False, False), ("api_error", 1, None, False),
        ("budget_limit", 0, None, False), ("budget_limit", 2, None, False),
        ("request_size_limit", 0, False, False),
    ]
    rows = []
    for repetition, (termination, calls, success, valid) in enumerate(cases):
        row = {**base, "termination_reason": termination, "model_calls": calls,
               "verified_task_success": success, "valid_final_report": valid, "repetition": repetition}
        if termination == "incomplete_response":
            row["interactions"] = [{"response": {"incomplete_details": {"reason": "max_output_tokens"}}}]
        rows.append(row)
    result = next(r for r in summarize(rows, 7) if r["category"] == "all")
    assert result["episodes_recorded"] == 10 and result["attempted_episodes"] == 8
    assert result["episodes_evaluable"] == 7
    assert result["operational_successes"] == 1 and result["operational_failures"] == 6
    assert result["success_rate"] == result["operational_success_rate"] == 1 / 7
    assert result["completed_episodes"] == 3 and result["completed_episode_success_rate"] == 1 / 3
    assert result["infrastructure_exclusions"] == 2 and result["budget_censored_episodes"] == 1
    assert result["attempted_budget_interruptions"] == 1
    assert result["exclusion_rate"] == .3 and result["infrastructure_exclusion_rate"] == .2
    assert result["budget_censoring_rate"] == .1
    assert result["truncations"] == 1 and result["truncation_rate"] == 1 / 8
    assert result["limit_hits"] == 2 and result["limit_hit_rate"] == 2 / 8
    assert result["invalid_final_reports"] == 1


def test_operational_rate_weights_attempted_episodes_not_tasks():
    first = {**analysis_rows()[0], "verified_task_success": True}
    rows = [first, {**first, "repetition": 1},
            {**first, "task_id": "other", "verified_task_success": False}]
    result = next(r for r in summarize(rows, 7) if r["category"] == "all")
    assert result["success_rate"] == result["completed_episode_success_rate"] == 2 / 3
    assert result["task_macro_success_rate"] == result["completed_task_macro_success_rate"] == .5


def test_primary_paired_contrast_includes_limits_and_keeps_completed_diagnostic():
    rows = analysis_rows()[:4]
    rows[1]["verified_task_success"] = True
    rows[2].update(termination_reason="tool_limit", verified_task_success=False)
    comparisons = [r for r in paired(rows, 7) if r["category"] == "all"
                   and r["comparison"] == "primary_generic_vs_pressure"]
    assert next(r for r in comparisons if r["metric"] == "operational_task_success")["mean_paired_difference"] == -1
    assert not any(r["metric"] == "completed_episode_task_success" for r in comparisons)


def test_analysis_uses_reported_total_tokens_without_adding_reasoning():
    row = {**analysis_rows()[0], "input_tokens": 100, "output_tokens": 50,
           "reasoning_tokens": 30, "total_tokens": 150}
    result = next(r for r in summarize([row], 7) if r["category"] == "all")
    assert result["mean_total_tokens"] == 150


def test_v4_preserves_execution_order_and_uses_new_identities(pilot):
    for name in ("pilot_manifest.json", "pilot_manifest_v2.json", "pilot_manifest_v3.json"):
        old = json.loads((ROOT / name).read_text())
        assert old["source_sha256"] != pilot["source_sha256"]
        for key in ("seed", "models", "repetitions", "selected_tasks", "conditions", "system_prompt",
                    "tools", "final_format", "tasks", "limits", "reasoning_effort", "request_settings",
                    "pricing_config", "caps_usd", "randomization", "stage_b_admission", "analysis"):
            assert old[key] == pilot[key]
        assert [{k: v for k, v in p.items() if k != "episode_id"} for p in old["plan"]] == [
            {k: v for k, v in p.items() if k != "episode_id"} for p in pilot["plan"]]
        assert not {p["episode_id"] for p in old["plan"]} & {p["episode_id"] for p in pilot["plan"]}
        assert old["manifest_id"] != pilot["manifest_id"]
        if name == "pilot_manifest_v3.json":
            assert old["evaluation_rules"] == pilot["evaluation_rules"]
        with pytest.raises(ValueError, match="differs"):
            load_pilot_manifest(ROOT / name, pilot["pricing_config"])
    rules = " ".join(pilot["evaluation_rules"])
    assert "Operational primary" in rules and "configured output truncation are failures" in rules


from competitive_prompting.budget import (ENCUMBRANCE_ID, ENCUMBRANCE_AMOUNT,
    ENCUMBRANCE_EPISODE_ID, ENCUMBRANCE_REQUEST_ID, approve_encumbrance, replay_ledger)


def historical_ledger(tmp_path, approved=False):
    path = tmp_path / "historical-spend.jsonl"
    events = [
        {"event": "reserve", "id": "settled-first-request", "stage": "pilot", "episode_id": ENCUMBRANCE_EPISODE_ID, "allowance": ".04563125"},
        {"event": "settle", "id": "settled-first-request", "stage": "pilot", "cost": ".000950"},
        {"event": "reserve", "id": ENCUMBRANCE_ID, "stage": "pilot", "episode_id": ENCUMBRANCE_EPISODE_ID, "allowance": ".0489500"}]
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    if approved:
        approve_encumbrance(path, ENCUMBRANCE_ID, approved=True)
    return path


def test_explicit_encumbrance_is_append_only_exactly_once_and_not_a_charge(tmp_path):
    path = historical_ledger(tmp_path)
    before = path.read_bytes()
    with pytest.raises(BudgetError, match="Explicit user approval"):
        approve_encumbrance(path, ENCUMBRANCE_ID)
    with pytest.raises(BudgetError):
        Budget(path)
    assert path.read_bytes() == before
    assert approve_encumbrance(path, ENCUMBRANCE_ID, approved=True) is True
    after = path.read_bytes()
    assert after.startswith(before)
    assert approve_encumbrance(path, ENCUMBRANCE_ID, approved=True) is False
    assert path.read_bytes() == after
    entries = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(entries) == 4 and entries[-1]["event"] == "encumbered_unverified"
    assert entries[-1]["episode_id"] == ENCUMBRANCE_EPISODE_ID
    assert entries[-1]["request_id"] == ENCUMBRANCE_REQUEST_ID
    assert "cost" not in entries[-1]
    budget = Budget(path)
    assert not budget.pending() and set(budget.encumbered()) == {ENCUMBRANCE_ID}
    assert budget.reported_spend() == Decimal(".000950")
    assert budget.encumbered_exposure() == ENCUMBRANCE_AMOUNT
    assert budget.exposure() == Decimal(".049900")
    assert budget.pilot_cap - budget.exposure("pilot") == Decimal(".700100")
    assert budget.total_cap - budget.exposure() == Decimal("8.950100")


def test_encumbered_allowance_and_new_reservations_are_not_double_counted(tmp_path):
    budget = Budget(historical_ledger(tmp_path, approved=True))
    rid = budget.reserve(.1, "v4-new-request")
    assert budget.exposure() == Decimal(".149900")
    budget.settle(rid, .02)
    assert budget.reported_spend() == Decimal(".020950")
    assert budget.encumbered_exposure() == Decimal(".048950")
    assert budget.exposure() == Decimal(".069900")


def test_new_unknown_request_blocks_again_and_cannot_be_encumbered(tmp_path):
    path = historical_ledger(tmp_path, approved=True)
    budget = Budget(path)
    new_id = budget.reserve(.02, "new-failed-request")
    with pytest.raises(BudgetError):
        budget.require_available(.001)
    with pytest.raises(BudgetError, match="Unsettled"):
        Budget(path)
    before = path.read_bytes()
    with pytest.raises(BudgetError, match="designated historical"):
        approve_encumbrance(path, new_id, approved=True)
    assert path.read_bytes() == before
    assert budget.exposure() == Decimal(".069900")


@pytest.mark.parametrize("stage,remaining,allowance", [("pilot", 9, ".700101"),
    ("main", 9, "8.950101"), ("pilot", ".01", ".010001")])
def test_encumbrance_counts_against_every_cap(tmp_path, stage, remaining, allowance):
    budget = Budget(historical_ledger(tmp_path, approved=True), stage=stage, remaining=remaining)
    before = list(budget.entries)
    with pytest.raises(BudgetError):
        budget.reserve(allowance, "over-cap")
    assert budget.entries == before


@pytest.mark.parametrize("verified_cost", ["0", ".01"])
def test_encumbrance_cannot_be_released_without_external_evidence(tmp_path, verified_cost):
    path = historical_ledger(tmp_path, approved=True)
    budget = Budget(path)
    before = path.read_bytes()
    with pytest.raises(BudgetError, match="Externally verified"):
        budget.settle(ENCUMBRANCE_ID, 0)
    assert path.read_bytes() == before
    # A test evidence reference represents an externally verified fixture charge;
    # no real ledger settlement or provider request is performed by this test.
    budget.settle(ENCUMBRANCE_ID, verified_cost, external_evidence="offline-test-only: verified billing fixture")
    assert not budget.encumbered() and budget.reported_spend() == Decimal(".000950") + Decimal(verified_cost)
    assert budget.exposure() == Decimal(".000950") + Decimal(verified_cost)
    assert Budget(path).exposure() == budget.exposure()
    with pytest.raises(BudgetError, match="Duplicate"):
        budget.settle(ENCUMBRANCE_ID, 0, external_evidence="offline-test-only: fixture")


@pytest.mark.parametrize("corruption", ["duplicate", "amount", "episode", "request", "settle_without_evidence", "unknown_event"])
def test_encumbrance_ledger_replay_rejects_invalid_transitions(tmp_path, corruption):
    path = historical_ledger(tmp_path, approved=True)
    events = [json.loads(line) for line in path.read_text().splitlines()]
    if corruption == "duplicate":
        events.append(deepcopy(events[-1]))
    elif corruption == "amount":
        events[-1]["allowance"] = "0"
    elif corruption == "episode":
        events[-1]["episode_id"] = "wrong-episode"
    elif corruption == "request":
        events[-1]["request_id"] = "wrong-request"
    elif corruption == "settle_without_evidence":
        events.append({"event": "settle", "id": ENCUMBRANCE_ID, "stage": "pilot", "cost": "0"})
    else:
        events.append({"event": "release", "id": ENCUMBRANCE_ID, "stage": "pilot"})
    with pytest.raises(BudgetError):
        replay_ledger(events)


def test_stage_a_b_gates_accept_authorized_encumbrance_but_reject_new_pending(pilot, tmp_path):
    budget = Budget(historical_ledger(tmp_path, approved=True))
    a = pilot_rows(pilot, budget=budget)
    assert validate_pilot_stage(a, pilot, "A", budget)["passed"]
    assert all(r["cumulative_encumbered_unverified_usd"] == .048950 for r in a)
    assert budget.encumbered_exposure() == ENCUMBRANCE_AMOUNT
    b = pilot_rows(pilot, "B", budget)
    assert validate_pilot_stage(b, pilot, "B", budget)["passed"]
    budget.reserve(.02, "new-unknown-request")
    assert not validate_pilot_stage(a, pilot, "A", budget)["passed"]
    assert not validate_pilot_stage(b, pilot, "B", budget)["passed"]


def test_api_error_with_old_encumbrance_stops_and_retains_new_hold(tmp_path):
    budget = Budget(historical_ledger(tmp_path, approved=True))
    row = episode(transport=Script([RuntimeError("offline network error")]), dry_run=False,
                  pricing=PRICES, budget=budget)
    assert row["termination_reason"] == "api_error" and row["model_calls"] == 1
    assert len(budget.pending()) == 1 and len(budget.encumbered()) == 1
    assert budget.reported_spend() == Decimal(".000950")
    with pytest.raises(BudgetError):
        budget.reserve(.001, "no-retry")


def test_encumbrance_cli_requires_explicit_flag_and_never_combines_with_paid(tmp_path, monkeypatch):
    import competitive_prompting.runner as runner
    path = historical_ledger(tmp_path)
    monkeypatch.setattr(runner, "approve_encumbrance", lambda _, rid, **kw: approve_encumbrance(path, rid, **kw))
    monkeypatch.setattr(runner, "Budget", lambda _: Budget(path))
    before = path.read_bytes()
    for args in (["--encumber-reservation", ENCUMBRANCE_ID], ["--approve-unverified-encumbrance"],
                 ["--encumber-reservation", ENCUMBRANCE_ID, "--approve-unverified-encumbrance", "--execute"]):
        with pytest.raises(SystemExit):
            main(args)
    assert path.read_bytes() == before
    main(["--encumber-reservation", ENCUMBRANCE_ID, "--approve-unverified-encumbrance"])
    assert Budget(path).encumbered_exposure() == ENCUMBRANCE_AMOUNT
