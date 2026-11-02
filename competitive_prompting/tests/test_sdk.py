"""Exercise the installed official SDK on an in-memory HTTP transport only."""
import json
from copy import deepcopy
from pathlib import Path

import httpx2 as httpx
import pytest
from openai import OpenAI
from openai.types.responses.response_create_params import ResponseCreateParamsNonStreaming
from openai.types.responses.response_function_tool_call_param import ResponseFunctionToolCallParam
from pydantic import TypeAdapter, ValidationError

from competitive_prompting.budget import Budget, BudgetError, RunLock
from competitive_prompting.runner import (LiveResponses, Limits, ROOT, build_pilot_manifest, build_request,
    main, run_episode, serialize_input_items)
from competitive_prompting.tasks import BY_ID

RECORDED = json.loads((Path(__file__).parent / "fixtures" / "recorded_first_response.json").read_text())
REQUEST_SCHEMA = TypeAdapter(ResponseCreateParamsNonStreaming)


def assert_api_input(request):
    # The SDK's TypedDict validators otherwise ignore unknown keys. Requiring an
    # exact roundtrip also detects fields silently dropped by the official schema.
    validated = REQUEST_SCHEMA.validate_python(request, strict=True)
    assert json.loads(REQUEST_SCHEMA.dump_json(validated)) == request
    assert '"async_"' not in json.dumps(request["input"])
    calls = [i["call_id"] for i in request["input"] if i.get("type") == "function_call"]
    results = [i["call_id"] for i in request["input"] if i.get("type") == "function_call_output"]
    assert len(set(calls)) == len(calls) and len(set(results)) == len(results)
    assert calls == results


def test_responses_sdk_roundtrip_and_no_http_retries(tmp_path):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(429, json={"error": {"message": "Offline rate limit fixture",
                                                 "type": "rate_limit_error", "code": "rate_limit_exceeded"}})

    client = OpenAI(api_key="offline-test-placeholder", max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    class SDKTransport:
        def create(self, **request):
            return client.responses.create(**request).model_dump(mode="json")

    try:
        row = run_episode(BY_ID["stopping_done"], "A", "gpt-6-luna", 0, 7,
                          SDKTransport(), dry_run=False,
                          pricing={"input": .1, "cached_input": .01, "cache_write": .125, "output": .5},
                          budget=Budget(tmp_path / "spend.jsonl"))
    finally:
        client.close()
    assert len(requests) == 1
    assert row["termination_reason"] == "api_error"
    assert row["api_errors"][0]["type"] == "RateLimitError"
    assert row["api_errors"][0]["status_code"] == 429
    assert requests[0]["reasoning"] == {"effort": "low", "mode": "standard"}
    assert requests[0]["text"]["format"]["strict"] is True
    assert requests[0]["tools"][0]["type"] == "function"
    assert requests[0]["parallel_tool_calls"] is False
    assert requests[0]["max_output_tokens"] == 2048
    assert "offline-test-placeholder" not in json.dumps(row)


def test_sdk_reports_cache_write_and_reasoning_usage():
    payload = {"id": "resp_offline", "object": "response", "created_at": 0,
               "model": "gpt-6-luna", "status": "completed", "output": [],
               "usage": {"input_tokens": 100, "output_tokens": 30, "total_tokens": 130,
                         "input_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 10},
                         "output_tokens_details": {"reasoning_tokens": 15}}}
    with OpenAI(api_key="offline-test-placeholder", max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(
                    lambda _: httpx.Response(200, json=payload)))) as client:
        response = client.responses.create(model="gpt-6-luna", input="offline fixture")
    assert response.usage.input_tokens_details.cache_write_tokens == 10
    assert response.usage.output_tokens_details.reasoning_tokens == 15


def test_real_failure_fixture_next_request_is_api_compatible():
    task = BY_ID["execution_invoice"]
    call = deepcopy(RECORDED["output"][0])
    result = {"type": "function_call_output", "call_id": call["call_id"],
              "output": '{"ok":true,"records":{}}', "request_id": "response-only", "status": None}
    history = [{"role": "user", "content": "fixture"}, call, result]
    invalid = build_request(task, "B", "gpt-6.1-sol", Limits())
    invalid["input"] = history
    with pytest.raises((ValidationError, AssertionError)):
        assert_api_input(invalid)
    properties = TypeAdapter(ResponseFunctionToolCallParam).json_schema()["properties"]
    assert "async" in properties and "async_" not in properties
    fixed = build_request(task, "B", "gpt-6.1-sol", Limits(), history=history)
    assert_api_input(fixed)
    assert fixed["input"][1] == {"type": "function_call", "call_id": "call_recorded_read",
        "arguments": "{}", "name": "read_records", "id": "fc_recorded_read", "status": "completed"}
    assert fixed["input"][2] == {k: result[k] for k in ("type", "call_id", "output")}
    assert fixed["store"] is False


@pytest.mark.parametrize("asynchronous", [None, False, True])
@pytest.mark.parametrize("caller", [None, {"type": "direct"}, {"type": "program", "caller_id": "program_test"}])
def test_null_optional_metadata_and_supported_aliases(asynchronous, caller):
    call = {**RECORDED["output"][0], "async_": asynchronous, "caller": caller,
            "namespace": "workflow" if caller else None, "created_at": 999, "request_id": "ignored"}
    history = [call, {"type": "function_call_output", "call_id": call["call_id"], "output": "{}"}]
    request = build_request(BY_ID["execution_invoice"], "B", "gpt-6.1-sol", Limits(), history=history)
    assert_api_input(request)
    saved = request["input"][0]
    assert ("async" in saved) == (asynchronous is not None)
    if asynchronous is not None:
        assert saved["async"] is asynchronous
    assert saved.get("caller") == caller
    assert "created_at" not in saved and "request_id" not in saved
    assert all(value is not None for value in saved.values())


def test_multiple_calls_reasoning_content_and_message_phase_are_preserved():
    reasoning = {"type": "reasoning", "id": "rs_test", "encrypted_content": "opaque-test-content",
        "summary": [{"type": "summary_text", "text": "summary", "internal": None}],
        "content": [{"type": "reasoning_text", "text": "reasoning fixture", "internal": 1}],
        "status": "completed", "usage": {}, "async_": None}
    one = RECORDED["output"][0]
    two = {**one, "id": "fc_two", "call_id": "call_two", "name": "check_requirements",
           "arguments": '{"record_id":"I17"}'}
    output = lambda call: {"type": "function_call_output", "call_id": call["call_id"], "output": "{}"}
    history = [reasoning, one, two, output(one), output(two),
        {"type": "message", "role": "assistant", "id": "msg_test", "status": "completed", "phase": "commentary",
         "content": [{"type": "output_text", "text": "Continuing.", "annotations": [], "logprobs": None}]},
        {**reasoning, "id": "rs_two", "content": None, "encrypted_content": None}]
    request = build_request(BY_ID["execution_invoice"], "B", "gpt-6.1-sol", Limits(), history=history)
    assert_api_input(request)
    items = request["input"]
    assert [i.get("type", "message") for i in items] == ["reasoning", "function_call", "function_call",
        "function_call_output", "function_call_output", "message", "reasoning"]
    assert items[0]["encrypted_content"] == reasoning["encrypted_content"]
    assert items[0]["summary"] == [{"type": "summary_text", "text": "summary"}]
    assert items[0]["content"] == [{"type": "reasoning_text", "text": "reasoning fixture"}]
    assert items[-2] == {"role": "assistant", "phase": "commentary", "content": "Continuing."}
    assert "content" not in items[-1] and "encrypted_content" not in items[-1]


@pytest.mark.parametrize("problem", ["missing", "duplicate_call", "duplicate_result", "orphan_result", "unknown_item"])
def test_invalid_replay_chains_are_rejected_before_request(problem):
    call = deepcopy(RECORDED["output"][0])
    output = {"type": "function_call_output", "call_id": call["call_id"], "output": "{}"}
    cases = {"missing": [call], "duplicate_call": [call, call, output],
        "duplicate_result": [call, output, output], "orphan_result": [output],
        "unknown_item": [{"type": "unexpected_hosted_tool"}]}
    with pytest.raises(ValueError):
        serialize_input_items(cases[problem])


def test_full_sdk_mock_replays_recorded_response_and_consecutive_calls(tmp_path):
    requests = []

    def call(name, arguments, index):
        return {"type": "function_call", "name": name, "arguments": json.dumps(arguments),
                "id": f"fc_test_{index}", "call_id": f"call_test_{index}", "async_": None,
                "caller": None, "namespace": None, "status": "completed"}

    outputs = [RECORDED["output"],
        [{"type": "reasoning", "id": "rs_roundtrip", "summary": [], "encrypted_content": "opaque-fixture"},
         call("check_requirements", {"record_id": "I17"}, 2)],
        [call("apply_operation", {"record_id": "I17", "operation": "approve"}, 3)],
        [call("apply_operation", {"record_id": "I17", "operation": "pay"}, 4)],
        [call("verify_completion", {}, 5)],
        [{"type": "message", "id": "msg_done", "role": "assistant", "status": "completed", "phase": "final_answer",
          "content": [{"type": "output_text", "text": '{"status":"completed","reason":"Verified."}', "annotations": []}]}]]

    def handler(request):
        body = json.loads(request.content)
        assert_api_input(body)
        requests.append(body)
        payload = {**deepcopy(RECORDED), "object": "response", "created_at": 0,
                   "id": f"resp_test_{len(requests)}", "output": outputs[len(requests) - 1]}
        return httpx.Response(200, json=payload, headers={"x-request-id": f"req_mock_{len(requests)}"})

    live = LiveResponses.__new__(LiveResponses)
    live.client = OpenAI(api_key="offline-placeholder", max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    prices = {"input": 2, "cached_input": .1, "cache_write": 2.5, "output": 10}
    budget = Budget(tmp_path / "spend.jsonl")
    try:
        row = run_episode(BY_ID["execution_invoice"], "B", "gpt-6.1-sol", 0, 20261027,
                          live, dry_run=False, budget=budget, pricing=prices)
    finally:
        live.close()
    assert row["verified_task_success"] is True
    assert row["model_calls"] == 6 and row["tool_calls"] == 5
    assert not row["api_errors"] and not budget.pending()
    assert row["input_tokens"] == 6 * 405 and row["output_tokens"] == 6 * 14
    assert row["total_tokens"] == 6 * 419 and row["reasoning_tokens"] == 0
    assert any(i.get("encrypted_content") == "opaque-fixture" for i in requests[-1]["input"])
    assert row["interactions"][0]["response"]["request_id"] == "req_mock_1"


def test_sdk_error_fields_are_recorded_with_context_and_redacted(tmp_path, monkeypatch):
    secret = "sk-offline-test-secret"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    monkeypatch.setenv("UNRELATED_SECRET", "do-not-log-this")
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(200, json={**RECORDED, "object": "response", "created_at": 0})
        return httpx.Response(400, headers={"x-request-id": "req_rejected_fixture"}, json={"error": {
            "message": f"Unknown parameter input[1].async_. Authorization: Bearer {secret}",
            "type": "invalid_request_error", "code": "unknown_parameter", "param": "input[1].async_",
            "debug": "do-not-log-this"}})

    live = LiveResponses.__new__(LiveResponses)
    live.client = OpenAI(api_key=secret, max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    budget = Budget(tmp_path / "spend.jsonl")
    try:
        row = run_episode(BY_ID["execution_invoice"], "B", "gpt-6.1-sol", 0, 20261027, live,
            dry_run=False, budget=budget, pricing={"input": 2, "cached_input": .1, "cache_write": 2.5, "output": 10},
            episode_id="offline-error-episode")
    finally:
        live.close()
    assert len(requests) == 2 and row["termination_reason"] == "api_error"
    error = row["api_errors"][0]
    assert error["status_code"] == 400 and error["code"] == "unknown_parameter"
    assert error["param"] == "input[1].async_" and error["request_id"] == "req_rejected_fixture"
    assert error["episode_id"] == "offline-error-episode" and error["request_sequence"] == 2
    assert "Unknown parameter" in error["message"]
    assert secret not in json.dumps(row) and "do-not-log-this" not in json.dumps(row)
    assert "Authorization: Bearer" not in json.dumps(row)
    assert row["known_api_cost_usd"] == .00095 and budget.pending()


def test_unresolved_historical_charge_blocks_new_v3_before_client_creation(tmp_path, monkeypatch):
    import competitive_prompting.runner as runner
    ledger = tmp_path / "spend.jsonl"
    events = [
        {"event": "reserve", "id": "prior-settled", "stage": "pilot", "episode_id": "old", "allowance": ".04563125"},
        {"event": "settle", "id": "prior-settled", "stage": "pilot", "cost": ".00095"},
        {"event": "reserve", "id": "5996f285d52d467abb3d8e8ecd7a0433", "stage": "pilot", "episode_id": "old", "allowance": ".0489500"}]
    ledger.write_text("".join(json.dumps(e) + "\n" for e in events))
    before = ledger.read_bytes()
    pilot = build_pilot_manifest(json.loads((ROOT / "pricing.json").read_text()))
    path = tmp_path / "pilot_v3.json"
    path.write_text(json.dumps(pilot))
    monkeypatch.setenv("OPENAI_API_KEY", "offline-placeholder")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "proj_offline")
    monkeypatch.setattr(runner, "Budget", lambda _, *a: Budget(ledger, *a))
    monkeypatch.setattr(runner, "RunLock", lambda _: RunLock(ledger))
    class ForbiddenLive:
        def __init__(self, **_):
            pytest.fail("An unresolved charge must block even API client creation")
    monkeypatch.setattr(runner, "LiveResponses", ForbiddenLive)
    with pytest.raises(BudgetError, match="Unsettled"):
        main(["--execute", "--pilot-manifest", str(path), "--pilot-stage", "A", "--output-dir", str(tmp_path / "run")])
    assert ledger.read_bytes() == before
    assert sum(float(e["cost"]) for e in events if e["event"] == "settle") == .00095
