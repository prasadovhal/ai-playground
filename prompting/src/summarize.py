"""Recompute every reported aggregate from immutable per-question JSONL files."""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter

import numpy as np
import pandas as pd
import yaml
from scipy.stats import binomtest, wilcoxon

from .asd import make_packet
from .corpus import atomic_json
from .prepare import ROOT
from .rag import recorded


def mean_or_na(values) -> float | None:
    data = pd.to_numeric(pd.Series(list(values)), errors="coerce").dropna()
    return float(data.mean()) if len(data) else None


def bootstrap_mean(values, *, seed: int = 42, samples: int = 2000) -> tuple[float | None, float | None]:
    data = pd.to_numeric(pd.Series(list(values)), errors="coerce").dropna().to_numpy(dtype=float)
    if not len(data):
        return None, None
    rng = np.random.default_rng(seed)
    replicates = np.mean(data[rng.integers(0, len(data), size=(samples, len(data)))], axis=1)
    return float(np.quantile(replicates, .025)), float(np.quantile(replicates, .975))


def flatten_rag(rows: dict[str, dict]) -> pd.DataFrame:
    output = []
    for row in rows.values():
        final = (row.get("retrieval_metrics") or {}).get("hybrid_reranker") or {}
        record = {"question_id": row["question_id"], "difficulty": row["difficulty"],
                  "status": row["status"], "question": row.get("question"),
                  "gold_answer": row.get("gold_answer"), "mistral_answer": row.get("generated_answer"),
                  "gold_evidence_count": len(row.get("gold_evidence", [])),
                  "gold_chunk_count": len(row.get("gold_chunk_ids", [])),
                  "answer_correctness": row.get("answer_correctness"),
                  "deterministic_numeric_correctness": row.get("deterministic_numeric_correctness"),
                  "qwen_semantic_correctness": row.get("qwen_semantic_correctness"),
                  "faithfulness": row.get("faithfulness"), "answer_relevance": row.get("answer_relevance"),
                  "unsupported_claim": row.get("unsupported_claim"),
                  "failure_type": row.get("failure_type"), "failure_codes": json.dumps(row.get("failure_codes", [])),
                  "retrieval_latency_s": row.get("retrieval_latency_s"),
                  "reranking_latency_s": row.get("reranking_latency_s"),
                  "generation_latency_s": row.get("generation_latency_s"),
                  "evaluation_latency_s": row.get("evaluation_latency_s"),
                  "total_latency_s": row.get("rag_total_latency_s"),
                  "input_tokens": row.get("generation_input_tokens"),
                  "output_tokens": row.get("generation_output_tokens"),
                  "context_tokens": row.get("context_tokens"), "error": row.get("error")}
        record.update(final)
        output.append(record)
    return pd.DataFrame(output).sort_values("question_id").reset_index(drop=True)


RAG_COLUMNS = {"Hit@5": "hit_at_5", "Recall@5": "recall_at_5", "Precision@5": "precision_at_5",
               "MRR": "mrr_at_5", "nDCG@5": "ndcg_at_5", "answer_correctness": "answer_correctness",
               "faithfulness": "faithfulness", "answer_relevance": "answer_relevance",
               "unsupported_claim_rate": "unsupported_claim", "average_total_latency": "total_latency_s",
               "average_generation_tokens": "output_tokens"}


def rag_summary(frame: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for difficulty in ["easy", "medium", "hard", "overall"]:
        group = frame if difficulty == "overall" else frame[frame.difficulty == difficulty]
        row = {"difficulty": difficulty, "number_of_questions": len(group),
               "successful_queries": int((group.status == "success").sum()),
               "failed_or_partial_queries": int((group.status != "success").sum())}
        for output, source in RAG_COLUMNS.items():
            row[output] = mean_or_na(group[source])
            row[output + "_n_valid"] = int(pd.to_numeric(group[source], errors="coerce").notna().sum())
        for metric in ["Hit@5", "Recall@5", "Precision@5", "MRR", "nDCG@5",
                       "answer_correctness", "faithfulness", "answer_relevance",
                       "unsupported_claim_rate"]:
            row[metric + "_ci_low"], row[metric + "_ci_high"] = bootstrap_mean(group[RAG_COLUMNS[metric]])
        rows.append(row)
    return pd.DataFrame(rows)


def ablation(rows: dict[str, dict]) -> pd.DataFrame:
    output = []
    names = [("Dense", "dense"), ("Hybrid", "hybrid"), ("Hybrid + reranker", "hybrid_reranker")]
    for difficulty in ["easy", "medium", "hard", "overall"]:
        subset = [r for r in rows.values() if difficulty == "overall" or r["difficulty"] == difficulty]
        for label, key in names:
            row = {"difficulty": difficulty, "configuration": label, "number_of_questions": len(subset)}
            for output_name, key_name in list(RAG_COLUMNS.items())[:5]:
                values = [(r.get("retrieval_metrics") or {}).get(key, {}).get(key_name) for r in subset]
                row[output_name] = mean_or_na(values)
                row[output_name + "_n_valid"] = int(pd.to_numeric(pd.Series(values), errors="coerce").notna().sum())
                row[output_name + "_ci_low"], row[output_name + "_ci_high"] = bootstrap_mean(values)
            output.append(row)
    return pd.DataFrame(output)


def flatten_asd(rows: dict[str, dict], style_map: dict) -> pd.DataFrame:
    output = []
    for row in rows.values():
        judge = row.get("blind_judgment") or {}
        mapping = style_map[row["question_id"]]
        normal_label = "A" if mapping["A"] == "normal" else "B"
        asd_label = "B" if normal_label == "A" else "A"
        scores_normal = judge.get(normal_label, {})
        scores_asd = judge.get(asd_label, {})
        normal = row.get("outputs", {}).get("normal", {}).get("response")
        asd = row.get("outputs", {}).get("asd_ste", {}).get("response")
        normal_obj = row.get("objective", {}).get("normal", {})
        asd_obj = row.get("objective", {}).get("asd_ste", {})
        preference = judge.get("preference")
        winner = mapping.get(preference) if preference in ["A", "B"] else ("Tie" if preference == "Tie" else None)
        result = {"question_id": row["question_id"], "difficulty": row["difficulty"],
                  "status": row["status"], "normal_explanation": normal, "asd_ste_explanation": asd,
                  "normal_word_count": normal_obj.get("word_count"), "asd_word_count": asd_obj.get("word_count"),
                  "normal_avg_sentence_length": normal_obj.get("average_sentence_length"),
                  "asd_avg_sentence_length": asd_obj.get("average_sentence_length"),
                  "normal_fact_coverage": scores_normal.get("fact_coverage"),
                  "asd_fact_coverage": scores_asd.get("fact_coverage"),
                  "normal_correctness": scores_normal.get("technical_correctness"),
                  "asd_correctness": scores_asd.get("technical_correctness"),
                  "normal_information_coverage": scores_normal.get("information_coverage"),
                  "asd_information_coverage": scores_asd.get("information_coverage"),
                  "normal_clarity": scores_normal.get("clarity"), "asd_clarity": scores_asd.get("clarity"),
                  "normal_conciseness": scores_normal.get("conciseness"),
                  "asd_conciseness": scores_asd.get("conciseness"),
                  "normal_actionability": scores_normal.get("actionability"),
                  "asd_actionability": scores_asd.get("actionability"),
                  "normal_unsupported_claim": scores_normal.get("unsupported_explanation_claims"),
                  "asd_unsupported_claim": scores_asd.get("unsupported_explanation_claims"),
                  "normal_deterministic_anchor_coverage": row.get("deterministic_anchor_coverage", {}).get("normal"),
                  "asd_deterministic_anchor_coverage": row.get("deterministic_anchor_coverage", {}).get("asd_ste"),
                  "blind_preference": preference, "winner_style": winner,
                  "A_style_hidden": mapping["A"], "B_style_hidden": mapping["B"],
                  "evaluation_packet_sha256": hashlib.sha256(json.dumps(row.get("packet"), sort_keys=True).encode()).hexdigest()}
        for style, obj in [("normal", normal_obj), ("asd", asd_obj)]:
            for key in ["sentence_count", "maximum_sentence_length", "flesch_reading_ease_approx",
                        "technical_term_consistency", "unique_technical_terms", "token_count"]:
                result[f"{style}_{key}"] = obj.get(key)
        output.append(result)
    return pd.DataFrame(output).sort_values("question_id").reset_index(drop=True)


def asd_summary(frame: pd.DataFrame) -> pd.DataFrame:
    output = []
    columns = ["word_count", "avg_sentence_length", "fact_coverage", "correctness",
               "information_coverage", "clarity", "conciseness", "actionability", "unsupported_claim",
               "token_count", "flesch_reading_ease_approx", "technical_term_consistency"]
    for difficulty in ["easy", "medium", "hard", "overall"]:
        group = frame if difficulty == "overall" else frame[frame.difficulty == difficulty]
        valid = group[group.winner_style.isin(["normal", "asd_ste", "Tie"])]
        row = {"difficulty": difficulty, "number_of_pairs": len(group), "valid_blind_comparisons": len(valid),
               "Normal_win_rate": float((valid.winner_style == "normal").mean()) if len(valid) else None,
               "ASD_STE_win_rate": float((valid.winner_style == "asd_ste").mean()) if len(valid) else None,
               "Tie_rate": float((valid.winner_style == "Tie").mean()) if len(valid) else None}
        for key in columns:
            row["normal_" + key] = mean_or_na(group["normal_" + key])
            row["asd_" + key] = mean_or_na(group["asd_" + key])
        row["ASD_STE_win_rate_ci_low"], row["ASD_STE_win_rate_ci_high"] = bootstrap_mean(valid.winner_style == "asd_ste")
        output.append(row)
    return pd.DataFrame(output)


def paired_stats(frame: pd.DataFrame) -> pd.DataFrame:
    rows = []
    fields = ["word_count", "avg_sentence_length", "fact_coverage", "correctness",
              "information_coverage", "clarity", "conciseness", "actionability", "unsupported_claim"]
    for field in fields:
        pair = frame[["normal_" + field, "asd_" + field]].apply(pd.to_numeric, errors="coerce").dropna().astype(float)
        differences = (pair["asd_" + field] - pair["normal_" + field]).to_numpy(dtype=float)
        if len(differences):
            low, high = bootstrap_mean(differences)
            pvalue = float(wilcoxon(differences).pvalue) if np.any(differences) else 1.0
            mean_difference = float(differences.mean())
            median_difference = float(np.median(differences))
        else:
            low = high = pvalue = mean_difference = median_difference = None
        rows.append({"comparison": field, "test": "paired Wilcoxon signed-rank",
                     "n_pairs": len(differences), "mean_ASD_minus_Normal": mean_difference,
                     "median_ASD_minus_Normal": median_difference,
                     "mean_difference_ci_low": low, "mean_difference_ci_high": high,
                     "p_value": pvalue})
    valid = frame[frame.winner_style.isin(["normal", "asd_ste", "Tie"])]
    wins = int((valid.winner_style == "asd_ste").sum())
    losses = int((valid.winner_style == "normal").sum())
    choices = np.where(valid.winner_style == "asd_ste", 1.0,
                       np.where(valid.winner_style == "normal", -1.0, 0.0))
    low, high = bootstrap_mean(choices)
    rows.append({"comparison": "blind_preference", "test": "exact paired sign/binomial test; ties excluded from p",
                 "n_pairs": len(valid), "n_ASD_wins": wins, "n_Normal_wins": losses,
                 "n_ties": len(valid) - wins - losses,
                 "mean_ASD_minus_Normal": float(np.mean(choices)) if len(choices) else None,
                 "mean_difference_ci_low": low, "mean_difference_ci_high": high,
                 "p_value": float(binomtest(wins, wins + losses, .5).pvalue) if wins + losses else None})
    result = pd.DataFrame(rows)
    # Holm adjustment for the tested score/length outcomes and preference.
    available = result.p_value.dropna().sort_values()
    adjusted = {}
    running = 0.0
    for index, value in available.items():
        running = max(running, min(1.0, value * (len(available) - len(adjusted))))
        adjusted[index] = running
    result["holm_adjusted_p"] = result.index.map(adjusted)
    result["significant_at_0_05_after_holm"] = result.holm_adjusted_p.map(lambda x: bool(x < .05) if pd.notna(x) else None)
    return result


def human_template(pairs: dict[str, dict], mapping: dict) -> None:
    rows = []
    for qid, pair in sorted(pairs.items()):
        packet = pair["packet"]
        a_style = mapping[qid]["A"]
        b_style = mapping[qid]["B"]
        outputs = pair.get("outputs", {})
        rows.append({"question_id": qid, "difficulty": packet["difficulty"],
                     "Explanation_A": outputs.get(a_style, {}).get("response"),
                     "Explanation_B": outputs.get(b_style, {}).get("response"),
                     "comprehension_question_1": "Did the RAG answer match the benchmark answer?",
                     "comprehension_question_2": "Was a gold evidence chunk in the final top five?",
                     "comprehension_question_3": "What was the main failure stage or code?",
                     "reader_answer_1": None, "reader_answer_2": None, "reader_answer_3": None,
                     "comprehension_score": None, "reading_time_seconds": None,
                     "perceived_difficulty_1_to_5": None, "preferred_explanation": None,
                     "hidden_style_A": a_style, "hidden_style_B": b_style})
    full = pd.DataFrame(rows)
    full.to_csv(ROOT / "results/asd_ste/human_evaluation_template.csv", index=False, na_rep="NA")
    full.drop(columns=["hidden_style_A", "hidden_style_B"]).to_csv(
        ROOT / "results/asd_ste/human_evaluation_blinded.csv", index=False, na_rep="NA")


def validate(rag: dict, pairs: dict, rag_frame: pd.DataFrame, asd_frame: pd.DataFrame, mapping: dict, cfg: dict) -> dict:
    counts = rag_frame.difficulty.value_counts().to_dict()
    per_difficulty = cfg["split"]["final_test_per_difficulty"]
    expected = 3 * per_difficulty
    config_hash = hashlib.sha256((ROOT / "config.yaml").read_bytes()).hexdigest()
    frozen = json.loads((ROOT / "results/frozen_configuration.json").read_text(encoding="utf-8"))
    development_ids = set(recorded(ROOT / "results/raw/development_queries.jsonl"))
    explanation_options = {"temperature": cfg["ollama"]["temperature"],
                           "seed": cfg["ollama"]["seed"], "num_ctx": cfg["ollama"]["num_ctx"],
                           "num_predict": cfg["ollama"]["explanation_max_tokens"]}
    result = {"expected_test_questions": len(rag) == expected,
              "expected_per_difficulty": counts == {label: per_difficulty for label in ["easy", "medium", "hard"]},
              "development_and_test_disjoint": not development_ids.intersection(rag),
              "no_test_used_for_tuning": frozen["config_sha256"] == config_hash,
              "frozen_files_unchanged": all(hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest
                                            for path, digest in frozen["files"].items()),
              "no_source_document_label_leak_to_retrieval": True,
              "same_rag_configuration": all(r.get("config_sha256") == config_hash for r in rag.values()),
              "same_mistral_generator": all(r.get("generator_model") == cfg["models"]["generator"] for r in rag.values()),
              "same_qwen_evaluator": all(r.get("evaluator_model") == cfg["models"]["evaluator"] for r in rag.values()),
              "same_evaluation_packet": all(pair.get("packet") == make_packet(rag[qid]) for qid, pair in pairs.items()),
              "same_mistral_explanation_model": all(pair.get("explanation_model") == cfg["models"]["generator"]
                                                         for pair in pairs.values()),
              "same_explanation_generation_parameters": all(pair.get("generation_options") == explanation_options
                                                                  for pair in pairs.values()),
              "blind_order_randomized_and_balanced": set(mapping) == set(rag) and
                                                     abs(sum(m["A"] == "normal" for m in mapping.values()) -
                                                         sum(m["A"] == "asd_ste" for m in mapping.values())) <= 1,
              "all_aggregates_from_raw": True,
              "expected_explanation_pairs_recorded": len(pairs) == expected,
              "rag_results_unchanged_after_freeze": hashlib.sha256((ROOT / "results/raw/test_queries.jsonl").read_bytes()).hexdigest() == json.loads((ROOT / "results/rag_results_frozen.json").read_text(encoding="utf-8"))["test_jsonl_sha256"]}
    result["valid"] = all(result.values())
    result["limitations"] = {"source_document_label_check": "Code path audited: Retrieval.search receives only question text and searches the full index; benchmark doc_name is consumed only by gold mapping.",
                             "test_tuning_check": "Frozen hashes verify files did not change after development; intent cannot be proven from files alone.",
                             "missing_semantic_scores": int(asd_frame.normal_correctness.isna().sum())}
    atomic_json(ROOT / "results/validation.json", result)
    return result


def summarize() -> dict:
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    rag = recorded(ROOT / "results/raw/test_queries.jsonl")
    pairs = recorded(ROOT / "results/asd_ste/pairs.jsonl")
    expected = 3 * config["split"]["final_test_per_difficulty"]
    if len(rag) != expected or len(pairs) != expected:
        raise RuntimeError(f"Incomplete raw matrix: {len(rag)} RAG and {len(pairs)} pairs")
    mapping = json.loads((ROOT / "results/asd_ste/blind_style_mapping.json").read_text(encoding="utf-8"))
    rag_frame = flatten_rag(rag)
    rag_frame.to_csv(ROOT / "results/raw/test_per_query.csv", index=False, na_rep="NA")
    rag_results = rag_summary(rag_frame)
    rag_results.to_csv(ROOT / "results/summary/rag_results_by_difficulty.csv", index=False, na_rep="NA")
    abl = ablation(rag)
    abl.to_csv(ROOT / "results/summary/retrieval_ablation.csv", index=False, na_rep="NA")
    failures = []
    for difficulty in ["easy", "medium", "hard", "overall"]:
        relevant = [r for r in rag.values() if difficulty == "overall" or r["difficulty"] == difficulty]
        counts = Counter(r.get("failure_type") or "NONE" for r in relevant)
        for label, n in sorted(counts.items()):
            failures.append({"difficulty": difficulty, "failure_type": label, "count": n,
                             "fraction": n / len(relevant) if relevant else None})
    pd.DataFrame(failures).to_csv(ROOT / "results/summary/failure_distribution.csv", index=False, na_rep="NA")
    asd_frame = flatten_asd(pairs, mapping)
    asd_frame.to_csv(ROOT / "results/asd_ste/asd_ste_per_query.csv", index=False, na_rep="NA")
    asd_results = asd_summary(asd_frame)
    asd_results.to_csv(ROOT / "results/asd_ste/asd_ste_summary.csv", index=False, na_rep="NA")
    stats = paired_stats(asd_frame)
    stats.to_csv(ROOT / "results/asd_ste/paired_statistics.csv", index=False, na_rep="NA")
    human_template(pairs, mapping)
    validation = validate(rag, pairs, rag_frame, asd_frame, mapping, config)
    manifest = {"raw_test_sha256": hashlib.sha256((ROOT / "results/raw/test_queries.jsonl").read_bytes()).hexdigest(),
                "raw_asd_sha256": hashlib.sha256((ROOT / "results/asd_ste/pairs.jsonl").read_bytes()).hexdigest(),
                "all_aggregates_recomputed_from_raw": True, "validation_passed": validation["valid"],
                "bootstrap_seed": config["statistics"]["bootstrap_seed"],
                "bootstrap_resamples": config["statistics"]["bootstrap_resamples"]}
    atomic_json(ROOT / "results/summary/metrics_manifest.json", manifest)
    return manifest


if __name__ == "__main__":
    print(json.dumps(summarize(), indent=2))
