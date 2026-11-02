"""Frozen plans and v4-reported-token cost forecasts. No API client is used."""
from dataclasses import asdict
import csv
from decimal import Decimal
import hashlib
import json
import random
from pathlib import Path
from statistics import mean

from .. import runner as shared
from ..prompts import CONDITIONS, SYSTEM, FINAL_FORMAT, messages
from ..simulator import TOOLS
from .tasks import ALL, DEVELOPMENT, HELDOUT, VERSION
from .evaluation import oracle

ROOT = Path(__file__).resolve().parent
SEED = 20261027
MODELS = ("gpt-6-luna", "gpt-6.1-sol")
LIMITS = shared.Limits()  # All original model/generation/execution settings retained.
DEV_CAP = Decimal("0.50")
PHASE_CAP = Decimal("6.00")


def source_hash():
    digest = hashlib.sha256(shared.source_digest().encode())
    for path in sorted(ROOT.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def payload(case):
    return json.loads(json.dumps(asdict(case)))


def stage_tasks(collection):
    cases = DEVELOPMENT if collection == "development" else HELDOUT
    groups = {}
    rng = random.Random(SEED + 1)
    for category in ("execution", "recovery", "stopping"):
        selected = [c for c in cases if c.task.category == category]
        rng.shuffle(selected)
        for index, case in enumerate(selected):
            # Dev: one task/category in each of two stages. Heldout: two/category
            # in each of four stages. Assignment is fixed before model outcomes.
            groups[case.task.id] = index + 1 if collection == "development" else index // 2 + 1
    return groups


def planned(collection):
    cases = DEVELOPMENT if collection == "development" else HELDOUT
    repetitions = 1 if collection == "development" else 2
    plan = shared.schedule(MODELS, [c.task for c in cases], repetitions, SEED)
    if collection == "development":
        plan = [item for item in plan if item["condition"] == "A"]
    groups = stage_tasks(collection)
    plan.sort(key=lambda item: groups[item["task_id"]])  # Stable partition keeps block/condition order.
    for order, item in enumerate(plan):
        item.update(stage=groups[item["task_id"]], order=order,
            episode_id=f"phase3:{collection}:{VERSION}:{item['model']}:{item['task_id']}:{item['repetition']}:{item['condition']}")
    return plan


def oracle_requests(case, condition, model):
    script = oracle(case)
    history = messages(case.task, condition)
    requests = []
    for index, action in enumerate(script["actions"]):
        requests.append(shared.build_request(case.task, condition, model, LIMITS, history=history))
        history.append({"type": "function_call", "call_id": f"oracle_{index}",
                        "name": action["tool"], "arguments": json.dumps(action["arguments"])})
        history.append({"type": "function_call_output", "call_id": f"oracle_{index}",
                        "output": json.dumps(script["trace"][index]["response"], sort_keys=True)})
    requests.append(shared.build_request(case.task, condition, model, LIMITS, history=history))
    return requests


def v4_measurements():
    stats, hashes = {}, {}
    for stage in ("A", "B"):
        path = shared.ROOT / "results" / "pilot_v4" / f"phase2-pilot-20261027-v4-{stage}-live" / "episodes.jsonl"
        hashes[str(path.relative_to(shared.ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            key = (row["model"], row["category"])
            group = stats.setdefault(key, {"ratios": [], "output": [], "reasoning": []})
            for call in row["interactions"]:
                usage = call["response"]["usage"]
                size = len(json.dumps(call["request"], ensure_ascii=False).encode("utf-8"))
                group["ratios"].append(usage["input_tokens"] / size)
                group["output"].append(usage["output_tokens"])
                group["reasoning"].append(usage["output_tokens_details"]["reasoning_tokens"])
    return stats, hashes


def forecasts():
    stats, hashes = v4_measurements()
    rates = json.loads((shared.ROOT / "pricing.json").read_text())["models"]
    estimates = {}
    for case in ALL:
        for model in MODELS:
            for condition in CONDITIONS:
                inputs = oracle_requests(case, condition, model)
                observed = stats[(model, case.task.category)]
                # Byte scaling includes the larger records and full growing conversation,
                # rather than multiplying the v4 per-episode average by action count.
                input_tokens = sum(len(json.dumps(r, ensure_ascii=False).encode("utf-8")) *
                                   mean(observed["ratios"]) for r in inputs)
                output_tokens = len(inputs) * mean(observed["output"]) * 1.5
                price = rates[model]
                # No cache discount; include potential 30m cache-write pricing.
                nominal = (input_tokens * price["cache_write"] + output_tokens * price["output"]) / 1_000_000
                stressed = nominal * 2 + len(inputs) * 256 * price["output"] / 1_000_000
                estimates[(case.task.id, model, condition)] = {
                    "nominal_usd": nominal, "stressed_usd": stressed,
                    "oracle_model_calls": len(inputs), "oracle_tool_calls": len(inputs) - 1,
                    "max_request_bytes": max(len(json.dumps(r, ensure_ascii=False).encode("utf-8")) for r in inputs)}
    return estimates, hashes


def build_manifest(collection, estimates=None, v4_hashes=None, *, one_repetition=False):
    if one_repetition and collection != "heldout":
        raise ValueError("One-repetition alternative is for heldout evaluation only")
    if estimates is None:
        estimates, v4_hashes = forecasts()
    cases = DEVELOPMENT if collection == "development" else HELDOUT
    plan = planned(collection)
    if one_repetition:
        plan = [p for p in plan if p["repetition"] == 0]
    for item in plan:
        item["forecast"] = estimates[(item["task_id"], item["model"], item["condition"])]
    manifest = {"benchmark": VERSION, "collection": collection, "seed": SEED,
        "variant": "one_rep_alternative" if one_repetition else "requested_plan",
        "repetitions": 1 if collection == "development" or one_repetition else 2,
        "release": "neutral development only" if collection == "development" else "candidate frozen; live release requires reviewed development results",
        "models": list(MODELS), "conditions": {c: {"text": text, "sha256": hashlib.sha256(text.encode()).hexdigest()}
                                                  for c, text in CONDITIONS.items()},
        "settings": {"limits": asdict(LIMITS), "reasoning_effort": {m: "low" for m in MODELS},
                     "request": shared.REQUEST_SETTINGS, "system": SYSTEM, "final_format": FINAL_FORMAT, "tools": TOOLS},
        "tasks": {c.task.id: {"specification": payload(c), "sha256": shared.json_hash(payload(c)),
                    "oracle_actions": oracle(c)["actions"],
                    "user_prompt_sha256": {condition: hashlib.sha256(messages(c.task, condition)[0]["content"].encode()).hexdigest()
                                           for condition in CONDITIONS}} for c in cases},
        "evaluation": {"primary": "Operational task success; independent explicit gold counts plus reference trace checks agree with original deterministic metrics.",
            "contrast": "C-B", "secondary": ["violations", "unnecessary calls", "incorrect retries", "duplicate writes",
                "recovery/stopping accuracy", "tokens/cost/latency", "D-C"],
            "denominators": "Original definitions retained: agent limits/output truncation/incorrect reports fail; infrastructure separate; zero-request budget censoring excluded.",
            "independence": "Gold counts are hand specified; no LLM judge, no Environment.evaluate/expected_state calls in the independent checker.",
            "inference": "Pair within task/model/repetition, average repeats within tasks, resample tasks within category; no significance/equivalence claims."},
        "budget": {"development_usd": str(DEV_CAP), "phase3_additional_usd": str(PHASE_CAP),
                   "experiment_usd": "9.00", "ledger": "competitive_prompting/results/spend.jsonl",
                   "historical_encumbrance": "Keep fully counted globally; never reset or duplicate the ledger."},
        "randomization": "Original scheduler seed 20261027 shuffles blocks/conditions. Task membership within categories is shuffled with seed 20261028, then stably partitioned into balanced stages. Fresh conversations/state for all episodes.",
        "v4_measurement_sha256": v4_hashes, "pricing": json.loads((shared.ROOT / "pricing.json").read_text()),
        "forecast": {name: sum(item["forecast"][name] for item in plan) for name in ("nominal_usd", "stressed_usd")},
        "forecast_assumptions": "Mean v4 reported input-token/serialized-byte ratios by model/category; oracle growing histories; output 1.5x v4 mean/call; no cache discount, 30m write rate. Stress: 2x nominal plus 256 reasoning tokens/request. Forecast only, not measured Phase 3 usage or guaranteed bounds.",
        "source_sha256": source_hash(), "plan": plan}
    manifest["sha256"] = shared.json_hash(manifest)
    return manifest


def prepare():
    estimates, hashes = forecasts()
    for collection in ("development", "heldout"):
        manifest = build_manifest(collection, estimates, hashes)
        path = ROOT / f"manifest_{collection}_{VERSION}.json"
        with path.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(manifest, indent=2) + "\n")
    alternative = build_manifest("heldout", estimates, hashes, one_repetition=True)
    with (ROOT / f"manifest_heldout_one_rep_{VERSION}.json").open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(alternative, indent=2) + "\n")
    with (ROOT / f"oracle_traces_{VERSION}.jsonl").open("x", encoding="utf-8") as stream:
        for case in ALL:
            stream.write(json.dumps(oracle(case)) + "\n")
    inventory = []
    for case in ALL:
        inventory.append({"task_id": case.task.id, "collection": case.split, "category": case.task.category,
            "records": len(case.task.records), "oracle_tool_calls": len(oracle(case)["actions"]),
            "faults": len(case.task.failures), "expected_status": case.task.expected_status, "rationale": case.rationale})
    with (ROOT / f"task_inventory_{VERSION}.csv").open("x", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(inventory[0]))
        writer.writeheader()
        writer.writerows(inventory)
    full = build_manifest("heldout", estimates, hashes)
    dev = build_manifest("development", estimates, hashes)
    alternatives = {}
    for name, subset in (("384_all_tasks_two_reps", full["plan"]),
                         ("192_all_tasks_one_rep", [p for p in full["plan"] if p["repetition"] == 0]),
                         ("96_twelve_tasks_one_rep", [p for p in full["plan"] if p["repetition"] == 0 and p["stage"] in (1, 2)])):
        alternatives[name] = {key: sum(p["forecast"][key] for p in subset) for key in ("nominal_usd", "stressed_usd")}
    with (ROOT / f"cost_forecast_{VERSION}.json").open("x", encoding="utf-8") as stream:
        stream.write(json.dumps({"development": dev["forecast"], "heldout_alternatives": alternatives,
            "note": "No automatic downsizing. The 192-episode alternative has its own candidate manifest and requires explicit selection and development review. The 96-episode figure is a forecast only.",
            "available_after_development_cap_usd": str(PHASE_CAP - DEV_CAP)}, indent=2) + "\n")
    return dev, full


def load_manifest(path):
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    if manifest["source_sha256"] != source_hash() or manifest != build_manifest(
            manifest["collection"], one_repetition=manifest.get("variant") == "one_rep_alternative"):
        raise ValueError("Frozen Phase 3 manifest/source/task/settings/forecast mismatch")
    return manifest
