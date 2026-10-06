# FinanceBench difficulty and explanation experiment

Status: complete. The 45-question final RAG test and all 45 paired explanation comparisons finished on 2026-10-06. The user amended the final test from 90 to 45 before any final-test inference. The original split is preserved.

The supplied `FinanceRAG/data` contains 150 FinanceBench questions, 180 selected passages, and relevance labels. It lacks the benchmark answers, evidence metadata, and full filings. The experiment therefore uses the official open FinanceBench repository at commit `cc39aeb4afdf33909ee1412188bf89035950c2eb`. Its open subset has 150 questions; its PDF tree has 368 files. All 368 PDFs are retrieved without a question-specific document filter. The licensed 10,231-question benchmark is not available and is not claimed as used.

Python 3.12 is not installed. Python 3.14.3 is used because current Docling supports it and the local ML packages are present. The project environment is `.venv`; actual package versions and hardware are written to `results/environment.json` before inference.

## Fixed execution order

1. Inspect and validate annotations and all source PDFs.
2. Assign difficulty from question metadata and question structure. Save the original 90-test and 30-development split before inference. After the user-requested amendment, preserve it and select a fixed 45-test subset.
3. Convert PDFs of at most 20 pages with Docling and its table model. The 321 longer PDFs use page-aware PyMuPDF extraction with row and column spacing retained. This fixed, document-length-only rule does not consult question labels. The full source is about 54,120 pages, so all-page Docling is impractical on this workstation. Record parser and extraction warnings for every PDF.
4. Create token-aware 500-token chunks with 75-token overlap, then build normalized BGE embeddings, an exact FAISS index, and BM25 on the same chunks.
5. Map benchmark evidence to chunks independently of retrieval and flag uncertain matches.
6. Run sanity tests and 30 development questions. Repair implementation defects only.
7. Freeze `config.yaml` and its SHA-256, then run 45 test questions once. Save every request and ranking.
8. Freeze RAG outputs. Generate paired explanations, blind comparisons, objective writing metrics, and human-evaluation templates.
9. Recompute summaries from raw files, validate the design, and produce the final report and examples.

The exact prompts and parameters are in `config.yaml`. Seed 42 controls splitting, blind presentation, model options, and bootstrap intervals. The final report must distinguish measured values, NA, NOT MEASURED, and FAILED.

The first pre-inference assignment had 90 test questions, 30 per difficulty, and a disjoint 30-question development set. The 90-question assignment, complete split, and manifest are preserved as `results/difficulty_assignment_original_90.csv`, `results/question_split_original_90.csv`, and `results/dataset_manifest_original_90.json`. The first pre-audit difficulty draft remains at `results/difficulty_assignment_pre_audit.csv`.

At 2026-10-05 15:00 +05:30, the user requested a smaller final run because the exact local models were taking minutes per request. The final test will use 15 easy, 15 medium, and 15 hard questions (45 total), selected deterministically with seed 42 from the preserved 90-question test pool. Development remains the original disjoint 30 questions. No final-test retrieval, generation, judging, or explanation output existed before this change. The reduction changes statistical precision, so confidence intervals and limitations must reflect 45 rather than 90 test pairs.

All 368 PDFs normalized successfully: 321 via positional PyMuPDF rows and 47 via Docling, covering 54,120 PDF pages. Of those pages, 218 had no extractable text. None of the 189 annotated evidence items referred to an empty extracted page. Some Docling table-cell matching warnings were emitted; the conversion log and per-document CSV are retained. This page and document count is a pre-inference observation, not a RAG performance result.

The local CPU environment took 30.49 seconds to embed a representative batch of 32 approximately 450-token texts. The available CUDA-enabled host runtime took 2.41 seconds on the same batch. A separate representative CPU reranker call took 7.07 seconds for eight candidates. Consequently the fixed embedding build and a precomputed retrieval/reranking pass use the GTX 1650 Ti CUDA runtime. The `.venv` runtime handles Mistral/Qwen inference. The retrieval algorithm and model weights are unchanged; the saved pass prevents GPU contention and model reloads. Exact index-runtime versions will be stored in `results/index_manifest.json`.

The background final-stage watcher checked every development record for the fixed model/configuration tags, ranking shapes, generated answer, and a recorded semantic evaluation or explicit error. Of 30 development questions, 29 had valid Qwen evaluations; one repeatedly returned truncated JSON and remains a recorded development failure. This did not alter the frozen test prompt or the test split. All 45 final RAG questions and all 45 blind explanation comparisons produced valid outputs.

The frozen test and ASD-STE stages completed without a failed question. Raw outputs were summarized successfully, and `results/validation.json` passed every recorded check. The initial report-generation command failed only because its consistency checker could not verify excerpts that combined separate `config.yaml` sections. The checker was corrected after all inference and aggregation, and the report was regenerated. `results/report_consistency.json` now verifies all 774 recorded cells and excerpts with zero errors. The stale failure in the first `logs/report.log` attempt documents this reporting-only repair.
