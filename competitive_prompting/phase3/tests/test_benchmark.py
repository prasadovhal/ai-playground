from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path

import pytest

from competitive_prompting import runner as original
from competitive_prompting.budget import Budget, BudgetError, ENCUMBRANCE_ID, ENCUMBRANCE_AMOUNT
from competitive_prompting.simulator import Environment, TOOLS
from competitive_prompting.prompts import CONDITIONS, SYSTEM
from competitive_prompting.phase3.tasks import ALL, DEVELOPMENT, HELDOUT, BY_ID
from competitive_prompting.phase3.evaluation import oracle, evaluate, initial_state, OracleResponses
from competitive_prompting.phase3.preparation import (LIMITS, forecasts, build_manifest, load_manifest, ROOT)
from competitive_prompting.phase3.runner import Phase3Budget, audit_row, run_stage, check_development_release, infrastructure_complete
from competitive_prompting.phase3.analysis import paired, task_interval, write_reports
from competitive_prompting.tests.test_experiment import historical_ledger


@pytest.mark.parametrize("case", ALL, ids=lambda c: c.task.id)
def test_oracle_gold_faults_reset_and_determinism(case):
    first, second = oracle(case), oracle(case)
    assert first == second
    assert first["evaluation"]["verified_task_success"]
    assert first["evaluation"]["faults_exposed"] == len(case.task.failures)
    assert first["evaluation"]["uninspected_write_retries"] == 0
    assert len(first["actions"]) < LIMITS.tool_calls
    env = Environment(case.task)
    assert env.state == initial_state(case)
    first_record = case.task.records[0]
    env.state[first_record.id]["counts"][first_record.steps[0]] = 999
    assert Environment(case.task).state == initial_state(case)
    # Explicit gold is independent: changing it cannot change simulator responses.
    altered = deepcopy(case)
    rid, counts = altered.expected[0]
    object.__setattr__(altered, "expected", ((rid, (9,) + counts[1:]),) + altered.expected[1:])
    assert not evaluate(altered, second["trace"], second["final_state"], second["final_text"], "completed")["verified_task_success"]


@pytest.mark.parametrize("case", ALL, ids=lambda c: c.task.id)
def test_every_task_rejects_early_or_false_completion(case):
    env = Environment(case.task)
    env.call("verify_completion", {})
    for status in ("completed", "blocked"):
        final = json.dumps({"status": status, "reason": "premature"})
        assert not evaluate(case, env.trace, env.state, final, "completed")["verified_task_success"]


@pytest.mark.parametrize("case", ALL, ids=lambda c: c.task.id)
def test_every_task_rejects_protected_record_write(case):
    env = Environment(case.task)
    env.call("read_records", {})  # Some recovery tasks transiently fail; retry reads.
    while not env.observed:
        env.call("read_records", {})
    protected = next(r for r in case.task.records if not r.requirements_met or not r.authorized or not r.available)
    env.call("check_requirements", {"record_id": protected.id})
    env.call("apply_operation", {"record_id": protected.id, "operation": protected.steps[0]})
    env.call("verify_completion", {})
    final = json.dumps({"status": case.task.expected_status, "reason": "unsafe action"})
    score = evaluate(case, env.trace, env.state, final, "completed")
    assert score["constraint_violations"] >= 1 and not score["verified_task_success"]


@pytest.mark.parametrize("case", ALL, ids=lambda c: c.task.id)
def test_every_task_rejects_duplicate_after_oracle_completion(case):
    result = oracle(case)
    env = Environment(case.task)
    for action in result["actions"]:
        env.call(action["tool"], action["arguments"])
    allowed = next(r for r in case.task.records if r.authorized and r.available and r.requirements_met)
    env.call("apply_operation", {"record_id": allowed.id, "operation": allowed.steps[-1]})
    env.call("verify_completion", {})
    score = evaluate(case, env.trace, env.state, result["final_text"], "completed")
    assert score["duplicate_operations"] == 1 and not score["verified_task_success"]


def test_collection_separation_and_no_ground_truth_in_public_responses():
    assert len(DEVELOPMENT) == 6 and len(HELDOUT) == 24 and len(BY_ID) == 30
    assert not {c.task.id for c in DEVELOPMENT} & {c.task.id for c in HELDOUT}
    for cases, expected in ((DEVELOPMENT, 2), (HELDOUT, 8)):
        for category in ("execution", "recovery", "stopping"):
            assert sum(c.task.category == category for c in cases) == expected

    def keys(value):
        if isinstance(value, dict):
            return set(value) | set().union(*(keys(v) for v in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(v) for v in value)) if value else set()
        return set()
    for case in ALL:
        for record in case.task.records:
            assert set(record.counts) <= {0, 1}
            assert list(record.counts) == sorted(record.counts, reverse=True)
        assert case.rationale not in case.task.instruction
        for event in oracle(case)["trace"]:
            assert not keys(event["response"]) & {"expected", "expected_status", "failures", "hidden_failure", "score", "oracle", "evaluation"}


@pytest.mark.parametrize("case", ALL, ids=lambda c: c.task.id)
def test_dependencies_cannot_be_skipped(case):
    env = Environment(case.task)
    for _ in range(8):
        if env.call("read_records", {})["ok"]:
            break
    record = next(r for r in case.task.records if r.authorized and r.available and r.requirements_met)
    for _ in range(8):
        if env.call("check_requirements", {"record_id": record.id})["ok"]:
            break
    before = deepcopy(env.state)
    result = env.call("apply_operation", {"record_id": record.id, "operation": record.steps[-1]})
    assert not result["ok"] and env.state == before
    score = evaluate(case, env.trace, env.state, '{"status":"completed","reason":"skipped steps"}', "completed")
    assert score["constraint_violations"] == 1 and not score["verified_task_success"]


def test_oracle_does_not_use_simulator_scoring(monkeypatch):
    def forbidden(*_):
        raise AssertionError("Independent oracle/checker must not use simulator labels")
    monkeypatch.setattr(Environment, "evaluate", forbidden)
    monkeypatch.setattr(Environment, "expected_state", forbidden)
    assert all(oracle(c)["evaluation"]["verified_task_success"] for c in ALL)


def test_reference_checker_detects_corrupt_transition():
    case = DEVELOPMENT[0]
    result = oracle(case)
    result["trace"][0]["after"][case.task.records[0].id]["counts"][case.task.records[0].steps[0]] = 7
    with pytest.raises(ValueError, match="independent"):
        evaluate(case, result["trace"], result["final_state"], result["final_text"], "completed")


def test_blind_retry_is_a_separate_diagnostic_without_redefining_old_violations():
    case = next(c for c in DEVELOPMENT if c.task.id.endswith("invoice_reconcile"))
    env = Environment(case.task)
    env.call("read_records", {})
    env.call("check_requirements", {"record_id": "C32"})
    args = {"record_id": "C32", "operation": "approve"}
    env.call("apply_operation", args)  # Scheduled pre-commit timeout.
    env.call("apply_operation", args)  # Blind retry; another scheduled timeout.
    env.call("read_records", {})
    env.call("apply_operation", args)
    score = evaluate(case, env.trace, env.state, "", "model_limit")
    assert score["uninspected_write_retries"] == 1
    assert score["constraint_violations"] == 0 and score["duplicate_operations"] == 0


def test_prompt_conditions_share_all_controls_and_oracle_outcomes():
    rows, requests = [], []
    case = DEVELOPMENT[2]
    for condition in CONDITIONS:
        row = original.run_episode(case.task, condition, "gpt-6.1-sol", 0, 20261027,
                                   OracleResponses(case), limits=LIMITS)
        audit_row(case, row)
        assert row["independent_audit_passed"] and row["verified_task_success"]
        request = deepcopy(row["interactions"][0]["request"])
        assert request["input"][0]["content"].startswith(CONDITIONS[condition])
        request["input"] = []
        requests.append(request)
        rows.append(row)
    assert all(r == requests[0] for r in requests)
    assert all(r["action_trace"] == rows[0]["action_trace"] for r in rows)
    assert LIMITS == original.Limits() and requests[0]["instructions"] == SYSTEM


@pytest.fixture(scope="module")
def manifests():
    estimates, hashes = forecasts()
    return {c: build_manifest(c, estimates, hashes) for c in ("development", "heldout")}


def test_full_manifest_and_balanced_stages_are_reproducible(manifests, tmp_path):
    dev, heldout = manifests["development"], manifests["heldout"]
    assert len(dev["plan"]) == 12 and {p["condition"] for p in dev["plan"]} == {"A"}
    assert len(heldout["plan"]) == 384
    assert len({p["episode_id"] for p in heldout["plan"]}) == 384
    for stage in range(1, 5):
        plan = [p for p in heldout["plan"] if p["stage"] == stage]
        assert len(plan) == 96
        for category in ("execution", "recovery", "stopping"):
            assert len({p["task_id"] for p in plan if BY_ID[p["task_id"]].task.category == category}) == 2
        for start in range(0, len(plan), 4):
            block = plan[start:start + 4]
            assert {p["condition"] for p in block} == set(CONDITIONS)
            assert len({(p["model"], p["task_id"], p["repetition"]) for p in block}) == 1
    assert all(p["forecast"]["max_request_bytes"] < LIMITS.request_bytes for p in heldout["plan"])
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(heldout))
    assert load_manifest(path) == heldout
    heldout = deepcopy(heldout)
    heldout["settings"]["limits"]["model_calls"] = 999
    path.write_text(json.dumps(heldout))
    with pytest.raises(ValueError):
        load_manifest(path)


def test_cost_forecast_uses_v4_usage_and_flags_unaffordable_full_plan(manifests):
    assert manifests["development"]["forecast"]["nominal_usd"] < .5
    assert manifests["heldout"]["forecast"]["nominal_usd"] > 6
    assert len(manifests["heldout"]["v4_measurement_sha256"]) == 2
    assert manifests["heldout"]["forecast"]["stressed_usd"] > manifests["heldout"]["forecast"]["nominal_usd"]


def test_explicit_one_rep_alternative_preserves_every_task_model_condition(manifests, tmp_path):
    full = manifests["heldout"]
    # Use frozen full forecasts to avoid recomputing token estimates in this test.
    estimates = {(p["task_id"], p["model"], p["condition"]): p["forecast"] for p in full["plan"]}
    alt = build_manifest("heldout", estimates, full["v4_measurement_sha256"], one_repetition=True)
    assert alt["plan"] == [p for p in full["plan"] if p["repetition"] == 0]
    assert len(alt["plan"]) == 192 and alt["sha256"] != full["sha256"]
    assert alt["tasks"] == full["tasks"] and alt["settings"] == full["settings"]
    assert {p["repetition"] for p in alt["plan"]} == {0}
    assert all(sum(p["stage"] == s for p in alt["plan"]) == 48 for s in range(1, 5))
    path = tmp_path / "alternative.json"
    path.write_text(json.dumps(alt))
    assert load_manifest(path) == alt


def test_stage_gates_keep_agent_limit_failures_separate_from_infrastructure():
    row = {"independent_audit_passed": True, "termination_reason": "model_limit", "model_calls": 24}
    assert infrastructure_complete(row)
    for stop in ("tool_limit", "request_size_limit"):
        assert infrastructure_complete({**row, "termination_reason": stop})
    truncated = {**row, "termination_reason": "incomplete_response", "interactions": [
        {"response": {"incomplete_details": {"reason": "max_output_tokens"}}}]}
    assert infrastructure_complete(truncated)
    for stop in ("api_error", "budget_limit", "evaluator_error", "usage_missing"):
        assert not infrastructure_complete({**row, "termination_reason": stop})


def test_phase3_and_dev_caps_are_cumulative_and_count_global_encumbrance(tmp_path):
    path = historical_ledger(tmp_path, approved=True)
    dev = Phase3Budget(path, "development")
    assert dev.encumbered_exposure() == ENCUMBRANCE_AMOUNT
    with pytest.raises(BudgetError, match="development"):
        dev.reserve(.500001, "phase3:development:hard-v1:test")
    rid = dev.reserve(.5, "phase3:development:hard-v1:test")
    dev.settle(rid, .4)
    with pytest.raises(BudgetError):
        Phase3Budget(path, "development").reserve(.100001, "phase3:development:hard-v2:test")
    main = Phase3Budget(path, "heldout")
    rid = main.reserve(5.6, "phase3:heldout:hard-v1:test")
    main.settle(rid, 5.6)
    assert main.phase_exposure() == Decimal("6")
    assert main.exposure() == Decimal("6.049900")
    with pytest.raises(BudgetError, match="Phase 3"):
        Phase3Budget(path, "heldout").reserve(.001, "phase3:heldout:hard-v2:test")
    with pytest.raises(BudgetError, match="namespace"):
        main.reserve(.001, "untracked-prefix")


def test_global_cap_cannot_be_bypassed_by_new_collection(tmp_path):
    path = historical_ledger(tmp_path, approved=True)
    budget = Budget(path, stage="main")
    rid = budget.reserve(8.5, "unrelated-earlier-experiment")
    budget.settle(rid, 8.5)
    phase = Phase3Budget(path, "heldout")
    assert phase.phase_exposure() == 0
    with pytest.raises(BudgetError):
        phase.reserve(.46, "phase3:heldout:hard-v1:test")


def test_new_unknown_phase3_charge_blocks_restart(tmp_path):
    path = historical_ledger(tmp_path, approved=True)
    budget = Phase3Budget(path, "development")
    budget.reserve(.02, "phase3:development:hard-v1:unknown")
    with pytest.raises(BudgetError, match="Unsettled"):
        Phase3Budget(path, "heldout")
    assert list(budget.encumbered()) == [ENCUMBRANCE_ID]


def test_main_requires_real_development_review_and_default_stage_is_offline(manifests, tmp_path, monkeypatch):
    class Forbidden:
        def __init__(self, **_):
            pytest.fail("Default execution must never construct a live client")
    monkeypatch.setattr(original, "LiveResponses", Forbidden)
    directory = run_stage(manifests["development"], 1, tmp_path)
    rows = [json.loads(l) for l in (directory / "episodes.jsonl").read_text().splitlines()]
    assert len(rows) == 6 and all(r["dry_run"] and r["condition"] == "A" for r in rows)
    assert all(r["independent_audit_passed"] for r in rows)
    with pytest.raises(ValueError, match="review"):
        run_stage(manifests["heldout"], 1, tmp_path, execute=True)
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"decision": "freeze", "heldout_manifest_sha256": manifests["heldout"]["sha256"],
        "reviewer": "offline test", "difficulty_review": "fixture", "development_results": [str(directory / "episodes.jsonl")],
        "development_results_sha256": "incorrect"}))
    with pytest.raises(ValueError, match="hash"):
        check_development_release(approval, manifests["heldout"])


def test_unaffordable_full_plan_blocks_before_api_client(manifests, tmp_path, monkeypatch):
    import competitive_prompting.phase3.runner as runner
    from competitive_prompting.budget import RunLock
    ledger = historical_ledger(tmp_path, approved=True)
    original_budget = Phase3Budget
    monkeypatch.setattr(runner, "Phase3Budget", lambda _, c: original_budget(ledger, c))
    monkeypatch.setattr(runner, "RunLock", lambda _: RunLock(ledger))
    monkeypatch.setattr(runner, "check_development_release", lambda *_: {"decision": "freeze"})
    monkeypatch.setenv("OPENAI_API_KEY", "offline-placeholder")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "proj_offline")
    class Forbidden:
        def __init__(self, **_):
            pytest.fail("An unaffordable plan must not create an API client")
    monkeypatch.setattr(original, "LiveResponses", Forbidden)
    before = ledger.read_bytes()
    with pytest.raises(BudgetError, match="smaller balanced"):
        run_stage(manifests["heldout"], 1, tmp_path, execute=True, release="reviewed-placeholder")
    assert ledger.read_bytes() == before


def comparison_rows():
    result = []
    for case in HELDOUT:
        for repetition in range(2):
            for condition in CONDITIONS:
                result.append({"task_id": case.task.id, "category": case.task.category, "model": "gpt-6-luna",
                    "repetition": repetition, "seed": 20261027, "condition": condition, "model_calls": 1,
                    "verified_task_success": condition == "C", "termination_reason": "completed",
                    "collection": "heldout", "dry_run": False, "source_sha256": "same", "benchmark_manifest_sha256": "same",
                    "tool_calls": 2, "unnecessary_tool_calls": 0, "constraint_violations": 0, "retries": 0,
                    "correct_stopping": False, "false_completion_claim": False, "known_api_cost_usd": .01,
                    "estimated_api_cost_usd": .01})
    return result


def test_analysis_clusters_repeats_and_never_mixes_development(tmp_path):
    rows = comparison_rows()
    primary = next(r for r in paired(rows) if r["category"] == "all" and
                   r["comparison"] == "primary_C_minus_B" and r["metric"] == "operational_task_success")
    assert primary["paired_tasks"] == 24 and primary["paired_repeated_blocks"] == 48
    assert primary["mean_task_paired_difference"] == primary["ci_low"] == primary["ci_high"] == 1
    assert task_interval({c.task.id: 0 for c in HELDOUT[:4]}) == (None, None)
    dev = {**rows[0], "collection": "development", "task_id": DEVELOPMENT[0].task.id}
    write_reports(rows + [dev], tmp_path)
    assert DEVELOPMENT[0].task.id not in (tmp_path / "task_summary.csv").read_text()
    with pytest.raises(ValueError, match="Duplicate"):
        paired(rows + [rows[0]])
