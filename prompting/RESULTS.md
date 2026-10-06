# FinanceBench RAG difficulty and explanation experiment: actual results

This handoff reports the completed local run. It separates measured RAG performance from a second, paired explanation experiment. ASD-STE100-inspired means controlled, simple technical writing; it is not a certification. No human comprehension study was run.

## 1. Experiment goal

The first question tests RAG performance across structurally assigned easy, medium, and hard financial questions. The second tests whether an ASD-STE100-inspired explanation communicates the same frozen evaluation packet more clearly than a normal explanation without losing technical facts.

## 2. Dataset details

The source is the [official open FinanceBench release](https://github.com/patronus-ai/financebench). The supplied FinanceRAG passages were not used as the retrieval corpus because they are selected excerpts and lack the benchmark answers. The licensed full question set was not available. Retrieval indexed all readable source PDFs in the open repository, with no question-specific document filter.

Source: `results/dataset_manifest.json`.

```json
{
  "source_revision": "cc39aeb4afdf33909ee1412188bf89035950c2eb",
  "source_question_sha256": "b927dae968f6f8dd36a920393717d0a5c2a537d0c6bc93a7e754bf356d7a73b0",
  "source_document_metadata_sha256": "c9bee8f606e1abbac90392e0c05debfc2f2821022cd09298f1230d825e129e7b",
  "question_count": 150,
  "pdf_count": 368,
  "pdf_bytes": 705172379,
  "document_metadata_count": 361,
  "difficulty_eligible_counts": {
    "hard": 57,
    "medium": 57,
    "easy": 32,
    "unassigned": 4
  },
  "test_counts": {
    "hard": 15,
    "medium": 15,
    "easy": 15
  },
  "development_count": 30,
  "missing_question_source_pdfs": [],
  "difficulty_assignment_sha256": "5fd8e6807e843ec04bea4e347fbe1cf7b30797ce376418855336a4e6f773105a",
  "question_split_sha256": "de0de4221b9666c64abbffb6c3ac3967b959b945be4dcd5db999c303c6493411",
  "candidate_pool_per_difficulty": 30,
  "selection_algorithm": "numpy.default_rng(42), sorted IDs, initial pool per class, development from outside pool, then final test subset from pool"
}
```

Source: `results/preflight_validation.json`.

```json
{
  "valid": true,
  "checks": {
    "150_questions": true,
    "expected_test_and_development": true,
    "expected_each_difficulty": true,
    "disjoint_development_test": true,
    "all_368_pdfs_parsed": true,
    "all_368_pdfs_chunked": true,
    "no_oversize_or_empty_chunks": true,
    "all_150_gold_mappings_exist": true,
    "no_unmapped_test_question": true
  },
  "chunk_count": 110138,
  "oversize_chunks": 0,
  "empty_chunks": 0,
  "parsed_pages": 54120,
  "pages_without_extractable_text": 218,
  "parser_counts": {
    "pymupdf_positional_rows": 321,
    "docling": 47
  },
  "test_uncertain_evidence_items": 4,
  "test_questions_with_uncertain_mapping": 3
}
```

## 3. Exact test split

Source: `results/difficulty_assignment.csv`.

```json
{
  "easy": [
    "financebench_id_00464",
    "financebench_id_00476",
    "financebench_id_00995",
    "financebench_id_01091",
    "financebench_id_01254",
    "financebench_id_01319",
    "financebench_id_01328",
    "financebench_id_01488",
    "financebench_id_01490",
    "financebench_id_02419",
    "financebench_id_03282",
    "financebench_id_03531",
    "financebench_id_04171",
    "financebench_id_04672",
    "financebench_id_08286"
  ],
  "medium": [
    "financebench_id_00302",
    "financebench_id_00407",
    "financebench_id_00494",
    "financebench_id_00521",
    "financebench_id_00591",
    "financebench_id_00605",
    "financebench_id_00724",
    "financebench_id_00735",
    "financebench_id_00822",
    "financebench_id_00839",
    "financebench_id_01487",
    "financebench_id_01902",
    "financebench_id_01964",
    "financebench_id_03471",
    "financebench_id_07507"
  ],
  "hard": [
    "financebench_id_00070",
    "financebench_id_00222",
    "financebench_id_00669",
    "financebench_id_00685",
    "financebench_id_00720",
    "financebench_id_01198",
    "financebench_id_01226",
    "financebench_id_01290",
    "financebench_id_02987",
    "financebench_id_04080",
    "financebench_id_04254",
    "financebench_id_04458",
    "financebench_id_04735",
    "financebench_id_10130",
    "financebench_id_10420"
  ]
}
```

The separate development set contains no test question. Its IDs, question text, and split status are in `results/question_split.csv`. Final claims below use the test set only. Before any final-test inference, the user reduced the test from the originally selected 30 per difficulty to 15 per difficulty because local inference was slow. The original 90-question assignment and split remain in `results/difficulty_assignment_original_90.csv` and `results/question_split_original_90.csv`. The final 45 are a deterministic seed-42 subset of the original pool; the original development 30 are unchanged. The smaller test has wider uncertainty and less power for paired explanation comparisons.

Source: `results/split_amendment.json`.

```json
{
  "amended_at": "2026-10-05T17:07:08+0530",
  "reason": "User requested 15 easy, 15 medium, and 15 hard final-test questions to reduce local runtime",
  "old_test_count": 90,
  "new_test_count": 45,
  "old_per_difficulty": 30,
  "new_per_difficulty": 15,
  "development_count": 30,
  "development_ids_unchanged": true,
  "new_test_is_subset_of_original_test": true,
  "no_final_test_inference_or_freeze_before_amendment": true,
  "old_config_sha256": "24d1bdd1009f4a597ae3484442ad32e79821b0bca9fddc9280f89e7b36ad0596",
  "new_config_sha256": "33e082be509880e262f0f3465393522b85195fb3392def433936cdf3b1085db5",
  "selection": "numpy.default_rng(42): choose original 30 per class, choose original 30 development outside pool, then choose 15 per class from original pool",
  "limitation": "Halving the test size widens confidence intervals and reduces power for paired tests"
}
```

## 4. Difficulty assignment method

Difficulty was assigned before inference using question reasoning metadata, evidence count, and question-wording cues. Direct one-evidence lookups were eligible for easy; arithmetic and comparison questions for medium; multiple evidence, logic, causal, or conditional synthesis for hard. The first draft and pre-inference audit are retained. No model result determined difficulty.

Source: `results/difficulty_assignment.csv`.

| question_id | difficulty | question_type | reasoning_type | gold_evidence_count | difficulty_reason |
| --- | --- | --- | --- | --- | --- |
| financebench_id_00070 | hard | domain-relevant | Numerical reasoning OR Logical reasoning | 2 | Causal or conditional interpretation in question; reasoning=numerical reasoning or logical reasoning; 2 evidence item(s). |
| financebench_id_00222 | hard | domain-relevant | Logical reasoning (based on numerical reasoning) OR Logical reasoning | 1 | Causal or conditional interpretation in question; reasoning=logical reasoning (based on numerical reasoning) or logical reasoning; 1 evidence item(s). |
| financebench_id_00302 | medium | novel-generated | NA | 1 | Numerical/comparison structure; reasoning=none; 1 evidence item(s). |
| financebench_id_00407 | medium | novel-generated | NA | 1 | Numerical/comparison structure; reasoning=none; 1 evidence item(s). |
| financebench_id_00464 | easy | novel-generated | NA | 1 | Direct single-evidence lookup without an arithmetic or synthesis cue; reasoning=none. |
| financebench_id_00476 | easy | domain-relevant | Information extraction | 1 | Direct single-evidence lookup without an arithmetic or synthesis cue; reasoning=information extraction. |
| financebench_id_00494 | medium | novel-generated | NA | 1 | Numerical/comparison structure; reasoning=none; 1 evidence item(s). |
| financebench_id_00521 | medium | domain-relevant | Information extraction | 1 | Numerical/comparison structure; reasoning=information extraction; 1 evidence item(s). |
| financebench_id_00591 | medium | novel-generated | NA | 1 | Numerical/comparison structure; reasoning=none; 1 evidence item(s). |
| financebench_id_00605 | medium | novel-generated | NA | 1 | Numerical/comparison structure; reasoning=none; 1 evidence item(s). |
| financebench_id_00669 | hard | domain-relevant | Logical reasoning (based on numerical reasoning) OR Numerical reasoning OR Logical reasoning | 1 | Causal or conditional interpretation in question; reasoning=logical reasoning (based on numerical reasoning) or numerical reasoning or logical reasoning; 1 evidence item(s). |
| financebench_id_00685 | hard | domain-relevant | Logical reasoning (based on numerical reasoning) OR Logical reasoning | 1 | Causal or conditional interpretation in question; reasoning=logical reasoning (based on numerical reasoning) or logical reasoning; 1 evidence item(s). |
| financebench_id_00720 | hard | domain-relevant | Logical reasoning (based on numerical reasoning) OR Numerical reasoning OR Logical reasoning | 1 | Causal or conditional interpretation in question; reasoning=logical reasoning (based on numerical reasoning) or numerical reasoning or logical reasoning; 1 evidence item(s). |
| financebench_id_00724 | medium | novel-generated | NA | 1 | Numerical/comparison structure; reasoning=none; 1 evidence item(s). |
| financebench_id_00735 | medium | domain-relevant | Information extraction | 1 | Numerical/comparison structure; reasoning=information extraction; 1 evidence item(s). |
| financebench_id_00822 | medium | novel-generated | NA | 1 | Numerical/comparison structure; reasoning=none; 1 evidence item(s). |
| financebench_id_00839 | medium | novel-generated | NA | 1 | Numerical/comparison structure; reasoning=none; 1 evidence item(s). |
| financebench_id_00995 | easy | domain-relevant | Information extraction | 1 | Direct single-evidence lookup without an arithmetic or synthesis cue; reasoning=information extraction. |
| financebench_id_01091 | easy | domain-relevant | Information extraction | 1 | Direct single-evidence lookup without an arithmetic or synthesis cue; reasoning=information extraction. |
| financebench_id_01198 | hard | domain-relevant | Information extraction | 1 | Causal or conditional interpretation in question; reasoning=information extraction; 1 evidence item(s). |
| financebench_id_01226 | hard | domain-relevant | Logical reasoning (based on numerical reasoning) OR Numerical reasoning OR Logical reasoning | 1 | Causal or conditional interpretation in question; reasoning=logical reasoning (based on numerical reasoning) or numerical reasoning or logical reasoning; 1 evidence item(s). |
| financebench_id_01254 | easy | domain-relevant | Information extraction | 1 | Direct single-evidence lookup without an arithmetic or synthesis cue; reasoning=information extraction. |
| financebench_id_01290 | hard | domain-relevant | Information extraction OR Logical reasoning | 3 | 3 benchmark evidence items require synthesis across multiple facts. |
| financebench_id_01319 | easy | domain-relevant | Information extraction | 1 | Direct single-evidence lookup without an arithmetic or synthesis cue; reasoning=information extraction. |
| financebench_id_01328 | easy | domain-relevant | Information extraction | 1 | Direct single-evidence lookup without an arithmetic or synthesis cue; reasoning=information extraction. |
| financebench_id_01487 | medium | novel-generated | NA | 1 | Numerical/comparison structure; reasoning=none; 1 evidence item(s). |
| financebench_id_01488 | easy | novel-generated | NA | 1 | Direct single-evidence lookup without an arithmetic or synthesis cue; reasoning=none. |
| financebench_id_01490 | easy | novel-generated | NA | 1 | Direct single-evidence lookup without an arithmetic or synthesis cue; reasoning=none. |
| financebench_id_01902 | medium | novel-generated | NA | 1 | Numerical/comparison structure; reasoning=none; 1 evidence item(s). |
| financebench_id_01964 | medium | novel-generated | NA | 1 | Numerical/comparison structure; reasoning=none; 1 evidence item(s). |
| financebench_id_02419 | easy | novel-generated | NA | 1 | Direct single-evidence lookup without an arithmetic or synthesis cue; reasoning=none. |
| financebench_id_02987 | hard | metrics-generated | Numerical reasoning | 2 | 2 evidence items with numerical or interpretive reasoning. |
| financebench_id_03282 | easy | metrics-generated | Information extraction | 1 | Direct single-evidence lookup without an arithmetic or synthesis cue; reasoning=information extraction. |
| financebench_id_03471 | medium | metrics-generated | Numerical reasoning | 1 | Numerical/comparison structure; reasoning=numerical reasoning; 1 evidence item(s). |
| financebench_id_03531 | easy | metrics-generated | Information extraction | 1 | Direct single-evidence lookup without an arithmetic or synthesis cue; reasoning=information extraction. |
| financebench_id_04080 | hard | metrics-generated | Numerical reasoning | 2 | 2 evidence items with numerical or interpretive reasoning. |
| financebench_id_04171 | easy | metrics-generated | Information extraction | 1 | Direct single-evidence lookup without an arithmetic or synthesis cue; reasoning=information extraction. |
| financebench_id_04254 | hard | metrics-generated | Numerical reasoning | 2 | 2 evidence items with numerical or interpretive reasoning. |
| financebench_id_04458 | hard | metrics-generated | Numerical reasoning | 2 | 2 evidence items with numerical or interpretive reasoning. |
| financebench_id_04672 | easy | metrics-generated | Information extraction | 1 | Direct single-evidence lookup without an arithmetic or synthesis cue; reasoning=information extraction. |
| financebench_id_04735 | hard | metrics-generated | Numerical reasoning | 2 | 2 evidence items with numerical or interpretive reasoning. |
| financebench_id_07507 | medium | metrics-generated | Numerical reasoning | 1 | Numerical/comparison structure; reasoning=numerical reasoning; 1 evidence item(s). |
| financebench_id_08286 | easy | metrics-generated | Information extraction | 1 | Direct single-evidence lookup without an arithmetic or synthesis cue; reasoning=information extraction. |
| financebench_id_10130 | hard | metrics-generated | Numerical reasoning | 2 | 2 evidence items with numerical or interpretive reasoning. |
| financebench_id_10420 | hard | metrics-generated | Numerical reasoning | 2 | 2 evidence items with numerical or interpretive reasoning. |

## 5. Exact RAG architecture

Every available PDF was included in one corpus. Short PDFs used Docling page Markdown with table modeling. Long PDFs used positional row extraction because full Docling processing on the complete page set was impractical on this workstation. The parser choice used PDF page count alone, never the question's source label. Row and column relationships were retained as positional separators where possible; extraction is not equally faithful across parser paths.

Source: `results/raw/document_processing.csv`.

```json
{
  "parser_counts": {
    "pymupdf_positional_rows": 321,
    "docling": 47
  },
  "failed_documents": 0,
  "empty_pages": 218
}
```

## 6. Exact model versions and digests

Source: `results/environment.json`.

```json
{
  "experiment_start_time": "2026-10-05T09:35:59+0530",
  "experiment_end_time": "2026-10-06T01:41:36+0530",
  "os": "Windows-11-10.0.26200-SP0",
  "cpu": "Intel(R) Core(TM) i7-9750H CPU @ 2.60GHz",
  "ram_bytes": 68554764288,
  "gpu": {
    "exit_code": 0,
    "stdout": "NVIDIA GeForce GTX 1650 Ti, 4096 MiB, 561.09\n",
    "stderr": ""
  },
  "python_version": "3.14.3 (tags/v3.14.3:323c59a, Feb  3 2026, 16:04:56) [MSC v.1944 64 bit (AMD64)]",
  "ollama_version": {
    "version": "0.35.0"
  },
  "dataset_revision": "cc39aeb4afdf33909ee1412188bf89035950c2eb",
  "workspace_git_commit": "45ad5974528fe3f2e8ff0b1f2f264123f6a54025",
  "financebench_git_commit": "cc39aeb4afdf33909ee1412188bf89035950c2eb",
  "models": {
    "generator": {
      "name": "mistral:latest",
      "digest": "6577803aa9a036369e481d648a2baebb381ebc6e897f2bb9a766a2aa7bfbc1cf",
      "size": 4372824384,
      "details": {
        "parent_model": "",
        "format": "gguf",
        "family": "llama",
        "families": [
          "llama"
        ],
        "parameter_size": "7.2B",
        "quantization_level": "Q4_K_M",
        "context_length": 32768,
        "embedding_length": 4096
      }
    },
    "evaluator": {
      "name": "qwen3.5:9b",
      "digest": "6488c96fa5faab64bb65cbd30d4289e20e6130ef535a93ef9a49f42eda893ea7",
      "size": 6594474711,
      "details": {
        "parent_model": "",
        "format": "gguf",
        "family": "qwen35",
        "families": [
          "qwen35"
        ],
        "parameter_size": "9.7B",
        "quantization_level": "Q4_K_M",
        "context_length": 262144,
        "embedding_length": 4096
      }
    }
  },
  "hf_model_revisions": {
    "bge-base-en-v1.5": "a5beb1e3e68b9ab74eb54cfd186867f64f240e1a",
    "bge-reranker-base": "2cfc18c9415c912f9d8155881c133215df768a70"
  },
  "package_versions": {
    "docling": "2.129.0",
    "docling-core": "2.99.0",
    "huggingface-hub": "1.33.0",
    "pymupdf": "1.27.2.3",
    "torch": "2.14.1",
    "sentence-transformers": "5.4.1",
    "transformers": "5.18.0",
    "faiss-cpu": "1.13.2",
    "rank-bm25": "0.2.2",
    "numpy": "2.5.3",
    "pandas": "3.0.6",
    "PyYAML": "6.0.3",
    "scipy": "1.18.1",
    "requests": "2.34.2",
    "psutil": "7.2.2",
    "tiktoken": "0.13.0"
  }
}
```

## 7. Chunking configuration

Source: `config.yaml`.

```json
{
  "tokenizer": "BAAI/bge-base-en-v1.5",
  "target_tokens": 500,
  "overlap_tokens": 75
}
```

## 8. Retrieval configuration

Source: `config.yaml`.

```json
{
  "embedding": {
    "model": "BAAI/bge-base-en-v1.5",
    "normalize": true,
    "index": "IndexFlatIP",
    "indexing_device": "cuda",
    "retrieval_device": "cuda"
  },
  "lexical": {
    "model": "BM25Okapi"
  },
  "retrieval": {
    "dense_candidates": 20,
    "bm25_candidates": 20,
    "rrf_k": 60,
    "final_top_k": 5
  }
}
```

Source: `results/index_manifest.json`.

```json
{
  "chunk_count": 110138,
  "chunk_sha256": "a815107556366d4b4162343c2e8fcf29b172cba5896dd1cdf0aca2267036b038",
  "embedding_model": "BAAI/bge-base-en-v1.5",
  "dimension": 768,
  "normalized": true,
  "faiss_index": "IndexFlatIP",
  "bm25": "BM25Okapi",
  "indexing_device": "cuda",
  "python_executable": "C:\\Python314\\python.exe",
  "python_version": "3.14.3 (tags/v3.14.3:323c59a, Feb  3 2026, 16:04:56) [MSC v.1944 64 bit (AMD64)]",
  "torch_version": "2.12.0+cu126",
  "transformers_version": "5.7.0",
  "sentence_transformers_version": "5.4.1",
  "faiss_cpu_version": "1.13.2",
  "rank_bm25_version": "0.2.2",
  "numpy_version": "2.4.4",
  "model_revisions": {
    "bge-base-en-v1.5": "a5beb1e3e68b9ab74eb54cfd186867f64f240e1a",
    "bge-reranker-base": "2cfc18c9415c912f9d8155881c133215df768a70"
  },
  "embedding_seconds_this_session": 10608.741373200028
}
```

## 9. Reranking configuration

Source: `config.yaml`.

```json
{
  "model": "BAAI/bge-reranker-base",
  "device": "cuda"
}
```

## 10. Generation configuration

Source: `config.yaml`.

```json
{
  "generator": {
    "tag": "mistral:latest",
    "digest": "6577803aa9a036369e481d648a2baebb381ebc6e897f2bb9a766a2aa7bfbc1cf"
  },
  "ollama": {
    "base_url": "http://localhost:11434",
    "temperature": 0,
    "seed": 42,
    "num_ctx": 8192,
    "generation_max_tokens": 256,
    "explanation_max_tokens": 320,
    "evaluation_max_tokens": 512,
    "warmup_calls": 5,
    "request_timeout_seconds": 600
  },
  "generation_instruction": "Answer the question using only the supplied context.\n\nIf the answer requires a calculation, perform the calculation.\n\nDo not use information outside the supplied context.\n\nIf the context does not contain enough information, state that the available evidence is insufficient.\n\nGive a concise answer.\n\nReference the supporting context IDs."
}
```

## 11. Evaluation configuration

Source: `config.yaml`.

```json
{
  "evaluator": {
    "tag": "qwen3.5:9b",
    "digest": "6488c96fa5faab64bb65cbd30d4289e20e6130ef535a93ef9a49f42eda893ea7"
  },
  "seed": 42,
  "statistics": {
    "bootstrap_resamples": 2000,
    "bootstrap_seed": 42
  }
}
```

Python computed retrieval metrics and unambiguous numerical equivalence. Qwen supplied only semantic judgments and blind explanation ratings. Malformed responses and retries remain in `results/raw/api_attempts.jsonl`. Numeric correctness and Qwen correctness are both retained when available.

## 12. Overall RAG results

Source: `results/summary/rag_results_by_difficulty.csv`.

| difficulty | number_of_questions | successful_queries | Hit@5 | Recall@5 | Precision@5 | MRR | nDCG@5 | answer_correctness | faithfulness | answer_relevance | unsupported_claim_rate | average_total_latency | average_generation_tokens |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| overall | 45 | 45 | 0.2222 | 0.1963 | 0.0444 | 0.1285 | 0.1413 | 0.0889 | 0.8000 | 0.8778 | 0.2889 | 112.6476 | 114.4667 |

## 13. Easy vs medium vs hard

Source: `results/summary/rag_results_by_difficulty.csv`.

| difficulty | number_of_questions | Hit@5 | Hit@5_ci_low | Hit@5_ci_high | answer_correctness | answer_correctness_ci_low | answer_correctness_ci_high | faithfulness | answer_relevance | unsupported_claim_rate | average_total_latency |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| easy | 15 | 0.3333 | 0.1333 | 0.6000 | 0.2667 | 0.0667 | 0.4683 | 0.8667 | 0.8667 | 0.2000 | 109.7036 |
| medium | 15 | 0.2000 | 0.0000 | 0.4000 | 0.0000 | 0.0000 | 0.0000 | 0.8667 | 0.9333 | 0.2000 | 106.1830 |
| hard | 15 | 0.1333 | 0.0000 | 0.3333 | 0.0000 | 0.0000 | 0.0000 | 0.6667 | 0.8333 | 0.4667 | 122.0563 |
| overall | 45 | 0.2222 | 0.1111 | 0.3556 | 0.0889 | 0.0222 | 0.1778 | 0.8000 | 0.8778 | 0.2889 | 112.6476 |

## 14. Retrieval ablation

Source: `results/summary/retrieval_ablation.csv`.

| difficulty | configuration | number_of_questions | Hit@5 | Recall@5 | Precision@5 | MRR | nDCG@5 | Hit@5_ci_low | Hit@5_ci_high |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| easy | Dense | 15 | 0.3333 | 0.3333 | 0.0667 | 0.1722 | 0.2128 | 0.1333 | 0.6000 |
| easy | Hybrid | 15 | 0.2667 | 0.2667 | 0.0533 | 0.1500 | 0.1795 | 0.0667 | 0.4667 |
| easy | Hybrid + reranker | 15 | 0.3333 | 0.3333 | 0.0667 | 0.2056 | 0.2374 | 0.1333 | 0.6000 |
| medium | Dense | 15 | 0.3333 | 0.3333 | 0.0667 | 0.1467 | 0.1925 | 0.1333 | 0.5350 |
| medium | Hybrid | 15 | 0.1333 | 0.1333 | 0.0267 | 0.1000 | 0.1087 | 0.0000 | 0.3333 |
| medium | Hybrid + reranker | 15 | 0.2000 | 0.2000 | 0.0400 | 0.1333 | 0.1508 | 0.0000 | 0.4000 |
| hard | Dense | 15 | 0.2667 | 0.1111 | 0.0533 | 0.1722 | 0.1061 | 0.0667 | 0.5333 |
| hard | Hybrid | 15 | 0.2000 | 0.0889 | 0.0400 | 0.1133 | 0.0729 | 0.0000 | 0.4000 |
| hard | Hybrid + reranker | 15 | 0.1333 | 0.0556 | 0.0267 | 0.0467 | 0.0356 | 0.0000 | 0.3333 |
| overall | Dense | 45 | 0.3111 | 0.2593 | 0.0622 | 0.1637 | 0.1705 | 0.1778 | 0.4444 |
| overall | Hybrid | 45 | 0.2000 | 0.1630 | 0.0400 | 0.1211 | 0.1204 | 0.0889 | 0.3333 |
| overall | Hybrid + reranker | 45 | 0.2222 | 0.1963 | 0.0444 | 0.1285 | 0.1413 | 0.1111 | 0.3556 |

Only Hybrid + reranker generated answers. Dense and Hybrid results are retrieval ablations on the same queries, not three separate answer-generation systems. Gold evidence-to-chunk mapping is independent of retrieved rankings.

Source: `results/gold_mapping_summary.json`.

```json
{
  "questions": 150,
  "evidence_items": 189,
  "unmapped_questions": 0,
  "uncertain_evidence_items": 13,
  "methods": {
    "same_page_token_overlap": 109,
    "exact_normalized_substring": 80
  }
}
```

## 15. Failure distribution

Source: `results/summary/failure_distribution.csv`.

| difficulty | failure_type | count | fraction |
| --- | --- | --- | --- |
| easy | G1 | 1 | 0.0667 |
| easy | G2 | 1 | 0.0667 |
| easy | G4 | 1 | 0.0667 |
| easy | NONE | 4 | 0.2667 |
| easy | R1 | 5 | 0.3333 |
| easy | R2 | 3 | 0.2000 |
| medium | G1 | 2 | 0.1333 |
| medium | G4 | 1 | 0.0667 |
| medium | R1 | 9 | 0.6000 |
| medium | R2 | 3 | 0.2000 |
| hard | G1 | 1 | 0.0667 |
| hard | G4 | 1 | 0.0667 |
| hard | R1 | 8 | 0.5333 |
| hard | R2 | 5 | 0.3333 |
| overall | G1 | 4 | 0.0889 |
| overall | G2 | 1 | 0.0222 |
| overall | G4 | 3 | 0.0667 |
| overall | NONE | 4 | 0.0889 |
| overall | R1 | 22 | 0.4889 |
| overall | R2 | 11 | 0.2444 |

## 16. Normal vs ASD-STE100-inspired explanations

Source: `results/asd_ste/asd_ste_summary.csv`.

| difficulty | number_of_pairs | valid_blind_comparisons | normal_word_count | asd_word_count | normal_avg_sentence_length | asd_avg_sentence_length | normal_fact_coverage | asd_fact_coverage | normal_correctness | asd_correctness | normal_clarity | asd_clarity | normal_conciseness | asd_conciseness | normal_actionability | asd_actionability | Normal_win_rate | ASD_STE_win_rate | Tie_rate |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| easy | 15 | 15 | 189.6667 | 126.5333 | 24.9743 | 13.4501 | 0.8667 | 0.7600 | 3.6667 | 3.7333 | 4.5333 | 4.3333 | 2.8667 | 4.5333 | 3.2667 | 3.5333 | 0.4667 | 0.5333 | 0.0000 |
| medium | 15 | 15 | 209.0000 | 127.2667 | 19.9190 | 12.9765 | 0.8467 | 0.6533 | 3.6667 | 3.2000 | 4.3333 | 4.2000 | 2.8000 | 4.6667 | 3.9333 | 3.1333 | 0.4667 | 0.5333 | 0.0000 |
| hard | 15 | 15 | 196.8000 | 147.0667 | 20.7984 | 15.3215 | 0.8133 | 0.6733 | 4.1333 | 3.0000 | 4.5333 | 4.3333 | 3.4000 | 4.3333 | 3.9333 | 3.2667 | 0.6000 | 0.4000 | 0.0000 |
| overall | 45 | 45 | 198.4889 | 133.6222 | 21.8972 | 13.9160 | 0.8422 | 0.6956 | 3.8222 | 3.3111 | 4.4667 | 4.2889 | 3.0222 | 4.5111 | 3.7111 | 3.3111 | 0.5111 | 0.4889 | 0.0000 |

Qwen scored both explanations in randomized blind order. Its ratings are model judgments, not human comprehension results. Exact failure-code and numeric-anchor retention is also saved as a narrow deterministic coverage check; semantic fact coverage comes from the blind Qwen ratings. Objective counts and a rough reading-ease estimate are saved per explanation; the syllable heuristic is poorly suited to finance terms and is not a direct comprehension measure. The human template contains no reader results.

## 17. Paired statistical tests

Source: `results/asd_ste/paired_statistics.csv`.

| comparison | test | n_pairs | mean_ASD_minus_Normal | median_ASD_minus_Normal | mean_difference_ci_low | mean_difference_ci_high | p_value | n_ASD_wins | n_Normal_wins | n_ties | holm_adjusted_p | significant_at_0_05_after_holm |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| word_count | paired Wilcoxon signed-rank | 45 | -64.8667 | -67.0000 | -76.3344 | -52.4656 | 0.0000 | NA | NA | NA | 0.0000 | True |
| avg_sentence_length | paired Wilcoxon signed-rank | 45 | -7.9812 | -8.9293 | -9.7479 | -6.1252 | 0.0000 | NA | NA | NA | 0.0000 | True |
| fact_coverage | paired Wilcoxon signed-rank | 45 | -0.1467 | 0.0000 | -0.2689 | -0.0200 | 0.0258 | NA | NA | NA | 0.1551 | False |
| correctness | paired Wilcoxon signed-rank | 45 | -0.5111 | 0.0000 | -1.1111 | 0.0889 | 0.1062 | NA | NA | NA | 0.5312 | False |
| information_coverage | paired Wilcoxon signed-rank | 45 | -0.7333 | -1.0000 | -1.2222 | -0.2222 | 0.0078 | NA | NA | NA | 0.0549 | False |
| clarity | paired Wilcoxon signed-rank | 45 | -0.1778 | 0.0000 | -0.4889 | 0.1333 | 0.2575 | NA | NA | NA | 0.7726 | False |
| conciseness | paired Wilcoxon signed-rank | 45 | 1.4889 | 2.0000 | 1.0667 | 1.8667 | 0.0000 | NA | NA | NA | 0.0000 | True |
| actionability | paired Wilcoxon signed-rank | 45 | -0.4000 | -1.0000 | -0.9111 | 0.1111 | 0.1272 | NA | NA | NA | 0.5312 | False |
| unsupported_claim | paired Wilcoxon signed-rank | 45 | 0.0667 | 0.0000 | -0.0667 | 0.2000 | 0.3173 | NA | NA | NA | 0.7726 | False |
| blind_preference | exact paired sign/binomial test; ties excluded from p | 45 | -0.0222 | NA | -0.3333 | 0.2889 | 1.0000 | 22.0000 | 23.0000 | 0.0000 | 1.0000 | False |

Tests are paired by question. Wilcoxon and exact sign tests are two-sided. Bootstrap intervals use seed 42. Holm-adjusted p-values address multiple listed comparisons. A small p-value does not by itself establish practical value.

## 18. Representative examples

Source: `results/summary/example_selection.csv`.

| question_id | difficulty | selection_reason |
| --- | --- | --- |
| financebench_id_00476 | easy | First ID in this difficulty with an incorrect or weak RAG answer. |
| financebench_id_00464 | easy | ASD-STE preference without lower judged fact coverage |
| financebench_id_00302 | medium | First ID in this difficulty with an incorrect or weak RAG answer. |
| financebench_id_00407 | medium | Possible information loss |
| financebench_id_00070 | hard | First ID in this difficulty with an incorrect or weak RAG answer. |
| financebench_id_00669 | hard | Possible information loss |

Six complete examples with both explanations, blind scores, and objective metrics are in `ARTICLE_EXAMPLES.md`. Selection includes failure cases and seeks ties and possible information loss where observed.

## 19. Unexpected findings

Measured facts: In this test, Dense retrieval had higher overall Hit@5 than the selected Hybrid + reranker pipeline. Answer correctness was low across all difficulty groups. The ASD-STE-inspired explanations were shorter and received higher conciseness ratings, but the blind preference was effectively split, and the clarity difference was not statistically significant after adjustment. The ASD explanations had a lower fact-coverage point estimate; that difference was also not significant after adjustment. The tables above give the exact estimates and uncertainty. Faithfulness and relevance ratings remained high even when answers were incorrect, so those evaluator dimensions do not substitute for answer correctness.

Possible explanations include parser fidelity, table extraction, evidence granularity, question difficulty, and local model behavior. This run does not isolate those causes. Some evidence-to-chunk mappings are uncertain, so retrieval comparisons depend in part on the constructed chunk labels.

## 20. Experimental limitations

The open subset is not the licensed full FinanceBench benchmark. The test split uses rule-based difficulty labels rather than independent expert difficulty ratings. Docling covers short PDFs, while long filings use positional extraction; some table layout can remain ambiguous. Gold evidence is often page-scale, so chunk mapping can be uncertain. One local Qwen judge provides semantic ratings. Blinded model ratings cannot establish real reader comprehension or formal ASD-STE100 compliance. The single workstation and serial Ollama calls limit performance generalization.

## 21. Failed runs and data quality

Source: `results/raw/test_per_query.csv`.

```json
{
  "failed_or_partial_test_queries": 0,
  "unmapped_gold_questions": 0,
  "uncertain_gold_evidence_items": 13
}
```

NA means no defensible value was calculated, such as an unambiguous numeric answer or a valid semantic judgment. NOT MEASURED applies to human comprehension outcomes. FAILED marks individual requests or documents that did not produce usable outputs; their raw attempts remain saved.

## 22. Raw files and reproducibility

- `results/asd_ste/asd_ste_per_query.csv`

- `results/asd_ste/asd_ste_summary.csv`

- `results/asd_ste/blind_style_mapping.json`

- `results/asd_ste/generated_pairs.jsonl`

- `results/asd_ste/human_evaluation_blinded.csv`

- `results/asd_ste/human_evaluation_template.csv`

- `results/asd_ste/packets.jsonl`

- `results/asd_ste/paired_statistics.csv`

- `results/asd_ste/pairs.jsonl`

- `results/chunk_manifest.json`

- `results/dataset_manifest.json`

- `results/dataset_manifest_original_90.json`

- `results/difficulty_assignment.csv`

- `results/difficulty_assignment_original_90.csv`

- `results/difficulty_assignment_pre_audit.csv`

- `results/environment.json`

- `results/frozen_configuration.json`

- `results/gold_chunk_mapping.json`

- `results/gold_mapping_summary.json`

- `results/gold_mapping_uncertain.csv`

- `results/index_manifest.json`

- `results/preflight_validation.json`

- `results/question_split.csv`

- `results/question_split_original_90.csv`

- `results/rag_results_frozen.json`

- `results/raw/api_attempts.jsonl`

- `results/raw/development_generation.jsonl`

- `results/raw/development_queries.jsonl`

- `results/raw/development_retrieval.jsonl`

- `results/raw/document_processing.csv`

- `results/raw/embedding_progress.json`

- `results/raw/retrieval_rankings.jsonl`

- `results/raw/test_generation.jsonl`

- `results/raw/test_per_query.csv`

- `results/raw/test_queries.jsonl`

- `results/raw/test_retrieval.jsonl`

- `results/report_recovery.json`

- `results/split_amendment.json`

- `results/summary/example_selection.csv`

- `results/summary/failure_distribution.csv`

- `results/summary/metrics_manifest.json`

- `results/summary/rag_results_by_difficulty.csv`

- `results/summary/retrieval_ablation.csv`

- `results/validation.json`

- `results/report_provenance.json`

- `results/report_consistency.json`

- `results/file_inventory.json`

- `results/pipeline_status.json`

- `results/autorun_final_status.json`

- `RESULTS.md`

- `ARTICLE_EXAMPLES.md`

- `README.md`

- `RUN_MANIFEST.md`

- `config.yaml`

- `requirements.txt`

The full per-query JSONL files retain nested rankings, contexts, prompts, answers, judgments, errors, and pair mapping. The reproduction commands are in `README.md`.
