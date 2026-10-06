"""Compute complete-corpus retrieval once per split, before Ollama generation."""
from __future__ import annotations

import argparse
import hashlib
import json

import pandas as pd
import yaml

from .index import Retrieval
from .prepare import ROOT
from .rag import append_jsonl, recorded


def run(split: str) -> None:
    config_file = ROOT / "config.yaml"
    cfg = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    config_hash = hashlib.sha256(config_file.read_bytes()).hexdigest()
    if split == "test":
        frozen = json.loads((ROOT / "results/frozen_configuration.json").read_text(encoding="utf-8"))
        if frozen["config_sha256"] != config_hash:
            raise RuntimeError("Retrieval configuration changed after freeze")
        for relative, digest in frozen["files"].items():
            if hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() != digest:
                raise RuntimeError(f"Frozen artifact changed: {relative}")
    assignments = pd.read_csv(ROOT / "results/question_split.csv")
    ids = sorted(assignments.loc[assignments.split == split, "question_id"])
    expected = cfg["split"]["development"] if split == "development" else 3 * cfg["split"]["final_test_per_difficulty"]
    if len(ids) != expected:
        raise RuntimeError(f"Expected {expected} {split} IDs, found {len(ids)}")
    questions = {row["financebench_id"]: row["question"] for row in
                 (json.loads(line) for line in (ROOT / cfg["source"]["questions"]).open(encoding="utf-8"))}
    target = ROOT / f"results/raw/{split}_retrieval.jsonl"
    done = recorded(target)
    pending = [qid for qid in ids if qid not in done]
    if not pending:
        print(f"No pending {split} retrieval", flush=True)
        return
    retriever = Retrieval(cfg)
    for position, qid in enumerate(pending, start=1):
        try:
            search = retriever.search(questions[qid])
            record = {"question_id": qid, "split": split, "question": questions[qid],
                      "config_sha256": config_hash, "search": search, "status": "success"}
        except Exception as exc:
            record = {"question_id": qid, "split": split, "question": questions[qid],
                      "config_sha256": config_hash, "search": None,
                      "status": "failed", "error": repr(exc)}
        append_jsonl(target, record)
        print(f"{split} retrieval {position}/{len(pending)} {qid} {record['status']}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("split", choices=["development", "test"])
    args = parser.parse_args()
    run(args.split)
