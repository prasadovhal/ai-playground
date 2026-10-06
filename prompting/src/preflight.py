"""Validate the fixed dataset, normalized corpus, chunks, and evidence map."""
from __future__ import annotations

import json

import pandas as pd
import yaml

from .corpus import atomic_json
from .prepare import ROOT
from .rag import verify_models


def validate() -> dict:
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    verify_models(config)
    split = pd.read_csv(ROOT / "results/question_split.csv")
    assigned = pd.read_csv(ROOT / "results/difficulty_assignment.csv")
    documents = pd.read_csv(ROOT / "results/raw/document_processing.csv")
    mapping = json.loads((ROOT / "results/gold_chunk_mapping.json").read_text(encoding="utf-8"))
    chunk_count = 0
    oversize = 0
    empty = 0
    documents_in_chunks = set()
    for line in (ROOT / "data/chunks.jsonl").open(encoding="utf-8"):
        chunk = json.loads(line)
        chunk_count += 1
        oversize += chunk["token_count"] > config["chunking"]["target_tokens"]
        empty += not bool(chunk["text"].strip())
        documents_in_chunks.add(chunk["document_id"])
    test_ids = set(split.loc[split.split == "test", "question_id"])
    development_ids = set(split.loc[split.split == "development", "question_id"])
    per_difficulty = config["split"]["final_test_per_difficulty"]
    expected_test = 3 * per_difficulty
    checks = {"150_questions": len(split) == 150 and split.question_id.nunique() == 150,
              "expected_test_and_development": len(test_ids) == expected_test and
                                               len(development_ids) == config["split"]["development"],
              "expected_each_difficulty": assigned.difficulty.value_counts().to_dict() ==
                                          {label: per_difficulty for label in ["easy", "medium", "hard"]},
              "disjoint_development_test": not bool(test_ids & development_ids),
              "all_368_pdfs_parsed": len(documents) == 368 and documents.status.eq("complete").all(),
              "all_368_pdfs_chunked": len(documents_in_chunks) == 368,
              "no_oversize_or_empty_chunks": oversize == 0 and empty == 0,
              "all_150_gold_mappings_exist": len(mapping) == 150,
              "no_unmapped_test_question": all(mapping[qid]["gold_chunk_ids"] for qid in test_ids)}
    checks = {key: bool(value) for key, value in checks.items()}
    result = {"valid": all(checks.values()), "checks": checks, "chunk_count": chunk_count,
              "oversize_chunks": oversize, "empty_chunks": empty,
              "parsed_pages": int(documents.source_pages.sum()),
              "pages_without_extractable_text": int(documents.empty_pages.sum()),
              "parser_counts": documents.parser.value_counts().to_dict(),
              "test_uncertain_evidence_items": sum(e["mapping_confidence"] in {"low", "none"}
                                                   for qid in test_ids for e in mapping[qid]["evidence"]),
              "test_questions_with_uncertain_mapping": sum(any(e["mapping_confidence"] in {"low", "none"}
                                                               for e in mapping[qid]["evidence"]) for qid in test_ids)}
    atomic_json(ROOT / "results/preflight_validation.json", result)
    if not result["valid"]:
        raise RuntimeError("Preflight validation failed: " + json.dumps(checks))
    return result


if __name__ == "__main__":
    print(json.dumps(validate(), indent=2))
