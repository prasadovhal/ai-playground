"""Map benchmark evidence to corpus chunks without using retrieval results."""
from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from difflib import SequenceMatcher

import pandas as pd

from .corpus import atomic_json
from .index import load_chunks
from .prepare import ROOT


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:[.,%-][0-9]+)?", text.lower())


def normalized(text: str) -> str:
    return " ".join(words(text))


def overlap(a: list[str], b: list[str]) -> float:
    ca, cb = Counter(a), Counter(b)
    shared = sum(min(count, cb[token]) for token, count in ca.items())
    return shared / math.sqrt(max(1, len(a) * len(b)))


def build_mapping() -> dict:
    chunks = load_chunks()
    by_page = defaultdict(list)
    for chunk in chunks:
        by_page[(chunk["document_id"], chunk["page"])].append(chunk)
    questions = [json.loads(line) for line in (ROOT / "data/financebench_source/data/financebench_open_source.jsonl").open(encoding="utf-8")]
    mapped = {}
    uncertain = []
    for question in questions:
        evidence_rows = []
        union = set()
        for ordinal, evidence in enumerate(question["evidence"]):
            doc_name = evidence["doc_name"]
            page = int(evidence["evidence_page_num"]) + 1
            candidates = by_page.get((doc_name, page), [])
            page_offset = 0
            if not candidates:
                candidates = by_page.get((doc_name, page - 1), []) or by_page.get((doc_name, page + 1), [])
                page_offset = 1
            text = evidence["evidence_text"]
            norm = normalized(text)
            exact = [c for c in candidates if norm and norm in normalized(c["text"])]
            if exact:
                selected = exact
                method = "exact_normalized_substring"
                confidence = "high" if page_offset == 0 else "medium"
                score = 1.0
            elif candidates:
                query_words = words(text)
                scored = [(overlap(query_words, words(c["text"])), c) for c in candidates]
                scored.sort(key=lambda pair: (-pair[0], pair[1]["chunk_id"]))
                best = scored[0][0]
                selected = [c for value, c in scored if value >= max(0.12, best * 0.85)]
                selected = selected[:max(1, min(5, math.ceil(len(query_words) / 400)))]
                if best < 0.12:
                    ratio = [(SequenceMatcher(None, norm[:2500], normalized(c["text"])[:2500]).ratio(), c) for c in candidates]
                    ratio.sort(key=lambda pair: (-pair[0], pair[1]["chunk_id"]))
                    selected = [ratio[0][1]]
                    method = "fuzzy_sequence_fallback"
                    score = ratio[0][0]
                    confidence = "low"
                else:
                    method = "same_page_token_overlap"
                    score = best
                    confidence = "medium" if best >= 0.5 and page_offset == 0 else "low"
            else:
                selected = []
                method = "no_page_chunks"
                score = 0.0
                confidence = "none"
            ids = [c["chunk_id"] for c in selected]
            union.update(ids)
            record = {"ordinal": ordinal, "gold_evidence_text": text, "gold_document": doc_name,
                      "gold_page_zero_indexed": evidence["evidence_page_num"],
                      "gold_chunk_ids": ids, "mapping_method": method,
                      "mapping_confidence": confidence, "mapping_score": score,
                      "page_offset_used": page_offset}
            evidence_rows.append(record)
            if confidence in {"low", "none"}:
                uncertain.append({"question_id": question["financebench_id"], "evidence_ordinal": ordinal,
                                  "document": doc_name, "page": page, "method": method,
                                  "score": score, "mapped_chunk_ids": json.dumps(ids)})
        mapped[question["financebench_id"]] = {"question_id": question["financebench_id"],
                                                "evidence": evidence_rows,
                                                "gold_chunk_ids": sorted(union)}
    atomic_json(ROOT / "results/gold_chunk_mapping.json", mapped)
    pd.DataFrame(uncertain, columns=["question_id", "evidence_ordinal", "document", "page", "method", "score", "mapped_chunk_ids"]).to_csv(
        ROOT / "results/gold_mapping_uncertain.csv", index=False)
    summary = {"questions": len(mapped), "evidence_items": sum(len(x["evidence"]) for x in mapped.values()),
               "unmapped_questions": sum(not x["gold_chunk_ids"] for x in mapped.values()),
               "uncertain_evidence_items": len(uncertain),
               "methods": dict(Counter(e["mapping_method"] for x in mapped.values() for e in x["evidence"]))}
    atomic_json(ROOT / "results/gold_mapping_summary.json", summary)
    return summary


if __name__ == "__main__":
    print(json.dumps(build_mapping(), indent=2))
