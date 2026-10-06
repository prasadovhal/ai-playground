"""Begin the frozen test only after every development record passes sanity checks."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time

import pandas as pd
import yaml

from .corpus import atomic_json
from .prepare import ROOT
from .rag import recorded, verify_models


def development_ready() -> tuple[bool, str | None]:
    status_file = ROOT / "results/pipeline_status.json"
    if not status_file.exists():
        return False, None
    status = json.loads(status_file.read_text(encoding="utf-8"))
    if status["status"] == "failed":
        return False, f"Development pipeline stage failed: {status['stage']}"
    if status.get("stage") != "development" or status.get("status") != "complete":
        return False, None
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    verify_models(config)
    config_hash = hashlib.sha256((ROOT / "config.yaml").read_bytes()).hexdigest()
    amendment = ROOT / "results/split_amendment.json"
    if amendment.exists():
        original = ROOT / "configs/config_original_90.yaml"
        prior = yaml.safe_load(original.read_text(encoding="utf-8"))
        old_hash = hashlib.sha256(original.read_bytes()).hexdigest()
        same_pipeline = dict(prior, split=None) == dict(config, split=None)
        if not same_pipeline or prior["split"]["development"] != config["split"]["development"]:
            return False, "Development and final pipeline settings differ beyond the authorized test-size amendment"
        expected_development_hash = old_hash
        if config["split"]["final_test_per_difficulty"] != 15:
            return False, "Amended final split is not 15 per difficulty"
    else:
        expected_development_hash = config_hash
    rows = recorded(ROOT / "results/raw/development_queries.jsonl")
    retrieval = recorded(ROOT / "results/raw/development_retrieval.jsonl")
    expected = set(pd.read_csv(ROOT / "results/question_split.csv").query("split == 'development'").question_id)
    if set(rows) != expected or set(retrieval) != expected:
        return False, "Development record IDs do not match the assigned 30 questions"
    for qid in expected:
        row = rows[qid]
        if row["status"] not in {"success", "partial_evaluation_failed"} or retrieval[qid]["status"] != "success":
            return False, f"Development query failed before semantic evaluation: {qid}"
        if row["config_sha256"] != expected_development_hash:
            return False, f"Development configuration hash mismatch: {qid}"
        if row["generator_model"] != config["models"]["generator"] or \
           row["evaluator_model"] != config["models"]["evaluator"]:
            return False, f"Development model changed: {qid}"
        if len(row["retrieval"]["dense"]) != 20 or len(row["retrieval"]["bm25"]) != 20 or \
           len(row["retrieval"]["final"]) != 5:
            return False, f"Development ranking shape invalid: {qid}"
        if row.get("generated_answer") is None:
            return False, f"Development answer missing: {qid}"
        if row["status"] == "success" and row.get("qwen_evaluation") is None:
            return False, f"Development semantic result missing: {qid}"
        if row["status"] == "partial_evaluation_failed" and not row.get("qwen_evaluation_errors"):
            return False, f"Development semantic failure has no recorded error: {qid}"
    return True, None


def main() -> None:
    marker = ROOT / "results/autorun_final_status.json"
    while True:
        ready, problem = development_ready()
        if problem:
            atomic_json(marker, {"status": "stopped", "reason": problem})
            raise RuntimeError(problem)
        if ready:
            break
        time.sleep(30)
    atomic_json(marker, {"status": "development_validated", "question_count": 30,
                         "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    with (ROOT / "logs/autorun_final.log").open("a", encoding="utf-8") as output:
        process = subprocess.run([sys.executable, "-B", "-m", "src.orchestrate", "final"],
                                 cwd=ROOT, stdout=output, stderr=subprocess.STDOUT)
    atomic_json(marker, {"status": "complete" if process.returncode == 0 else "failed",
                         "exit_code": process.returncode,
                         "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    if process.returncode:
        raise RuntimeError("Final pipeline failed; inspect results/pipeline_status.json")


if __name__ == "__main__":
    main()
