"""User-requested 90-to-45 test reduction before any final-test inference."""
from __future__ import annotations

import hashlib
import json
import shutil
import time

import pandas as pd
import yaml

from .corpus import atomic_json
from .prepare import ROOT, prepare
from .rag import recorded


def amend() -> dict:
    results = ROOT / "results"
    forbidden = [results / "frozen_configuration.json", results / "raw/test_retrieval.jsonl",
                 results / "raw/test_generation.jsonl", results / "raw/test_queries.jsonl",
                 results / "asd_ste/pairs.jsonl"]
    if any(path.exists() for path in forbidden):
        raise RuntimeError("Final-test inference or freeze has begun; refusing split amendment")
    current = ROOT / "config.yaml"
    cfg = yaml.safe_load(current.read_text(encoding="utf-8"))
    if cfg["split"]["final_test_per_difficulty"] != 30:
        raise RuntimeError("Expected original 30-per-difficulty configuration")
    old_split = pd.read_csv(results / "question_split.csv")
    old_test = set(old_split.loc[old_split.split == "test", "question_id"])
    old_development = set(old_split.loc[old_split.split == "development", "question_id"])
    if len(old_test) != 90 or len(old_development) != 30:
        raise RuntimeError("Original 90/30 assignment is unavailable")
    if len(recorded(results / "raw/development_queries.jsonl")) != 30:
        raise RuntimeError("Wait for the 30 development evaluations to finish before amendment")
    archives = [("difficulty_assignment.csv", "difficulty_assignment_original_90.csv"),
                ("question_split.csv", "question_split_original_90.csv"),
                ("dataset_manifest.json", "dataset_manifest_original_90.json")]
    for source, archive in archives:
        backup = results / archive
        if backup.exists():
            if backup.read_bytes() != (results / source).read_bytes():
                raise RuntimeError(f"Original archive differs from current source: {archive}")
        else:
            shutil.copy2(results / source, backup)
    original_config = ROOT / "configs/config_original_90.yaml"
    if original_config.exists():
        if original_config.read_bytes() != current.read_bytes():
            raise RuntimeError("Original configuration archive differs")
    else:
        shutil.copy2(current, original_config)
    old_hash = hashlib.sha256(current.read_bytes()).hexdigest()
    text = current.read_text(encoding="utf-8")
    needle = "  final_test_per_difficulty: 30\n"
    replacement = "  candidate_pool_per_difficulty: 30\n  final_test_per_difficulty: 15\n"
    if text.count(needle) != 1:
        raise RuntimeError("Expected one original split setting")
    current.write_text(text.replace(needle, replacement), encoding="utf-8")
    try:
        manifest = prepare(current, force=True)
        new_split = pd.read_csv(results / "question_split.csv")
        new_test = set(new_split.loc[new_split.split == "test", "question_id"])
        new_development = set(new_split.loc[new_split.split == "development", "question_id"])
        if len(new_test) != 45 or not new_test <= old_test or new_development != old_development:
            raise RuntimeError("Amended test is not a 45-question subset with unchanged development IDs")
    except Exception:
        current.write_bytes(original_config.read_bytes())
        for source, archive in archives:
            shutil.copy2(results / archive, results / source)
        raise
    record = {"amended_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "reason": "User requested 15 easy, 15 medium, and 15 hard final-test questions to reduce local runtime",
              "old_test_count": 90, "new_test_count": 45,
              "old_per_difficulty": 30, "new_per_difficulty": 15,
              "development_count": 30, "development_ids_unchanged": True,
              "new_test_is_subset_of_original_test": True,
              "no_final_test_inference_or_freeze_before_amendment": True,
              "old_config_sha256": old_hash,
              "new_config_sha256": hashlib.sha256(current.read_bytes()).hexdigest(),
              "original_test_ids": sorted(old_test), "final_test_ids": sorted(new_test),
              "selection": "numpy.default_rng(42): choose original 30 per class, choose original 30 development outside pool, then choose 15 per class from original pool",
              "limitation": "Halving the test size widens confidence intervals and reduces power for paired tests"}
    atomic_json(results / "split_amendment.json", record)
    return record


if __name__ == "__main__":
    print(json.dumps(amend(), indent=2))
