# FinanceBench RAG difficulty and explanation experiment

This project runs two linked local experiments. First, it measures one fixed RAG
pipeline on 15 easy, 15 medium, and 15 hard FinanceBench questions. Second, it
compares normal and ASD-STE100-inspired explanations of those *frozen* RAG
results. ASD-STE100-inspired means controlled, simple technical writing; it is
not formal certification. The open FinanceBench release has 150 annotated
questions. The licensed larger benchmark is not included.

The supplied `FinanceRAG/data` was inspected. It has selected passages and
retrieval labels, but lacks benchmark answers and the complete PDF corpus. The
experiment therefore downloads the official open FinanceBench repository and
indexes every PDF in its `pdfs` directory. The benchmark source document and
gold evidence are used only after retrieval, for scoring.

The original pre-inference plan selected 30 questions per difficulty (90
total). On 2026-10-05, the user reduced the final test to 15 per difficulty
because local inference was substantially slower than expected. The original
90-question assignment and split are preserved in `results/*_original_90.*`.
The final 45 are a deterministic seed-42 subset of that original test pool;
the 30-question development set is unchanged. No final-test inference had
started at the time of the amendment. The smaller sample yields wider
uncertainty intervals and less power for the paired explanation comparison.

## Environment and input setup (Windows PowerShell)

Run these commands from this folder. Python 3.12 is preferred if installed;
this recorded run uses Python 3.14.3 because 3.12 was unavailable. `uv` and
Git must be installed. The model downloads and Git clone require network
access; all inference uses the local Ollama server.

```powershell
uv venv --python 3.14 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
git clone --filter=blob:none --sparse https://github.com/patronus-ai/financebench.git data/financebench_source
git -C data/financebench_source checkout cc39aeb4afdf33909ee1412188bf89035950c2eb
git -C data/financebench_source sparse-checkout set data pdfs
.venv\Scripts\python.exe -c "from huggingface_hub import snapshot_download; snapshot_download('BAAI/bge-base-en-v1.5', revision='a5beb1e3e68b9ab74eb54cfd186867f64f240e1a', local_dir='data/models/bge-base-en-v1.5'); snapshot_download('BAAI/bge-reranker-base', revision='2cfc18c9415c912f9d8155881c133215df768a70', local_dir='data/models/bge-reranker-base')"
ollama --version
ollama list
```

The run requires `mistral:latest` with digest
`6577803aa9a036369e481d648a2baebb381ebc6e897f2bb9a766a2aa7bfbc1cf`
and `qwen3.5:9b` with digest
`6488c96fa5faab64bb65cbd30d4289e20e6130ef535a93ef9a49f42eda893ea7`.
The code stops if either tag or digest differs. Confirm the Ollama server is
running before starting inference. GPU availability changes execution speed,
not model selection or scoring.

## Exact execution order

```powershell
Copy-Item configs\config_original_90.yaml config.yaml
.venv\Scripts\python.exe -B -m src.environment
.venv\Scripts\python.exe -B -m src.prepare
.venv\Scripts\python.exe -B -m src.corpus convert
.venv\Scripts\python.exe -B -m src.corpus chunk
.venv\Scripts\python.exe -B -m src.gold
.venv\Scripts\python.exe -B -m src.preflight
python -B -m src.index build
python -B -m src.retrieval_stage development
.venv\Scripts\python.exe -B -m unittest discover -s tests -v
.venv\Scripts\python.exe -B -m src.rag development
.venv\Scripts\python.exe -B -m src.amend_split
.venv\Scripts\python.exe -B -m src.preflight
.venv\Scripts\python.exe -B -m src.freeze
python -B -m src.retrieval_stage test
.venv\Scripts\python.exe -B -m src.rag test
.venv\Scripts\python.exe -B -m src.asd
.venv\Scripts\python.exe -B -m src.summarize
.venv\Scripts\python.exe -B -m src.environment --final
.venv\Scripts\python.exe -B -m src.report
```

`src.environment --final` records the experiment end time. Long stages write
progress to disk after each PDF, embedding batch, or question and can be
restarted with the same command. A rerun skips completed records. The test
stage verifies SHA-256 hashes of the frozen configuration, corpus, index,
evidence map, split, and test-affecting source code. Do not use `src.prepare
--force` after the freeze. `src.rag test` must run once under the frozen
settings; the resume mechanism is for interruption, not selective repair of
answers.

The recorded index stage uses the host `python` with CUDA-enabled
PyTorch 2.12.0+cu126, sentence-transformers 5.4.1, and transformers 5.7.0.
`config.yaml` requires CUDA for the one-time embedding build and for the
separate full-corpus retrieval/reranking pass. Ollama inference follows that
saved pass, so the local generator and judge do not compete with the BGE
reranker for VRAM. Before indexing, verify the host runtime
with `python -c "import torch; print(torch.__version__,
torch.cuda.is_available())"`; it must print a CUDA-enabled installation and
`True`. Install the pinned packages from `requirements.txt` for the main
`.venv`; the host CUDA runtime is a separate compute dependency and its exact
versions are saved in `results/index_manifest.json`. The document conversion,
gold mapping, Mistral/Qwen inference, and reports run in `.venv`.

For an unattended development run after conversion starts,
`.venv\Scripts\python.exe -B -m src.launch development` waits for all 368
documents, then runs chunking, gold mapping, indexing, tests, and the
development set. Inspect `results/pipeline_status.json` and the matching
`logs/<stage>.log`. Run `src.amend_split` only after all 30 development
evaluations finish and before any final-test inference. Then
`.venv\Scripts\python.exe -B -m src.launch final` runs the frozen 45-question
test and explanation stages. Alternatively, start
`.venv\Scripts\python.exe -B -m src.launch autofinal` **after the amendment**;
it checks all 30 development records and stops if any request, ranking,
evaluation, model tag, or permitted configuration comparison fails. Do not
launch both `final` and `autofinal`.

The order of `src.gold` and `src.index build` does not affect either result:
the evidence map uses document/page/text matching and never examines retrieval
rankings. All 368 PDFs are included before indexing. PDFs with up to 20 pages
use Docling with table recognition. Longer PDFs use PyMuPDF positional row
extraction; the fixed page-count rule is recorded in `config.yaml` and the
per-document log. OCR is disabled. This mixed parsing is a measured limitation
for image-only pages and complex long-document tables.

## Configuration, results, and interpretation

`config.yaml` fixes seed 42, 500-token recursive chunks with 75-token overlap,
BGE-base normalized embeddings with FAISS IndexFlatIP, BM25, dense/BM25 top
20, RRF `k=60`, BGE-reranker-base, final top five, Ollama temperature zero,
seed 42, context window 8192, and fixed output limits. Python computes
retrieval and unambiguous numeric metrics. Qwen provides semantic evaluation
and blinded explanation ratings. No LLM defines gold evidence or difficulty.
To prevent repeated model reloads, retrieval/reranking rankings are saved for
all questions before Ollama inference. All Mistral answers are generated and
saved before the Qwen evaluation pass. The paired explanation stage likewise
generates both styles for every packet before its Qwen blind-comparison pass.
Each model receives five warm-up calls before measured requests.

Raw requests, rankings, answers, judgments, errors, and paired explanations
are saved under `results/raw/` and `results/asd_ste/`. Aggregates and
validation are in `results/summary/` and `results/validation.json`.
`RESULTS.md` is the self-contained handoff; `ARTICLE_EXAMPLES.md`
contains six selected measured examples. Human comprehension fields are `NA`
because this run does not conduct a human study. Readability heuristics are
descriptive, not a direct measure of comprehension.

See `RUN_MANIFEST.md` for dataset revision, preparation decisions, stage
status, and run-specific limitations. `results/environment.json` records
hardware, exact tags/digests, source revision, package versions, and times.
