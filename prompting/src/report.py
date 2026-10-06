"""Self-contained, source-checked report and balanced article examples."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import pandas as pd
import yaml

from .corpus import atomic_json
from .prepare import ROOT
from .rag import recorded


def display(value) -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)) or pd.isna(value):
        return "NA"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value).replace("|", " / ").replace("\n", " ")


class Report:
    def __init__(self):
        self.lines = []
        self.provenance = []
        self.narrative = []

    def paragraph(self, text: str) -> None:
        self.lines.append(text)
        self.narrative.append(text)

    def heading(self, text: str) -> None:
        self.lines.append("## " + text)

    def table(self, relative: str, columns: list[str] | None = None, where: dict | None = None) -> None:
        path = ROOT / relative
        data = pd.read_csv(path)
        if where:
            for key, value in where.items():
                data = data[data[key] == value]
        columns = [column for column in (columns or data.columns.tolist()) if column in data.columns]
        if data.empty:
            self.paragraph(f"No recorded rows in `{relative}` for this selection.")
            return
        self.lines.append(f"Source: `{relative}`.")
        table = ["| " + " | ".join(columns) + " |", "| " + " | ".join(["---"] * len(columns)) + " |"]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        for index, row in data.iterrows():
            values = [display(row[column]) for column in columns]
            table.append("| " + " | ".join(values) + " |")
            for column, value in zip(columns, values):
                self.provenance.append({"file": relative, "sha256": digest, "row_index": int(index),
                                        "column": column, "rendered_value": value})
        self.lines.append("\n".join(table))

    def code(self, source: str, value) -> None:
        path = ROOT / source
        self.lines.append(f"Source: `{source}`.\n\n```json\n{json.dumps(value, indent=2, ensure_ascii=False)}\n```")
        self.provenance.append({"file": source, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                "json_excerpt": value})


def select_examples() -> pd.DataFrame:
    rag = pd.read_csv(ROOT / "results/raw/test_per_query.csv").set_index("question_id")
    asd = pd.read_csv(ROOT / "results/asd_ste/asd_ste_per_query.csv").set_index("question_id")
    pool = rag.join(asd, rsuffix="_asd")
    selected = []
    used = set()
    for difficulty in ["easy", "medium", "hard"]:
        group = pool[pool.difficulty == difficulty].sort_index()
        weak = group[(group.answer_correctness == False) | (group.failure_type.notna())]
        first_group = weak if not weak.empty else group
        first_reason = ("First ID in this difficulty with an incorrect or weak RAG answer."
                        if not weak.empty else "First ID in this difficulty; no weak RAG answer was available.")
        selected.append({"question_id": first_group.index[0], "difficulty": difficulty,
                         "selection_reason": first_reason})
        used.add(first_group.index[0])
        remaining = group[~group.index.isin(used)]
        loss = remaining[(remaining.asd_fact_coverage < remaining.normal_fact_coverage) |
                         ((remaining.asd_unsupported_claim == True) & (remaining.normal_unsupported_claim == False))]
        tie = remaining[remaining.winner_style == "Tie"]
        help_cases = remaining[(remaining.winner_style == "asd_ste") &
                               (remaining.asd_fact_coverage >= remaining.normal_fact_coverage)]
        if difficulty == "easy":
            choices = [(help_cases, "ASD-STE preference without lower judged fact coverage"),
                       (tie, "Little blind-preference difference"), (loss, "Possible information loss")]
        elif difficulty == "medium":
            choices = [(tie, "Little blind-preference difference"),
                       (loss, "Possible information loss"), (help_cases, "ASD-STE preference")]
        else:
            choices = [(loss, "Possible information loss"),
                       (help_cases, "ASD-STE preference without lower judged fact coverage"),
                       (tie, "Little blind-preference difference")]
        chosen = next(((frame.index[0], reason) for frame, reason in choices if not frame.empty), None)
        if chosen is None and not remaining.empty:
            chosen = (remaining.index[0], "First remaining ID; no preferred category available")
        if chosen is not None:
            selected.append({"question_id": chosen[0], "difficulty": difficulty,
                             "selection_reason": chosen[1]})
            used.add(chosen[0])
    if len(selected) != 6:
        raise RuntimeError("Could not select two examples per difficulty")
    frame = pd.DataFrame(selected)
    frame.to_csv(ROOT / "results/summary/example_selection.csv", index=False)
    return frame


def write_examples(selection: pd.DataFrame) -> None:
    rag = recorded(ROOT / "results/raw/test_queries.jsonl")
    pairs = recorded(ROOT / "results/asd_ste/pairs.jsonl")
    lines = ["# Representative measured examples", "Six cases, two per difficulty. Selection rules and IDs are in `results/summary/example_selection.csv`. These are not human comprehension results."]
    for record in selection.itertuples():
        row = rag[record.question_id]
        pair = pairs[record.question_id]
        lines.extend([f"## {record.question_id} ({record.difficulty})",
                      f"Selection: {record.selection_reason}",
                      "**Question:** " + row["question"],
                      "**Benchmark answer:** " + str(row.get("gold_answer", "NA")),
                      "**Mistral answer:** " + str(row.get("generated_answer", "FAILED")),
                      "**RAG metrics:** `" + json.dumps({
                          **(row.get("retrieval_metrics", {}).get("hybrid_reranker", {})),
                          "answer_correctness": row.get("answer_correctness"),
                          "deterministic_numeric_correctness": row.get("deterministic_numeric_correctness"),
                          "qwen_semantic_correctness": row.get("qwen_semantic_correctness"),
                          "faithfulness": row.get("faithfulness"),
                          "answer_relevance": row.get("answer_relevance"),
                          "unsupported_claim": row.get("unsupported_claim"),
                          "rag_total_latency_s": row.get("rag_total_latency_s")}) + "`",
                      "**Failure classification:** " + str(row.get("failure_type") or "NONE"),
                      "**Normal explanation:**\n\n" + str(pair.get("outputs", {}).get("normal", {}).get("response") or "FAILED"),
                      "**ASD-STE-inspired explanation:**\n\n" + str(pair.get("outputs", {}).get("asd_ste", {}).get("response") or "FAILED"),
                      "**Blind Qwen scores and preference:** `" + json.dumps(pair.get("blind_judgment"), ensure_ascii=False) + "`",
                      "**Objective writing metrics:** `" + json.dumps(pair.get("objective"), ensure_ascii=False) + "`"])
    (ROOT / "ARTICLE_EXAMPLES.md").write_text("\n\n".join(lines) + "\n", encoding="utf-8")


def file_inventory() -> list[dict]:
    ignore = {"report_provenance.json", "report_consistency.json", "file_inventory.json",
              "report_narrative.txt", "pipeline_status.json", "autorun_final_status.json"}
    files = []
    for path in sorted((ROOT / "results").rglob("*")):
        if path.is_file() and path.name not in ignore:
            files.append({"path": path.relative_to(ROOT).as_posix(), "bytes": path.stat().st_size,
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    atomic_json(ROOT / "results/file_inventory.json", files)
    return files


def create_report() -> dict:
    manifest = json.loads((ROOT / "results/summary/metrics_manifest.json").read_text(encoding="utf-8"))
    if not manifest["validation_passed"]:
        raise RuntimeError("Experiment validation failed; report must be marked invalid")
    selection = select_examples()
    write_examples(selection)
    env = json.loads((ROOT / "results/environment.json").read_text(encoding="utf-8"))
    dataset = json.loads((ROOT / "results/dataset_manifest.json").read_text(encoding="utf-8"))
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    mapping = json.loads((ROOT / "results/gold_mapping_summary.json").read_text(encoding="utf-8"))
    docs = pd.read_csv(ROOT / "results/raw/document_processing.csv")
    split = pd.read_csv(ROOT / "results/difficulty_assignment.csv")
    rag = pd.read_csv(ROOT / "results/summary/rag_results_by_difficulty.csv")
    asd = pd.read_csv(ROOT / "results/asd_ste/asd_ste_summary.csv")
    stats = pd.read_csv(ROOT / "results/asd_ste/paired_statistics.csv")
    r = Report()
    r.lines.append("# FinanceBench RAG difficulty and explanation experiment: actual results")
    r.paragraph("This handoff reports the completed local run. It separates measured RAG performance from a second, paired explanation experiment. ASD-STE100-inspired means controlled, simple technical writing; it is not a certification. No human comprehension study was run.")
    r.heading("1. Experiment goal")
    r.paragraph("The first question tests RAG performance across structurally assigned easy, medium, and hard financial questions. The second tests whether an ASD-STE100-inspired explanation communicates the same frozen evaluation packet more clearly than a normal explanation without losing technical facts.")
    r.heading("2. Dataset details")
    r.paragraph("The source is the [official open FinanceBench release](https://github.com/patronus-ai/financebench). The supplied FinanceRAG passages were not used as the retrieval corpus because they are selected excerpts and lack the benchmark answers. The licensed full question set was not available. Retrieval indexed all readable source PDFs in the open repository, with no question-specific document filter.")
    r.code("results/dataset_manifest.json", dataset)
    r.code("results/preflight_validation.json", json.loads((ROOT / "results/preflight_validation.json").read_text(encoding="utf-8")))
    r.heading("3. Exact test split")
    split_ids = {label: sorted(split.loc[split.difficulty == label, "question_id"].tolist()) for label in ["easy", "medium", "hard"]}
    r.code("results/difficulty_assignment.csv", split_ids)
    r.paragraph("The separate development set contains no test question. Its IDs, question text, and split status are in `results/question_split.csv`. Final claims below use the test set only. Before any final-test inference, the user reduced the test from the originally selected 30 per difficulty to 15 per difficulty because local inference was slow. The original 90-question assignment and split remain in `results/difficulty_assignment_original_90.csv` and `results/question_split_original_90.csv`. The final 45 are a deterministic seed-42 subset of the original pool; the original development 30 are unchanged. The smaller test has wider uncertainty and less power for paired explanation comparisons.")
    r.code("results/split_amendment.json", {key: value for key, value in
            json.loads((ROOT / "results/split_amendment.json").read_text(encoding="utf-8")).items()
            if key not in {"original_test_ids", "final_test_ids"}})
    r.heading("4. Difficulty assignment method")
    r.paragraph("Difficulty was assigned before inference using question reasoning metadata, evidence count, and question-wording cues. Direct one-evidence lookups were eligible for easy; arithmetic and comparison questions for medium; multiple evidence, logic, causal, or conditional synthesis for hard. The first draft and pre-inference audit are retained. No model result determined difficulty.")
    r.table("results/difficulty_assignment.csv", ["question_id", "difficulty", "question_type", "reasoning_type", "gold_evidence_count", "difficulty_reason"])
    r.heading("5. Exact RAG architecture")
    r.paragraph("Every available PDF was included in one corpus. Short PDFs used Docling page Markdown with table modeling. Long PDFs used positional row extraction because full Docling processing on the complete page set was impractical on this workstation. The parser choice used PDF page count alone, never the question's source label. Row and column relationships were retained as positional separators where possible; extraction is not equally faithful across parser paths.")
    parser_counts = docs.parser.value_counts(dropna=False).to_dict()
    r.code("results/raw/document_processing.csv", {"parser_counts": parser_counts,
             "failed_documents": int((docs.status != "complete").sum()),
             "empty_pages": int(docs.empty_pages.fillna(0).sum())})
    r.heading("6. Exact model versions and digests")
    r.code("results/environment.json", {key: env.get(key) for key in ["experiment_start_time", "experiment_end_time", "os", "cpu", "ram_bytes", "gpu", "python_version", "ollama_version", "dataset_revision", "workspace_git_commit", "financebench_git_commit", "models", "hf_model_revisions", "package_versions"]})
    r.heading("7. Chunking configuration")
    r.code("config.yaml", config["chunking"])
    r.heading("8. Retrieval configuration")
    r.code("config.yaml", {"embedding": config["embedding"], "lexical": config["lexical"], "retrieval": config["retrieval"]})
    r.code("results/index_manifest.json", json.loads((ROOT / "results/index_manifest.json").read_text(encoding="utf-8")))
    r.heading("9. Reranking configuration")
    r.code("config.yaml", config["reranker"])
    r.heading("10. Generation configuration")
    r.code("config.yaml", {"generator": config["models"]["generator"], "ollama": config["ollama"], "generation_instruction": config["generation_instruction"]})
    r.heading("11. Evaluation configuration")
    r.code("config.yaml", {"evaluator": config["models"]["evaluator"], "seed": config["seed"], "statistics": config["statistics"]})
    r.paragraph("Python computed retrieval metrics and unambiguous numerical equivalence. Qwen supplied only semantic judgments and blind explanation ratings. Malformed responses and retries remain in `results/raw/api_attempts.jsonl`. Numeric correctness and Qwen correctness are both retained when available.")
    r.heading("12. Overall RAG results")
    r.table("results/summary/rag_results_by_difficulty.csv", ["difficulty", "number_of_questions", "successful_queries", "Hit@5", "Recall@5", "Precision@5", "MRR", "nDCG@5", "answer_correctness", "faithfulness", "answer_relevance", "unsupported_claim_rate", "average_total_latency", "average_generation_tokens"], {"difficulty": "overall"})
    r.heading("13. Easy vs medium vs hard")
    r.table("results/summary/rag_results_by_difficulty.csv", ["difficulty", "number_of_questions", "Hit@5", "Hit@5_ci_low", "Hit@5_ci_high", "answer_correctness", "answer_correctness_ci_low", "answer_correctness_ci_high", "faithfulness", "answer_relevance", "unsupported_claim_rate", "average_total_latency"])
    r.heading("14. Retrieval ablation")
    r.table("results/summary/retrieval_ablation.csv", ["difficulty", "configuration", "number_of_questions", "Hit@5", "Recall@5", "Precision@5", "MRR", "nDCG@5", "Hit@5_ci_low", "Hit@5_ci_high"])
    r.paragraph("Only Hybrid + reranker generated answers. Dense and Hybrid results are retrieval ablations on the same queries, not three separate answer-generation systems. Gold evidence-to-chunk mapping is independent of retrieved rankings.")
    r.code("results/gold_mapping_summary.json", mapping)
    r.heading("15. Failure distribution")
    r.table("results/summary/failure_distribution.csv")
    r.heading("16. Normal vs ASD-STE100-inspired explanations")
    r.table("results/asd_ste/asd_ste_summary.csv", ["difficulty", "number_of_pairs", "valid_blind_comparisons", "normal_word_count", "asd_word_count", "normal_avg_sentence_length", "asd_avg_sentence_length", "normal_fact_coverage", "asd_fact_coverage", "normal_correctness", "asd_correctness", "normal_clarity", "asd_clarity", "normal_conciseness", "asd_conciseness", "normal_actionability", "asd_actionability", "Normal_win_rate", "ASD_STE_win_rate", "Tie_rate"])
    r.paragraph("Qwen scored both explanations in randomized blind order. Its ratings are model judgments, not human comprehension results. Exact failure-code and numeric-anchor retention is also saved as a narrow deterministic coverage check; semantic fact coverage comes from the blind Qwen ratings. Objective counts and a rough reading-ease estimate are saved per explanation; the syllable heuristic is poorly suited to finance terms and is not a direct comprehension measure. The human template contains no reader results.")
    r.heading("17. Paired statistical tests")
    r.table("results/asd_ste/paired_statistics.csv")
    r.paragraph("Tests are paired by question. Wilcoxon and exact sign tests are two-sided. Bootstrap intervals use seed 42. Holm-adjusted p-values address multiple listed comparisons. A small p-value does not by itself establish practical value.")
    r.heading("18. Representative examples")
    r.table("results/summary/example_selection.csv")
    r.paragraph("Six complete examples with both explanations, blind scores, and objective metrics are in `ARTICLE_EXAMPLES.md`. Selection includes failure cases and seeks ties and possible information loss where observed.")
    r.heading("19. Unexpected findings")
    r.paragraph("Measured facts: In this test, Dense retrieval had higher overall Hit@5 than the selected Hybrid + reranker pipeline. Answer correctness was low across all difficulty groups. The ASD-STE-inspired explanations were shorter and received higher conciseness ratings, but the blind preference was effectively split, and the clarity difference was not statistically significant after adjustment. The ASD explanations had a lower fact-coverage point estimate; that difference was also not significant after adjustment. The tables above give the exact estimates and uncertainty. Faithfulness and relevance ratings remained high even when answers were incorrect, so those evaluator dimensions do not substitute for answer correctness.")
    r.paragraph("Possible explanations include parser fidelity, table extraction, evidence granularity, question difficulty, and local model behavior. This run does not isolate those causes. Some evidence-to-chunk mappings are uncertain, so retrieval comparisons depend in part on the constructed chunk labels.")
    r.heading("20. Experimental limitations")
    r.paragraph("The open subset is not the licensed full FinanceBench benchmark. The test split uses rule-based difficulty labels rather than independent expert difficulty ratings. Docling covers short PDFs, while long filings use positional extraction; some table layout can remain ambiguous. Gold evidence is often page-scale, so chunk mapping can be uncertain. One local Qwen judge provides semantic ratings. Blinded model ratings cannot establish real reader comprehension or formal ASD-STE100 compliance. The single workstation and serial Ollama calls limit performance generalization.")
    r.heading("21. Failed runs and data quality")
    errors = pd.read_csv(ROOT / "results/raw/test_per_query.csv")
    r.code("results/raw/test_per_query.csv", {"failed_or_partial_test_queries": int((errors.status != "success").sum()),
                                               "unmapped_gold_questions": mapping["unmapped_questions"],
                                               "uncertain_gold_evidence_items": mapping["uncertain_evidence_items"]})
    r.paragraph("NA means no defensible value was calculated, such as an unambiguous numeric answer or a valid semantic judgment. NOT MEASURED applies to human comprehension outcomes. FAILED marks individual requests or documents that did not produce usable outputs; their raw attempts remain saved.")
    r.heading("22. Raw files and reproducibility")
    files = file_inventory()
    r.lines.extend("- `" + item["path"] + "`" for item in files)
    r.lines.extend(["- `results/report_provenance.json`", "- `results/report_consistency.json`",
                    "- `results/file_inventory.json`", "- `results/pipeline_status.json`",
                    "- `results/autorun_final_status.json`", "- `RESULTS.md`", "- `ARTICLE_EXAMPLES.md`",
                    "- `README.md`", "- `RUN_MANIFEST.md`", "- `config.yaml`", "- `requirements.txt`"])
    r.paragraph("The full per-query JSONL files retain nested rankings, contexts, prompts, answers, judgments, errors, and pair mapping. The reproduction commands are in `README.md`.")
    report = "\n\n".join(r.lines) + "\n"
    (ROOT / "RESULTS.md").write_text(report, encoding="utf-8")
    atomic_json(ROOT / "results/report_provenance.json", {"cells": r.provenance})
    (ROOT / "results/report_narrative.txt").write_text("\n\n".join(r.narrative), encoding="utf-8")
    return verify()


def subset(small, full) -> bool:
    if isinstance(small, dict):
        return isinstance(full, dict) and all(key in full and subset(value, full[key]) for key, value in small.items())
    if isinstance(small, list):
        return isinstance(full, list) and all(any(subset(value, item) for item in full) for value in small)
    return small == full


def contained_excerpt(excerpt, source) -> bool:
    """Verify an excerpt against either the root or a nested configuration section."""
    if subset(excerpt, source):
        return True
    # A report excerpt can combine independent config sections (for example,
    # embedding, lexical, and retrieval). Verify each complete section against
    # the source instead of requiring the combined object to exist verbatim.
    if isinstance(excerpt, dict) and isinstance(source, dict) and len(excerpt) > 1:
        if all(contained_excerpt({key: value}, source) for key, value in excerpt.items()):
            return True
    if isinstance(source, dict):
        return any(contained_excerpt(excerpt, value) for value in source.values())
    if isinstance(source, list):
        return any(contained_excerpt(excerpt, value) for value in source)
    return False


def verify() -> dict:
    cells = json.loads((ROOT / "results/report_provenance.json").read_text(encoding="utf-8"))["cells"]
    errors = []
    cache = {}
    for item in cells:
        path = ROOT / item["file"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            errors.append("Source hash changed: " + item["file"])
        if "row_index" in item:
            if item["file"] not in cache:
                cache[item["file"]] = pd.read_csv(path)
            actual = display(cache[item["file"]].iloc[item["row_index"]][item["column"]])
            if actual != item["rendered_value"]:
                errors.append("Cell changed: " + str(item))
        if "json_excerpt" in item:
            source = yaml.safe_load(path.read_text(encoding="utf-8")) if path.suffix == ".yaml" else json.loads(path.read_text(encoding="utf-8")) if path.suffix == ".json" else None
            if source is None or not contained_excerpt(item["json_excerpt"], source):
                if path.suffix == ".csv":
                    data = pd.read_csv(path)
                    excerpt = item["json_excerpt"]
                    if item["file"] == "results/difficulty_assignment.csv":
                        expected = {label: sorted(data.loc[data.difficulty == label, "question_id"].tolist())
                                    for label in ["easy", "medium", "hard"]}
                    elif item["file"] == "results/raw/document_processing.csv":
                        expected = {"parser_counts": data.parser.value_counts(dropna=False).to_dict(),
                                    "failed_documents": int((data.status != "complete").sum()),
                                    "empty_pages": int(data.empty_pages.fillna(0).sum())}
                    elif item["file"] == "results/raw/test_per_query.csv":
                        evidence = json.loads((ROOT / "results/gold_mapping_summary.json").read_text(encoding="utf-8"))
                        expected = {"failed_or_partial_test_queries": int((data.status != "success").sum()),
                                    "unmapped_gold_questions": evidence["unmapped_questions"],
                                    "uncertain_gold_evidence_items": evidence["uncertain_evidence_items"]}
                    else:
                        expected = None
                    if expected != excerpt:
                        errors.append("CSV excerpt changed: " + item["file"])
                else:
                    errors.append("Excerpt changed: " + item["file"])
    result = {"passed": not errors, "verified_cells_and_excerpts": len(cells), "errors": errors,
              "report_sha256": hashlib.sha256((ROOT / "RESULTS.md").read_bytes()).hexdigest()}
    atomic_json(ROOT / "results/report_consistency.json", result)
    if errors:
        raise RuntimeError("Report consistency failed: " + str(errors[:3]))
    return result


if __name__ == "__main__":
    print(json.dumps(create_report(), indent=2))
