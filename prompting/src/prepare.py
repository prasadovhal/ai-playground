"""Inspect FinanceBench and freeze difficulty before any RAG inference."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
CALC = re.compile(r"\b(growth|change|difference|increase|decrease|ratio|margin|percent|percentage|average|total of|how much more|how much less|compare|compared|between|versus|vs\.?|calculate|compute)\b", re.I)
SYNTHESIS = re.compile(r"\b(why|what drove|explain|impact|effect|conclude|assess|could|would|should|based on .* and|drivers|contribute|more efficient|less efficient)\b", re.I)
COMPARE = re.compile(r"\b(highest|lowest|largest|biggest|most|least|best|among|more than|less than|substantially more|similar|stable trend|improving|retain)\b", re.I)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def classify(row: dict) -> tuple[str, str]:
    """Structural category; never consult answer text or model output."""
    question = row["question"]
    reasoning = str(row.get("question_reasoning") or "none").lower()
    evidence_count = len(row["evidence"])
    arithmetic = bool(CALC.search(question))
    synthesis = bool(SYNTHESIS.search(question))
    comparison = bool(COMPARE.search(question))
    direct_industry = bool(re.search(r"^what industry does\b", question, re.I))
    complex_condition = "if " in question.lower() and any(x in question.lower() for x in ["explain why", "not a useful", "not relevant"])
    causal_question = bool(re.search(r"\b(what drove|why did|what caused|main drivers|impact of)\b", question, re.I))
    multi_year_list = bool(re.search(r"\bmajor acquisitions\b", question, re.I)) and len(re.findall(r"FY\s?20\d\d", question, re.I)) > 1
    forecast_change = bool(re.search(r"\bproduction rate changes\b", question, re.I))
    multi_period = len(set(re.findall(r"(?:FY|Q\d of FY)\s?20\d\d", question, re.I))) > 1
    total_from_multiple = "total amount" in question.lower() and "agreements" in question.lower()
    logical = "logical reasoning" in reasoning
    numerical = "numerical reasoning" in reasoning
    extraction = "information extraction" in reasoning
    if direct_industry and evidence_count == 1:
        return "easy", "Direct industry lookup; no arithmetic, despite broad reasoning metadata."
    if evidence_count >= 3:
        return "hard", f"{evidence_count} benchmark evidence items require synthesis across multiple facts."
    if complex_condition or causal_question or re.search(r"events?.*increased net income", question, re.I):
        return "hard", f"Causal or conditional interpretation in question; reasoning={reasoning}; {evidence_count} evidence item(s)."
    if logical:
        return "hard", f"Reasoning metadata includes logical reasoning; {evidence_count} evidence item(s)."
    if evidence_count >= 2 and (numerical or synthesis):
        return "hard", f"{evidence_count} evidence items with numerical or interpretive reasoning."
    if numerical or arithmetic or comparison or multi_year_list or multi_period or total_from_multiple or forecast_change:
        return "medium", f"Numerical/comparison structure; reasoning={reasoning}; {evidence_count} evidence item(s)."
    if evidence_count == 1 and (extraction or reasoning == "none") and not synthesis:
        return "easy", f"Direct single-evidence lookup without an arithmetic or synthesis cue; reasoning={reasoning}."
    return "unassigned", f"Metadata and question structure do not justify a confident category; reasoning={reasoning}; {evidence_count} evidence item(s)."


def prepare(config: Path, *, force: bool = False) -> dict:
    cfg = yaml.safe_load(config.read_text(encoding="utf-8"))
    source = ROOT / cfg["source"]["questions"]
    docs_path = ROOT / cfg["source"]["document_metadata"]
    pdf_dir = ROOT / cfg["source"]["pdf_directory"]
    rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]
    docs = [json.loads(line) for line in docs_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    pdfs = sorted(pdf_dir.glob("*.pdf"))
    if len(rows) != cfg["source"]["expected_questions"] or len(pdfs) != cfg["source"]["expected_pdfs"]:
        raise RuntimeError(f"Source count changed: {len(rows)} questions, {len(pdfs)} PDFs")
    ids = [r["financebench_id"] for r in rows]
    if len(set(ids)) != len(ids):
        raise RuntimeError("Duplicate question IDs")
    for r in rows:
        if not all(k in r for k in ["question", "answer", "evidence", "question_type", "question_reasoning"]):
            raise RuntimeError(f"Required metadata missing for {r.get('financebench_id')}")
    classified = []
    for row in rows:
        difficulty, reason = classify(row)
        classified.append({"question_id": row["financebench_id"], "question": row["question"],
                           "difficulty": difficulty, "question_type": row["question_type"],
                           "reasoning_type": row["question_reasoning"],
                           "gold_evidence_count": len(row["evidence"]), "difficulty_reason": reason})
    frame = pd.DataFrame(classified).sort_values("question_id").reset_index(drop=True)
    counts = frame.difficulty.value_counts().to_dict()
    required = cfg["split"]["final_test_per_difficulty"]
    pool_size = cfg["split"].get("candidate_pool_per_difficulty", required)
    if required > pool_size or any(counts.get(label, 0) < pool_size for label in ["easy", "medium", "hard"]):
        raise RuntimeError(f"Insufficient structurally suitable questions for balanced test: {counts}")
    rng = np.random.default_rng(cfg["seed"])
    split = dict.fromkeys(ids, "unused")
    pool_ids = {}
    for label in ["easy", "medium", "hard"]:
        candidates = sorted(frame.loc[frame.difficulty == label, "question_id"].tolist())
        pool_ids[label] = rng.choice(candidates, size=pool_size, replace=False).tolist()
    original_pool = {qid for members in pool_ids.values() for qid in members}
    development_candidates = sorted(qid for qid in ids if qid not in original_pool)
    for qid in rng.choice(development_candidates, size=cfg["split"]["development"], replace=False).tolist():
        split[qid] = "development"
    for label in ["easy", "medium", "hard"]:
        selected = (pool_ids[label] if required == pool_size else
                    rng.choice(sorted(pool_ids[label]), size=required, replace=False).tolist())
        for qid in selected:
            split[qid] = "test"
    frame["split"] = frame.question_id.map(split)
    test = frame[frame.split == "test"].drop(columns="split")
    development = frame[frame.split == "development"].drop(columns="split")
    if len(test) != 3 * required or test.difficulty.value_counts().to_dict() != {label: required for label in ["easy", "medium", "hard"]} or len(development) != cfg["split"]["development"]:
        raise RuntimeError("Split count validation failed")
    result_dir = ROOT / "results"
    final_file = result_dir / "difficulty_assignment.csv"
    split_file = result_dir / "question_split.csv"
    if final_file.exists() and not force:
        prior = pd.read_csv(final_file)
        if not prior.equals(test.reset_index(drop=True)):
            raise RuntimeError("Frozen difficulty assignment differs; refusing to overwrite")
    test.to_csv(final_file, index=False)
    frame.to_csv(split_file, index=False)
    pdf_names = {p.stem for p in pdfs}
    missing_question_docs = sorted({r["doc_name"] for r in rows} - pdf_names)
    manifest = {"source_revision": cfg["source"]["revision"], "source_question_sha256": digest(source),
                "source_document_metadata_sha256": digest(docs_path), "question_count": len(rows),
                "pdf_count": len(pdfs), "pdf_bytes": sum(p.stat().st_size for p in pdfs),
                "document_metadata_count": len(docs), "difficulty_eligible_counts": counts,
                "test_counts": test.difficulty.value_counts().to_dict(),
                "development_count": len(development), "missing_question_source_pdfs": missing_question_docs,
                "difficulty_assignment_sha256": digest(final_file), "question_split_sha256": digest(split_file),
                "candidate_pool_per_difficulty": pool_size,
                "selection_algorithm": "numpy.default_rng(42), sorted IDs, initial pool per class, development from outside pool, then final test subset from pool"}
    (result_dir / "dataset_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    print(json.dumps(prepare(args.config, force=args.force), indent=2))
