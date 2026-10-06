"""Freeze all test-affecting artifacts after development and before final inference."""
from __future__ import annotations

import hashlib
import json
import time

from .corpus import atomic_json
from .prepare import ROOT
from .rag import recorded


def file_hash(relative: str) -> str:
    return hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()


def freeze() -> dict:
    development = recorded(ROOT / "results/raw/development_queries.jsonl")
    if len(development) != 30:
        raise RuntimeError(f"Development set incomplete: {len(development)}/30")
    files = {key: file_hash(key) for key in ["config.yaml", "src/prepare.py", "src/corpus.py",
                                           "src/index.py", "src/gold.py", "src/metrics.py", "src/rag.py",
                                           "src/asd.py", "src/summarize.py",
                                           "src/retrieval_stage.py",
                                           "results/difficulty_assignment.csv",
                                           "results/question_split.csv", "data/chunks.jsonl",
                                           "data/index/dense.faiss", "data/index/bm25.pkl",
                                           "results/gold_chunk_mapping.json",
                                           "results/split_amendment.json",
                                           "results/difficulty_assignment_original_90.csv",
                                           "results/question_split_original_90.csv",
                                           "configs/config_original_90.yaml"]}
    frozen = {"frozen_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "config_sha256": files["config.yaml"], "files": files,
              "development_questions": sorted(development),
              "development_status_counts": {status: sum(r["status"] == status for r in development.values())
                                            for status in sorted(set(r["status"] for r in development.values()))},
              "rule": "No test-affecting configuration or artifact may change after this point"}
    target = ROOT / "results/frozen_configuration.json"
    if target.exists():
        old = json.loads(target.read_text(encoding="utf-8"))
        if old["files"] != files:
            raise RuntimeError("Existing frozen configuration differs")
        return old
    atomic_json(target, frozen)
    return frozen


if __name__ == "__main__":
    print(json.dumps(freeze(), indent=2))
