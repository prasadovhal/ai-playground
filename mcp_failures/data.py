"""Isolated, fault-injected MCP task ledger and reproducible event capture.

These are real executions of this local SQLite task simulator, not observations
from a remote MCP server or Kaggle. Each scenario points to its captured event.
"""
from __future__ import annotations

import json
import csv
import hashlib
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
TASKS_EXTENSION = "io.modelcontextprotocol/tasks"
PROTOCOL_VERSION = "2026-07-28"
ACTIONS = ["POLL_EXISTING_TASK", "RETRY_ORIGINAL_REQUEST", "CHECK_PERSISTED_STATE",
           "CANCEL_TASK", "REQUEST_HUMAN_INTERVENTION", "REPORT_COMPLETION",
           "REPORT_FAILURE", "WAIT_FOR_UPDATE"]


def validate_instacart_data():
    """Validate the four real Kaggle CSVs without exposing local credentials."""
    expected = {
        "orders.csv": ["order_id", "user_id", "eval_set", "order_number", "order_dow",
                       "order_hour_of_day", "days_since_prior_order"],
        "order_products__prior.csv": ["order_id", "product_id", "add_to_cart_order", "reordered"],
        "products.csv": ["product_id", "product_name", "aisle_id", "department_id"],
        "departments.csv": ["department_id", "department"],
    }
    summary = {}
    for name, columns in expected.items():
        path = ROOT / "data" / name
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        with path.open(newline="", encoding="utf-8") as source:
            reader = csv.reader(source)
            actual = next(reader)
            count = bad = 0
            for row in reader:
                count += 1
                bad += len(row) != len(actual)
        assert actual == columns and bad == 0, (name, actual, bad)
        summary[name] = {"rows": count, "columns": actual, "malformed_rows": bad,
                         "bytes": path.stat().st_size, "sha256": digest.hexdigest()}
    departments = set()
    with (ROOT / "data" / "departments.csv").open(newline="", encoding="utf-8") as source:
        for row in csv.DictReader(source):
            departments.add(int(row["department_id"]))
    products = set()
    with (ROOT / "data" / "products.csv").open(newline="", encoding="utf-8") as source:
        for row in csv.DictReader(source):
            assert int(row["department_id"]) in departments
            products.add(int(row["product_id"]))
    # orders.csv is grouped by user, while prior products are grouped by order.
    # An order-ID indexed byte array checks every reference using about 4 MB.
    order_state = bytearray(4_000_000)
    with (ROOT / "data" / "orders.csv").open(newline="", encoding="utf-8") as source:
        for item in csv.DictReader(source):
            order_id = int(item["order_id"])
            if order_id >= len(order_state):
                order_state.extend(b"\x00" * (order_id - len(order_state) + 1))
            assert order_state[order_id] == 0, "Duplicate order ID"
            order_state[order_id] = 1 if item["eval_set"] == "prior" else 2
    prior_orders = missing_orders = invalid_products = 0
    with (ROOT / "data" / "order_products__prior.csv").open(newline="", encoding="utf-8") as prior_file:
        last_prior = -1
        for item in csv.DictReader(prior_file):
            order_id = int(item["order_id"])
            product_id = int(item["product_id"])
            invalid_products += product_id not in products
            if order_id != last_prior:
                assert order_id > last_prior, "Prior rows not sorted by order ID"
                prior_orders += 1
                missing_orders += order_id >= len(order_state) or order_state[order_id] != 1
                last_prior = order_id
    assert missing_orders == invalid_products == 0, (missing_orders, invalid_products, prior_orders)
    summary["referential_integrity"] = {"prior_orders": prior_orders,
        "missing_or_nonprior_orders": missing_orders, "invalid_product_ids": invalid_products,
        "products_with_valid_departments": len(products)}
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "instacart_data_validation.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def reference_basket(order_limit):
    """Independent Python reference for the C# server's real CSV calculation."""
    frequency, pairs = {}, {}
    orders = rows = 0
    current = None
    products = []

    def flush():
        sorted_products = sorted(products)
        for i, product in enumerate(sorted_products):
            for other in sorted_products[i + 1:]:
                key = (product, other)
                pairs[key] = pairs.get(key, 0) + 1

    with (ROOT / "data" / "order_products__prior.csv").open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        for item in reader:
            order = int(item["order_id"])
            if order != current:
                if current is not None:
                    flush()
                if orders == order_limit:
                    break
                current = order
                orders += 1
                products = []
            product = int(item["product_id"])
            products.append(product)
            frequency[product] = frequency.get(product, 0) + 1
            rows += 1
        else:
            if products:
                flush()
    lines = ["kind,product_id,other_product_id,count\n"]
    lines.extend(f"frequency,{key},0,{frequency[key]}\n" for key in sorted(frequency))
    lines.extend(f"pair,{a},{b},{pairs[a, b]}\n" for a, b in sorted(pairs))
    return {"orders": orders, "rows": rows,
            "sha256": hashlib.sha256("".join(lines).encode("utf-8")).hexdigest(),
            "output_bytes": "".join(lines).encode("utf-8")}


class InstacartMcpClient:
    """Direct JSON-RPC client for the official SDK's HTTP Tasks extension."""

    def __init__(self, run_id, url="http://127.0.0.1:5160/mcp"):
        import requests
        self.session = requests.Session()
        self.run_id = run_id
        self.url = url
        self.sequence = 0

    def close(self):
        self.session.close()

    def send(self, method, params=None, task=False, timeout=30):
        self.sequence += 1
        body = {"jsonrpc": "2.0", "id": self.sequence, "method": method,
                "params": dict(params or {})}
        body["params"]["_meta"] = {
            "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
            "io.modelcontextprotocol/clientCapabilities": {
                "extensions": {TASKS_EXTENSION: {}} if task else {}}}
        headers = {"Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": PROTOCOL_VERSION, "Mcp-Method": method}
        if method == "tools/call":
            headers["Mcp-Name"] = body["params"]["name"]
        elif method.startswith("tasks/"):
            headers["Mcp-Name"] = body["params"]["taskId"]
        sent = datetime.now(timezone.utc).isoformat()
        try:
            response = self.session.post(self.url, json=body, headers=headers, timeout=timeout)
        except Exception as error:
            with (RESULTS / "real_mcp_traces.jsonl").open("a", encoding="utf-8") as output:
                output.write(json.dumps({"run_id": self.run_id,
                    "provenance": "REAL_MCP_INSTACART_WORKLOAD", "sent_at_utc": sent,
                    "request": body, "client_exception": type(error).__name__},
                    sort_keys=True) + "\n")
            raise
        received = datetime.now(timezone.utc).isoformat()
        raw = response.text
        if response.headers.get("Content-Type", "").startswith("text/event-stream"):
            messages = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: ")]
            reply = next((item for item in messages if item.get("id") == self.sequence), None)
        else:
            reply = json.loads(raw)
        with (RESULTS / "real_mcp_traces.jsonl").open("a", encoding="utf-8") as output:
            output.write(json.dumps({"run_id": self.run_id,
                "provenance": "REAL_MCP_INSTACART_WORKLOAD", "sent_at_utc": sent,
                "received_at_utc": received, "request": body, "response": reply,
                "http_status": response.status_code}, sort_keys=True) + "\n")
        assert response.status_code == 200 and reply and "error" not in reply, reply
        return reply["result"]


def run_instacart_smoke(order_limit=20, delay_ms=1000):
    """One actual Tasks computation, independently checked against the CSV."""
    run_id = "instacart-" + uuid.uuid4().hex[:12]
    client = InstacartMcpClient(run_id)
    try:
        discovered = client.send("server/discover", task=True)
        assert TASKS_EXTENSION in discovered["capabilities"]["extensions"]
        tools = client.send("tools/list", task=True)
        assert "instacart_basket" in {tool["name"] for tool in tools["tools"]}
        created = client.send("tools/call", {"name": "instacart_basket",
            "arguments": {"orderLimit": order_limit, "requestKey": run_id,
                          "delayMs": delay_ms, "idempotent": False}}, task=True)
        assert created["resultType"] == "task", created
        task_id = created["taskId"]
        client.close()
        client = InstacartMcpClient(run_id)
        for _ in range(200):
            state = client.send("tasks/get", {"taskId": task_id}, task=True)
            if state["status"] != "working":
                break
            time.sleep(0.05)
        assert state["status"] == "completed", state
        result = json.loads(state["result"]["content"][0]["text"])
        reference = reference_basket(order_limit)
        output = Path(result["output"])
        assert output.is_file() and output.read_bytes() == reference["output_bytes"]
        assert result["checksum"] == reference["sha256"]
        assert result["rows"] == reference["rows"]
        return {"run_id": run_id, "task_id": task_id,
                "orders": result["orders"], "rows": result["rows"],
                "checksum": result["checksum"], "output": str(output),
                "task_status": state["status"], "reference_match": True}
    finally:
        client.close()


def run_instacart_trial(configuration, fault, repetition, order_limit=20):
    """Exercise one real MCP workload and verify its server ledger and artifacts."""
    import concurrent.futures

    assert configuration in "ABCD"
    assert fault in {"F0", "F1", "F2", "F3", "F6", "F7", "F8", "F9", "F10", "F11"}
    task_mode = configuration in "BC"
    idempotent = configuration in "CD"
    if fault in {"F2", "F8", "F9", "F10", "F11"} and not task_mode:
        raise ValueError("Fault requires a task handle or polling")
    trial_id = f"work-{configuration}-{fault}-{repetition:02d}-{uuid.uuid4().hex[:8]}"
    key = trial_id
    delay = 500 if fault in {"F8", "F9"} else 120
    if fault == "F11":
        delay = 150
    polls = retries = 0
    handles = []
    returned = []
    started = time.monotonic()
    client = InstacartMcpClient(trial_id)

    def submit(current_client, timeout=15):
        return current_client.send("tools/call", {"name": "instacart_basket",
            "arguments": {"orderLimit": order_limit, "requestKey": key,
                          "delayMs": delay, "idempotent": idempotent}}, task=task_mode,
            timeout=timeout)

    def collect(result):
        if result["resultType"] == "task":
            handles.append(result["taskId"])
        else:
            returned.append(json.loads(result["content"][0]["text"]))

    try:
        if fault == "F2":
            import subprocess
            import sys
            stage_path = ROOT / "data" / (trial_id + "-stage.json")
            code = ("import sys; from data import client_create_stage; "
                    "client_create_stage(sys.argv[1],sys.argv[2],sys.argv[3]=='1',"
                    "int(sys.argv[4]),int(sys.argv[5]),sys.argv[6])")
            child = subprocess.run([sys.executable, "-c", code, trial_id, key,
                "1" if idempotent else "0", str(order_limit), str(delay), str(stage_path)],
                cwd=ROOT, capture_output=True, text=True, timeout=20)
            assert child.returncode == 0, child.stderr[:500]
            stage = json.loads(stage_path.read_text(encoding="utf-8"))
            stage_path.unlink()
            handles.append(stage["task_id"])
            # The creating Python process has exited; this controller is a new client.
        elif fault == "F7":
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                def parallel(_):
                    c = InstacartMcpClient(trial_id)
                    try:
                        return submit(c)
                    finally:
                        c.close()
                for result in pool.map(parallel, range(2)):
                    collect(result)
        else:
            if fault == "F1" and not task_mode:
                import requests
                try:
                    submit(client, timeout=0.03)
                except requests.exceptions.Timeout:
                    pass
                else:
                    raise AssertionError("Expected ordinary call timeout did not occur")
                client.close()
                client = InstacartMcpClient(trial_id)
                retries += 1
                collect(submit(client))
                first = None
            else:
                first = submit(client)
            if first is not None and fault != "F3":
                collect(first)
            if fault in {"F1", "F3", "F6"}:
                if fault == "F1":
                    client.close()
                    client = InstacartMcpClient(trial_id)
                elif fault == "F3":
                    # The accepted JSON-RPC response is captured in the wire trace but
                    # deliberately discarded by this client before recovery.
                    retries += 1
                    collect(submit(client))
                elif fault == "F6":
                    retries += 1
                    collect(submit(client))
            elif fault == "F11":
                client.close()
                time.sleep(1.0)
                client = InstacartMcpClient(trial_id)
            elif fault == "F9":
                time.sleep(0.44)

        states = []
        for handle in handles:
            if fault in {"F8", "F9"}:
                try:
                    client.send("tasks/cancel", {"taskId": handle}, task=True)
                except AssertionError:
                    pass
            for _ in range(200):
                state = client.send("tasks/get", {"taskId": handle}, task=True)
                polls += 1
                if fault == "F10" and polls == 1:
                    # A genuine polling exchange occurred; the client discards its reply.
                    continue
                if state["status"] != "working":
                    break
                time.sleep(0.05)
            states.append(state["status"])
            if state["status"] == "completed":
                returned.append(json.loads(state["result"]["content"][0]["text"]))
        # Allow a second accepted execution to publish before reading the ledger.
        time.sleep(0.05)
        ledger_path = ROOT / "data" / "execution_ledger.jsonl"
        events = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()
                  if json.loads(line).get("requestKey") == key]
        executions = sum(item["eventName"] == "started" for item in events)
        completed = [item for item in events if item["eventName"] == "completed"]
        outputs = list((ROOT / "data" / "artifacts" / key).glob("*.csv")) if (
            ROOT / "data" / "artifacts" / key).exists() else []
        reference = reference_basket(order_limit)
        correct = bool(outputs) and all(path.read_bytes() == reference["output_bytes"] for path in outputs)
        checksum = completed[0]["checksum"] if completed else ""
        cancellation_ok = fault in {"F8", "F9"} and (
            (not outputs and all(s == "cancelled" for s in states)) or
            (correct and all(s in {"cancelled", "completed"} for s in states)))
        success = cancellation_ok if fault in {"F8", "F9"} else (
            correct and bool(returned) and all(x.get("checksum") == reference["sha256"] for x in returned))
        return {"trial_id": trial_id, "configuration": configuration, "fault": fault,
                "repetition": repetition, "status": "COMPLETED", "request_id": key,
                "task_id": "|".join(handles), "result_checksum": checksum,
                "reference_match": int(correct), "recovery_success": int(success),
                "duplicate_execution": int(executions > 1),
                "duplicate_artifact": int(len(outputs) > 1),
                "lost_task": int(any(s == "working" for s in states)),
                "recovery_latency_s": round(time.monotonic() - started, 4),
                "poll_count": polls, "retry_count": retries,
                "execution_count": executions, "artifact_count": len(outputs),
                "terminal_states": "|".join(states), "server_events": len(events)}
    finally:
        client.close()


def run_instacart_trials(repetitions=10, selected_faults=None):
    """Run supported A-D/fault pairs and write measured per-trial rows."""
    fields = ["trial_id", "configuration", "fault", "repetition", "status", "request_id",
              "task_id", "result_checksum", "reference_match", "recovery_success",
              "duplicate_execution", "duplicate_artifact", "lost_task", "recovery_latency_s",
              "poll_count", "retry_count", "execution_count", "artifact_count",
              "terminal_states", "server_events"]
    path = RESULTS / "real_mcp_trials.csv"
    if path.exists():
        with path.open(newline="", encoding="utf-8") as source:
            existing = list(csv.DictReader(source))
    else:
        existing = []
    complete = {(r["configuration"], r["fault"], int(r["repetition"])) for r in existing
                if r.get("status") == "COMPLETED"}
    faults = selected_faults or ["F0", "F1", "F2", "F3", "F6", "F7", "F8", "F9", "F10", "F11"]
    for fault in faults:
        for configuration in "ABCD":
            if fault in {"F2", "F8", "F9", "F10", "F11"} and configuration in "AD":
                continue
            for repetition in range(1, repetitions + 1):
                if (configuration, fault, repetition) in complete:
                    continue
                try:
                    row = run_instacart_trial(configuration, fault, repetition)
                except Exception as error:
                    row = dict.fromkeys(fields, "")
                    row.update(trial_id=f"failed-{configuration}-{fault}-{repetition}-{uuid.uuid4().hex[:8]}",
                               configuration=configuration, fault=fault, repetition=repetition,
                               status="TRIAL_ERROR", terminal_states=type(error).__name__)
                with path.open("a", newline="", encoding="utf-8") as output:
                    writer = csv.DictWriter(output, fieldnames=fields)
                    if output.tell() == 0:
                        writer.writeheader()
                    writer.writerow(row)
                print(configuration, fault, repetition, row["status"],
                      row.get("recovery_success", ""), flush=True)


def client_create_stage(run_id, request_key, idempotent, order_limit, delay_ms, stage_path):
    """Create a real task in a short-lived client process and persist its handle."""
    client = InstacartMcpClient(run_id)
    try:
        created = client.send("tools/call", {"name": "instacart_basket",
            "arguments": {"orderLimit": order_limit, "requestKey": request_key,
                          "delayMs": delay_ms, "idempotent": idempotent}}, task=True)
        assert created["resultType"] == "task"
        Path(stage_path).write_text(json.dumps({"task_id": created["taskId"]}), encoding="utf-8")
    finally:
        client.close()


def summarize_real_mcp_trials():
    """Recompute all reliability tables from measured rows, never simulator rows."""
    import math
    from collections import defaultdict

    def wilson(successes, total):
        if not total:
            return ("", "")
        z = 1.959963984540054
        p = successes / total
        denominator = 1 + z * z / total
        center = (p + z * z / (2 * total)) / denominator
        half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
        return round(center - half, 4), round(center + half, 4)

    with (RESULTS / "real_mcp_trials.csv").open(newline="", encoding="utf-8") as source:
        all_rows = list(csv.DictReader(source))
    ids = [row["trial_id"] for row in all_rows]
    assert len(ids) == len(set(ids)), "Duplicate trial IDs"
    groups = defaultdict(list)
    for row in all_rows:
        groups[(row["configuration"], row["fault"])].append(row)
    supported = {"A": {"F0", "F1", "F3", "F6", "F7"},
                 "B": {"F0", "F1", "F2", "F3", "F6", "F7", "F8", "F9", "F10", "F11"},
                 "C": {"F0", "F1", "F2", "F3", "F6", "F7", "F8", "F9", "F10", "F11"},
                 "D": {"F0", "F1", "F3", "F6", "F7"}}
    summary = []
    failures = []
    stats = []
    for config in "ABCD":
        for index in range(12):
            fault = f"F{index}"
            rows = groups[(config, fault)]
            completed = [r for r in rows if r["status"] == "COMPLETED"]
            n = len(completed)
            if fault not in supported[config]:
                reason = ("Worker is in the server process; isolated F4/F5 worker-crash boundary "
                          "was not implemented" if fault in {"F4", "F5"} else
                          "Ordinary calls have no task ID, tasks/cancel, or tasks/get polling")
                status = "NOT_RUN"
            elif n == 0:
                reason, status = "Supported harness path has no completed trials", "NOT_RUN"
            else:
                reason, status = "", "COMPLETED"
            success = sum(int(r["recovery_success"]) for r in completed)
            duplicates = sum(int(r["duplicate_execution"]) for r in completed)
            duplicate_outputs = sum(int(r["duplicate_artifact"]) for r in completed)
            correct = sum(int(r["reference_match"]) for r in completed)
            lost = sum(int(r["lost_task"]) for r in completed)
            summary.append({"configuration": config, "fault": fault, "completed_trials": n,
                            "status": status, "recovery_successes": success,
                            "duplicate_executions": duplicates,
                            "duplicate_artifacts": duplicate_outputs,
                            "reference_matches": correct, "lost_tasks": lost,
                            "reason": reason})
            for metric, count in (("recovery_success", success),
                                  ("duplicate_execution", duplicates),
                                  ("duplicate_artifact", duplicate_outputs),
                                  ("reference_match", correct), ("lost_task", lost)):
                low, high = wilson(count, n)
                stats.append({"configuration": config, "fault": fault, "metric": metric,
                              "numerator": count, "denominator": n,
                              "rate": round(count / n, 4) if n else "",
                              "ci95_low": low, "ci95_high": high,
                              "interval_note": "Wilson within one fixed fault template; repetitions are not independent failure types"})
            if status == "NOT_RUN":
                failures.append({"configuration": config, "fault": fault,
                                 "trial_id": "", "status": status, "reason": reason})
            for row in rows:
                if row["status"] != "COMPLETED" or row.get("recovery_success") != "1":
                    failures.append({"configuration": config, "fault": fault,
                                     "trial_id": row["trial_id"], "status": row["status"],
                                     "reason": row.get("terminal_states", "") or "Recovery outcome unsuccessful"})
                elif row.get("duplicate_execution") == "1":
                    failures.append({"configuration": config, "fault": fault,
                                     "trial_id": row["trial_id"], "status": "DUPLICATE_EXECUTION",
                                     "reason": ("Duplicate published artifact" if row.get("duplicate_artifact") == "1"
                                                else "Second execution started; no duplicate artifact")})

    # Fault-template clusters are the comparison units; 30 repeats of one fault
    # do not turn that fault into 30 independent failure mechanisms.
    import random
    rng = random.Random(20261011)
    for left, right in (("A", "B"), ("A", "D"), ("B", "C"), ("C", "D")):
        shared = sorted(supported[left] & supported[right])
        effects = []
        for fault in shared:
            lrows = [r for r in groups[(left, fault)] if r["status"] == "COMPLETED"]
            rrows = [r for r in groups[(right, fault)] if r["status"] == "COMPLETED"]
            if lrows and rrows:
                effects.append(sum(int(r["duplicate_execution"]) for r in lrows) / len(lrows)
                               - sum(int(r["duplicate_execution"]) for r in rrows) / len(rrows))
        if effects:
            samples = sorted(sum(rng.choice(effects) for _ in effects) / len(effects)
                             for _ in range(10000))
            stats.append({"configuration": f"{left}-{right}", "fault": "shared templates",
                          "metric": "paired_duplicate_rate_difference", "numerator": "",
                          "denominator": len(effects), "rate": round(sum(effects) / len(effects), 4),
                          "ci95_low": round(samples[249], 4),
                          "ci95_high": round(samples[9749], 4),
                          "interval_note": "Fault-template cluster bootstrap; positive means first configuration had more duplicates"})

    def write(name, rows, fields):
        with (RESULTS / name).open("w", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    write("real_mcp_summary.csv", summary, list(summary[0]))
    write("real_mcp_failures.csv", failures,
          ["configuration", "fault", "trial_id", "status", "reason"])
    write("real_mcp_statistics.csv", stats, list(stats[0]))
    return {"completed_trials": sum(row["status"] == "COMPLETED" for row in all_rows),
            "trial_errors": sum(row["status"] != "COMPLETED" for row in all_rows),
            "supported_combinations": sum(row["status"] == "COMPLETED" for row in summary),
            "not_run_combinations": sum(row["status"] == "NOT_RUN" for row in summary)}


def audit_real_mcp_trials():
    """Independently recheck saved rows against raw protocol, ledger, and output bytes."""
    from collections import defaultdict

    with (RESULTS / "real_mcp_trials.csv").open(newline="", encoding="utf-8") as source:
        rows = list(csv.DictReader(source))
    trace = defaultdict(list)
    for line in (RESULTS / "real_mcp_traces.jsonl").read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        if item.get("provenance") == "REAL_MCP_INSTACART_WORKLOAD":
            trace[item["run_id"]].append(item)
    ledger = defaultdict(list)
    for line in (ROOT / "data" / "execution_ledger.jsonl").read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        ledger[item["requestKey"]].append(item)
    reference = reference_basket(20)
    checked = task_states_checked = 0
    for row in rows:
        if row["status"] != "COMPLETED":
            continue
        run_id = row["trial_id"]
        messages = trace[run_id]
        assert messages and all("request" in x for x in messages), run_id
        calls = [x for x in messages if x["request"]["method"] == "tools/call"]
        assert calls, run_id
        for call in calls:
            assert call["request"]["params"]["arguments"]["requestKey"] == run_id
            assert call["request"]["params"]["name"] == "instacart_basket"
        task_mode = row["configuration"] in "BC"
        if task_mode:
            task_ids = row["task_id"].split("|")
            assert task_ids and task_ids[0]
            created = [x["response"]["result"] for x in calls if x.get("response") and
                       x["response"].get("result", {}).get("resultType") == "task"]
            assert all(item["taskId"] in {x["taskId"] for x in created} for item in created)
            assert set(task_ids) <= {x["taskId"] for x in created}
            for task in created:
                persisted = json.loads((ROOT / "data" / "task_store" /
                    (task["taskId"] + ".json")).read_text(encoding="utf-8"))
                assert persisted["TaskId"] == task["taskId"]
                assert persisted["Status"] in {"completed", "cancelled"}, (run_id, persisted["Status"])
                if persisted["Status"] == "completed":
                    stored_result = json.loads(persisted["Result"]["content"][0]["text"])
                    assert stored_result["requestKey"] == run_id
                    assert Path(stored_result["output"]).read_bytes() == reference["output_bytes"]
                else:
                    assert row["fault"] in {"F8", "F9"}
                task_states_checked += 1
        else:
            assert not row["task_id"]
        events = ledger[run_id]
        assert int(row["execution_count"]) == sum(x["eventName"] == "started" for x in events)
        paths = list((ROOT / "data" / "artifacts" / run_id).glob("*.csv")) if (
            ROOT / "data" / "artifacts" / run_id).exists() else []
        assert len(paths) == int(row["artifact_count"])
        assert int(row["duplicate_execution"]) == (len({x["invocation"] for x in events
            if x["eventName"] == "started"}) > 1)
        assert int(row["duplicate_artifact"]) == (len(paths) > 1)
        if paths:
            assert all(path.read_bytes() == reference["output_bytes"] for path in paths)
            assert row["result_checksum"] == reference["sha256"]
        else:
            assert row["fault"] in {"F8", "F9"} and not row["result_checksum"]
        checked += 1
    return {"independently_checked_trials": checked,
            "workload_trace_exchanges": sum(len(items) for items in trace.values()),
            "persistent_task_states_checked": task_states_checked,
            "unique_trial_ids": len({r["trial_id"] for r in rows}),
            "reference_sha256": reference["sha256"]}


def reconcile_active_restart(task_id, request_key, order_limit=20):
    """Cancel an orphaned durable task and safely retry its logical operation."""
    client = InstacartMcpClient("active-restart-reconcile-20261011")
    try:
        before = client.send("tasks/get", {"taskId": task_id}, task=True)
        assert before["status"] == "working"
        cancellation = client.send("tasks/cancel", {"taskId": task_id}, task=True)
        after = client.send("tasks/get", {"taskId": task_id}, task=True)
        assert after["status"] == "cancelled"
        created = client.send("tools/call", {"name": "instacart_basket",
            "arguments": {"orderLimit": order_limit, "requestKey": request_key,
                          "delayMs": 120, "idempotent": True}}, task=True)
        assert created["resultType"] == "task"
        for _ in range(100):
            state = client.send("tasks/get", {"taskId": created["taskId"]}, task=True)
            if state["status"] != "working":
                break
            time.sleep(0.05)
        assert state["status"] == "completed"
        result = json.loads(state["result"]["content"][0]["text"])
        reference = reference_basket(order_limit)
        assert Path(result["output"]).read_bytes() == reference["output_bytes"]
        return {"old_task": task_id, "before": before["status"],
                "cancel_ack": cancellation.get("status"), "old_after": after["status"],
                "new_task": created["taskId"], "new_status": state["status"],
                "reference_match": True, "checksum": result["checksum"]}
    finally:
        client.close()


def audit_fault_injections():
    """Check observable fault effects in trial-specific JSON-RPC traces."""
    from collections import Counter, defaultdict

    with (RESULTS / "real_mcp_trials.csv").open(newline="", encoding="utf-8") as source:
        trials = list(csv.DictReader(source))
    traces = defaultdict(list)
    for line in (RESULTS / "real_mcp_traces.jsonl").open(encoding="utf-8"):
        event = json.loads(line)
        traces[event.get("run_id")].append(event)
    observed = Counter()
    for row in trials:
        assert row["status"] == "COMPLETED"
        fault, config = row["fault"], row["configuration"]
        events = traces[row["trial_id"]]
        calls = [e for e in events if e["request"]["method"] == "tools/call"]
        polls = [e for e in events if e["request"]["method"] == "tasks/get"]
        cancels = [e for e in events if e["request"]["method"] == "tasks/cancel"]
        timeouts = [e for e in events if e.get("client_exception") == "ReadTimeout"]
        if fault == "F1" and config in "AD":
            assert len(calls) == 2 and len(timeouts) == 1
        elif fault in {"F3", "F6", "F7"}:
            assert len(calls) == 2
        elif fault in {"F8", "F9"}:
            assert len(cancels) == 1 and row["terminal_states"] == "cancelled"
        elif fault == "F10":
            assert len(polls) >= 2
        elif fault == "F11":
            assert float(row["recovery_latency_s"]) >= 1.0
        elif fault == "F2":
            assert len(calls) == 1 and len(polls) >= 1
        elif fault == "F1":
            assert len(calls) == 1 and len(polls) >= 1
        elif fault == "F0":
            assert len(calls) == 1
        else:
            raise AssertionError(fault)
        observed[fault] += 1
    result = {"validated_trials": len(trials), "observable_fault_counts": dict(sorted(observed.items())),
              "limitations": {"F1_tasks": "session.close is controller-observed, not a network proxy fault",
                              "F2": "short-lived child process exit is controller-observed",
                              "F3": "accepted response is captured then intentionally discarded by client",
                              "F10": "poll response is captured then intentionally discarded by client",
                              "F4_F5": "not run; no isolated worker process"}}
    (RESULTS / "fault_injection_audit.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def capture_scenarios(count=20):
    """Execute local task transitions and capture 10 failure modes twice."""
    RESULTS.mkdir(exist_ok=True)
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, request_key TEXT, status TEXT, output TEXT, executions INTEGER)")
    events, scenarios = [], []
    modes = [
        ("network_timeout_running", "running", "Network timeout", True, None),
        ("restart_known_id", "running", "Client restarted; saved task ID", True, None),
        ("restart_missing_id", "running", "Client restarted; no saved task ID", False, None),
        ("creation_response_lost", "running", "Task creation response lost", False, None),
        ("completion_response_lost", "completed", "Timed out awaiting completion", True, "artifact ready"),
        ("worker_crash_after_output", "failed", "Worker connection closed", True, "artifact written"),
        ("stale_status", "completed", "Cached status: running (stale)", True, "artifact ready"),
        ("cancel_ack_continues", "running", "Cancellation acknowledged", True, None),
        ("duplicate_request", "running", "Duplicate request detected", True, None),
        ("existing_recovered", "completed", "Task recovered; result available", True, "artifact ready"),
    ]
    for n in range(count):
        mode, status, response, known, output = modes[n % len(modes)]
        task_id, request_key = f"task-{n:03d}", f"request-{n:03d}"
        timeline = []
        db.execute("INSERT INTO tasks VALUES (?, ?, 'pending', NULL, 0)", (task_id, request_key))
        timeline.append({"operation": "create_task", "status": "pending", "executions": 0})
        db.execute("UPDATE tasks SET status='running',executions=1 WHERE id=?", (task_id,))
        timeline.append({"operation": "start_worker", "status": "running", "executions": 1})
        if output is not None:
            db.execute("UPDATE tasks SET output=? WHERE id=?", (output, task_id))
            timeline.append({"operation": "write_output", "output": output})
        if status != "running":
            db.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))
            timeline.append({"operation": "set_terminal_status", "status": status})
        row = db.execute("SELECT id,request_key,status,output,executions FROM tasks WHERE id=?", (task_id,)).fetchone()
        state = dict(zip(("task_id", "request_key", "status", "output", "executions"), row))
        event = {"event_id": f"event-{n:03d}", "source": "isolated_local_mcp_simulator",
                 "fault_injection": mode, "task_id": task_id, "client_response": response,
                 "timeline": timeline, "verified_state": state}
        events.append(event)
        # The limited arm sees only client-visible information and saved IDs.
        limited = {
            "network_timeout_running": ["POLL_EXISTING_TASK", "CHECK_PERSISTED_STATE", "WAIT_FOR_UPDATE"],
            "restart_known_id": ["POLL_EXISTING_TASK", "CHECK_PERSISTED_STATE"],
            "restart_missing_id": ["CHECK_PERSISTED_STATE", "REQUEST_HUMAN_INTERVENTION"],
            "creation_response_lost": ["CHECK_PERSISTED_STATE", "REQUEST_HUMAN_INTERVENTION"],
            "completion_response_lost": ["POLL_EXISTING_TASK", "CHECK_PERSISTED_STATE"],
            "worker_crash_after_output": ["CHECK_PERSISTED_STATE"],
            "stale_status": ["POLL_EXISTING_TASK", "CHECK_PERSISTED_STATE"],
            "cancel_ack_continues": ["POLL_EXISTING_TASK", "CHECK_PERSISTED_STATE", "WAIT_FOR_UPDATE"],
            "duplicate_request": ["CHECK_PERSISTED_STATE", "POLL_EXISTING_TASK"],
            "existing_recovered": ["REPORT_COMPLETION"],
        }[mode]
        full = {
            "network_timeout_running": ["POLL_EXISTING_TASK", "CHECK_PERSISTED_STATE", "WAIT_FOR_UPDATE"],
            "restart_known_id": ["POLL_EXISTING_TASK", "CHECK_PERSISTED_STATE"],
            "restart_missing_id": ["POLL_EXISTING_TASK", "CHECK_PERSISTED_STATE"],
            "creation_response_lost": ["POLL_EXISTING_TASK", "CHECK_PERSISTED_STATE"],
            "completion_response_lost": ["REPORT_COMPLETION", "CHECK_PERSISTED_STATE"],
            "worker_crash_after_output": ["CHECK_PERSISTED_STATE"],
            "stale_status": ["REPORT_COMPLETION", "CHECK_PERSISTED_STATE"],
            "cancel_ack_continues": ["POLL_EXISTING_TASK", "CHECK_PERSISTED_STATE", "WAIT_FOR_UPDATE"],
            "duplicate_request": ["POLL_EXISTING_TASK", "CHECK_PERSISTED_STATE"],
            "existing_recovered": ["REPORT_COMPLETION", "CHECK_PERSISTED_STATE"],
        }[mode]
        scenarios.append({"scenario_id": f"scenario-{n:03d}", "event_id": event["event_id"],
                          "provenance": "captured_isolated_execution", "failure_mode": mode,
                          "original_task": f"Generate an output artifact for {request_key} exactly once.",
                          "client_response": response,
                          "available_task_ids": [task_id] if known else [],
                          "verified_durable_state": state,
                          "available_actions": ACTIONS,
                          "acceptable_limited": limited, "acceptable_full": full,
                          "retry_duplicate_risk": True,
                          "expected_state": status})
    for name, rows in (("mcp_event_traces.jsonl", events), ("llm_scenarios.jsonl", scenarios)):
        (RESULTS / name).write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    db.close()
    return scenarios


def execute_recovery_action(scenario, action):
    """Replay one model choice in a fresh, isolated task ledger."""
    state = scenario["verified_durable_state"]
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, output TEXT, executions INTEGER)")
    db.execute("INSERT INTO tasks VALUES (?, ?, ?, ?)",
               (state["task_id"], state["status"], state["output"], state["executions"]))
    if action == "RETRY_ORIGINAL_REQUEST":
        db.execute("UPDATE tasks SET executions=executions+1 WHERE id=?", (state["task_id"],))
    elif action == "CANCEL_TASK" and scenario["failure_mode"] != "cancel_ack_continues":
        db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (state["task_id"],))
    row = db.execute("SELECT status,output,executions FROM tasks WHERE id=?", (state["task_id"],)).fetchone()
    db.close()
    return {"post_status": row[0], "post_output": row[1], "post_executions": row[2],
            "duplicate_executed": row[2] > state["executions"],
            "work_cancelled": row[0] == "cancelled",
            "premature_completion_report": action == "REPORT_COMPLETION" and state["status"] != "completed",
            "recovery_complete": action == "REPORT_COMPLETION" and state["status"] == "completed"}


def run_protocol_validation(url="http://127.0.0.1:5157/stateless"):
    """Probe the unmodified official C# conformance server over real HTTP JSON-RPC.

    This is a protocol smoke test. The server's slow_compute tool sleeps; it is
    not an Instacart workload or a recovery outcome trial.
    """
    import requests

    RESULTS.mkdir(exist_ok=True)
    run_id = "protocol-" + uuid.uuid4().hex[:12]
    trace_path = RESULTS / "real_mcp_traces.jsonl"
    sequence = 0
    session = requests.Session()
    observations = {}

    def send(method, params=None, opt_in=False, client_session=None):
        nonlocal sequence
        sequence += 1
        body = {"jsonrpc": "2.0", "id": sequence, "method": method,
                "params": dict(params or {})}
        meta = {"io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
                "io.modelcontextprotocol/clientCapabilities":
                    {"extensions": {TASKS_EXTENSION: {}} if opt_in else {}}}
        body["params"]["_meta"] = meta
        headers = {"Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": PROTOCOL_VERSION, "Mcp-Method": method}
        if method == "tools/call":
            headers["Mcp-Name"] = body["params"]["name"]
        elif method.startswith("tasks/"):
            headers["Mcp-Name"] = body["params"]["taskId"]
        started = datetime.now(timezone.utc).isoformat()
        response = (client_session or session).post(url, json=body, headers=headers, timeout=15)
        received = datetime.now(timezone.utc).isoformat()
        raw = response.text
        if response.headers.get("Content-Type", "").startswith("text/event-stream"):
            messages = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: ")]
            reply = next((item for item in messages if item.get("id") == sequence), None)
        else:
            reply = json.loads(raw)
        trace = {"run_id": run_id, "provenance": "REAL_MCP_TASKS_PROTOCOL_SMOKE",
                 "sent_at_utc": started, "received_at_utc": received,
                 "transport": "streamable_http_sse", "url": url,
                 "request": body, "response": reply, "http_status": response.status_code,
                 "response_content_type": response.headers.get("Content-Type")}
        with trace_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(trace, sort_keys=True) + "\n")
        assert response.status_code == 200, (method, reply)
        assert reply and reply.get("jsonrpc") == "2.0" and reply.get("id") == sequence, reply
        assert "error" not in reply, (method, reply)
        return reply["result"]

    try:
        discovered = send("server/discover", opt_in=True)
        assert PROTOCOL_VERSION in discovered["supportedVersions"]
        assert TASKS_EXTENSION in discovered["capabilities"]["extensions"]
        observations["discovery"] = "PASS"

        listed = send("tools/list", opt_in=True)
        names = {tool["name"] for tool in listed["tools"]}
        assert {"greet", "slow_compute"} <= names
        observations["tool_discovery"] = "PASS"

        ordinary = send("tools/call", {"name": "greet", "arguments": {"name": "MCP"}})
        assert ordinary.get("resultType") == "complete" and "content" in ordinary
        observations["ordinary_result"] = "PASS"

        created = send("tools/call", {"name": "slow_compute",
                                      "arguments": {"seconds": 1, "label": run_id}}, opt_in=True)
        assert created.get("resultType") == "task"
        assert isinstance(created.get("taskId"), str) and created["taskId"]
        assert created["status"] == "working"
        assert isinstance(created.get("createdAt"), str)
        task_id = created["taskId"]
        observations["task_creation"] = "PASS"
        observations["task_id"] = task_id

        # A fresh HTTP client sees the server-generated task ID after client transport loss.
        session.close()
        with requests.Session() as resumed:
            for _ in range(30):
                state = send("tasks/get", {"taskId": task_id}, opt_in=True,
                             client_session=resumed)
                assert state.get("resultType") == "complete"
                assert state.get("taskId") == task_id
                assert state.get("status") in {"working", "completed", "failed", "cancelled"}
                if state["status"] != "working":
                    break
                time.sleep(max(0.05, (state.get("pollIntervalMs") or 50) / 1000))
        assert state["status"] == "completed", state
        assert "result" in state and run_id in json.dumps(state["result"])
        observations["client_transport_reconnect"] = "PASS"
        observations["terminal_result"] = "PASS"

        with requests.Session() as next_client:
            to_cancel = send("tools/call", {"name": "slow_compute",
                                             "arguments": {"seconds": 3, "label": "cancel-" + run_id}},
                             opt_in=True, client_session=next_client)
            assert to_cancel.get("resultType") == "task"
            ack = send("tasks/cancel", {"taskId": to_cancel["taskId"]},
                       opt_in=True, client_session=next_client)
            assert ack.get("resultType") == "complete"
            observations["cancel_ack"] = "PASS"
            for _ in range(30):
                cancelled = send("tasks/get", {"taskId": to_cancel["taskId"]},
                                 opt_in=True, client_session=next_client)
                if cancelled["status"] in {"cancelled", "completed", "failed"}:
                    break
                time.sleep(0.05)
            observations["cancel_terminal_status"] = cancelled["status"]
        with requests.Session() as input_client:
            waiting = send("tools/call", {"name": "confirm_delete",
                                          "arguments": {"filename": "nonexistent-smoke-" + run_id}},
                           opt_in=True, client_session=input_client)
            assert waiting.get("resultType") == "task"
            for _ in range(30):
                required = send("tasks/get", {"taskId": waiting["taskId"]},
                                opt_in=True, client_session=input_client)
                if required["status"] == "input_required":
                    break
                time.sleep(0.05)
            assert required["status"] == "input_required", required
            input_key = next(iter(required["inputRequests"]))
            update = send("tasks/update", {"taskId": waiting["taskId"],
                                           "inputResponses": {input_key: {"action": "decline"}}},
                          opt_in=True, client_session=input_client)
            assert update.get("resultType") == "complete"
            for _ in range(30):
                updated = send("tasks/get", {"taskId": waiting["taskId"]},
                               opt_in=True, client_session=input_client)
                if updated["status"] in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.05)
            assert updated["status"] == "completed", updated
            observations["tasks_update"] = "PASS"
        return {"run_id": run_id, **observations}
    finally:
        session.close()


def validate_protocol_traces(run_id):
    """Check saved task messages against the official 2026-07-28 extension schema."""
    from jsonschema import Draft202012Validator

    schema_path = ROOT / ".vendor" / "ext-tasks" / "schema" / PROTOCOL_VERSION / "schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    definitions = schema["$defs"]
    rows = [json.loads(line) for line in (RESULTS / "real_mcp_traces.jsonl").read_text(encoding="utf-8").splitlines()]
    rows = [row for row in rows if row["run_id"] == run_id]
    assert rows, run_id
    checks = 0
    request_schemas = {"tasks/get": "GetTaskRequest", "tasks/cancel": "CancelTaskRequest",
                       "tasks/update": "UpdateTaskRequest"}
    response_schemas = {"tasks/get": "GetTaskResult", "tasks/cancel": "CancelTaskResult",
                        "tasks/update": "UpdateTaskResult"}
    for row in rows:
        method = row["request"]["method"]
        reply = row["response"]
        assert row["http_status"] == 200 and "error" not in reply, row
        if method in request_schemas:
            name = request_schemas[method]
            Draft202012Validator({"$defs": definitions, "$ref": "#/$defs/" + name}).validate(row["request"])
            checks += 1
        result = reply["result"]
        name = ("CreateTaskResult" if method == "tools/call" and result.get("resultType") == "task"
                else response_schemas.get(method))
        if name:
            Draft202012Validator({"$defs": definitions, "$ref": "#/$defs/" + name}).validate(result)
            checks += 1
        if method.startswith("tasks/"):
            assert result["resultType"] == "complete"
    return {"run_id": run_id, "exchanges": len(rows), "schema_checks": checks,
            "schema_source": str(schema_path)}


def validate_workload_task_schema():
    """Validate every captured workload Tasks message against the official schema."""
    from jsonschema import Draft202012Validator

    schema_path = ROOT / ".vendor" / "ext-tasks" / "schema" / PROTOCOL_VERSION / "schema.json"
    definitions = json.loads(schema_path.read_text(encoding="utf-8"))["$defs"]
    request_types = {"tasks/get": "GetTaskRequest", "tasks/cancel": "CancelTaskRequest",
                     "tasks/update": "UpdateTaskRequest"}
    result_types = {"tasks/get": "GetTaskResult", "tasks/cancel": "CancelTaskResult",
                    "tasks/update": "UpdateTaskResult"}
    validators = {name: Draft202012Validator({"$defs": definitions, "$ref": "#/$defs/" + name})
                  for name in set(request_types.values()) | set(result_types.values()) | {"CreateTaskResult"}}
    messages = checks = client_timeouts = rpc_errors = 0
    for line in (RESULTS / "real_mcp_traces.jsonl").open(encoding="utf-8"):
        row = json.loads(line)
        if row.get("provenance") != "REAL_MCP_INSTACART_WORKLOAD":
            continue
        messages += 1
        if "client_exception" in row:
            client_timeouts += 1
            continue
        method = row["request"]["method"]
        if method in request_types:
            validators[request_types[method]].validate(row["request"])
            checks += 1
        reply = row["response"]
        if "error" in reply:
            rpc_errors += 1
            continue
        result = reply["result"]
        name = ("CreateTaskResult" if method == "tools/call" and result.get("resultType") == "task"
                else result_types.get(method))
        if name:
            validators[name].validate(result)
            checks += 1
    result = {"workload_exchanges": messages, "schema_checks": checks,
              "client_timeouts": client_timeouts, "jsonrpc_errors": rpc_errors,
              "schema_source": str(schema_path)}
    (RESULTS / "real_mcp_schema_audit.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def probe_task_after_server_restart(task_id, url="http://127.0.0.1:5157/stateless"):
    """Query a saved task ID after manually restarting the official server."""
    import requests

    run_id = "restart-" + uuid.uuid4().hex[:12]
    request = {"jsonrpc": "2.0", "id": 1, "method": "tasks/get",
               "params": {"taskId": task_id, "_meta": {
                   "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
                   "io.modelcontextprotocol/clientCapabilities":
                       {"extensions": {TASKS_EXTENSION: {}}}}}}
    headers = {"Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": PROTOCOL_VERSION,
               "Mcp-Method": "tasks/get", "Mcp-Name": task_id}
    started = datetime.now(timezone.utc).isoformat()
    response = requests.post(url, json=request, headers=headers, timeout=10)
    received = datetime.now(timezone.utc).isoformat()
    data = [json.loads(line[6:]) for line in response.text.splitlines()
            if line.startswith("data: ")]
    reply = data[0] if data else response.json()
    with (RESULTS / "real_mcp_traces.jsonl").open("a", encoding="utf-8") as file:
        file.write(json.dumps({"run_id": run_id,
                               "provenance": "REAL_MCP_TASKS_PROTOCOL_SMOKE",
                               "condition": "after_official_server_process_restart",
                               "sent_at_utc": started, "received_at_utc": received,
                               "transport": "streamable_http_sse", "url": url,
                               "request": request, "response": reply,
                               "http_status": response.status_code,
                               "response_content_type": response.headers.get("Content-Type")},
                              sort_keys=True) + "\n")
    return {"run_id": run_id, "task_id": task_id, "response": reply}


def write_feasibility_matrix():
    """Retired pre-data audit; prevent accidental overwrite of real trial files."""
    raise RuntimeError("The pre-data feasibility matrix is historical. Use summarize_real_mcp_trials().")


if __name__ == "__main__":
    print("Use main.py for real workload trials; historical simulator files are preserved.")
