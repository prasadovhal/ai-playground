# Reproduce the local decision-judge study

Run commands from this directory. Python 3.14.3 and Ollama 0.35.0 were used for this execution. Exact installed package versions, hardware, model digests, parameter counts, quantization, templates, and model parameters are in `results/environment.json`.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python setup_study.py --source "D:\Study\Git_repo\rag_tutorials\competitions\LLM_science_exam\data\train.csv"
.\.venv\Scripts\python setup_study.py --pull
.\.venv\Scripts\python -m unittest test_study -v
.\.venv\Scripts\python run_study.py
.\.venv\Scripts\python setup_study.py --environment
.\.venv\Scripts\python analyze_study.py
.\.venv\Scripts\python plot_study.py
.\.venv\Scripts\python report_study.py
```

For unattended execution after dataset preparation, run `python run_all.py`. The controller performs model checks, sequential resumable runs, analysis, plots, report generation, and consistency checks. On Windows it temporarily requests that the system remain awake while it runs. Read `results/orchestration_status.json` and `results/controller_stdout.log` for progress when using the background controller launched in this session.

For another checkout, provide the path to the official competition `train.csv`. A copy of the exact source used here is retained in `results/source_train.csv`. Its SHA-256 is recorded in `dataset_validation.json`.

The local Ollama service must already be running. All inference calls go to `http://localhost:11434`; no cloud inference or model-generated reference labels are used. Model downloads contact the Ollama registry through the local service.

## Authoritative implementation

Use `setup_study.py`, `study_judges.py`, `run_study.py`, `analyze_study.py`, `plot_study.py`, and `report_study.py` for this run. The supplied scripts remain available for inspection in this folder. The original article is unchanged. `results/original_inventory.json` records the relocated supplied files and flags a checksum change in the legacy status note after the initial inventory. Its historic environment statements and external benchmark numbers do not describe this execution.

The updated workflow adds warm-up, complete raw-response retention, immediate durable checkpoints, bounded retries, resource monitoring, every requested secondary experiment, calibration, and clustered uncertainty estimates. `requirements.txt` pins the packages used by the updated workflow. The supplied legacy runner additionally requires `tqdm` if run separately.

## Dataset and methodological decisions

The source contains two hundred records. The requested seed is applied with `numpy.random.RandomState(42).choice(sorted_ids, 60, replace=False)`. Each selected question receives the same five transformations from the supplied `prepare_dataset.py`. The original hard-coded sample had no derivation from the requested seed. Its dataset and IDs are preserved separately; no original model predictions were supplied.

The structural validator checks source size, IDs, option matching, nonempty fields, counts, label ranges, and deterministic regeneration. These checks do not establish semantic correctness. `dataset_validation.json` explicitly records that limitation.

The original construction labels are retained. A wrong option can contain supported subclaims, the concatenated partial answer need not be mostly supported, and an offset source need not be unrelated. Bare option fragments may also be ambiguous evidence. `construction_audit.csv` flags exact repeated sentences without introducing new labels. Report the primary metrics as agreement with constructed references, not human accuracy or factual ground truth.

## Fixed protocol

`results/protocol.json` is written before inference and checked on resume. The primary dataset has three hundred cases. The stability subset contains fifty cases from ten source questions, with all five variants and three fresh repetitions. Prompt sensitivity uses twenty-five cases with a common base rubric and two alternate formulations. Injection tests use twenty cases and four placements. Context-noise tests use twenty-five cases at four target lengths.

All sampling, generation, and bootstrap seeds are forty-two. Stability input order uses the seed plus the repeat index. Generative judges use temperature zero, thinking disabled, a JSON response, a generation cap of one hundred twenty-eight tokens, and a context setting of four thousand ninety-six tokens. Decision judges use native System One settings; that endpoint does not expose an equivalent generation temperature in this workflow.

The rubric text and ordering are identical across model families as far as the APIs allow. Models receive only the question, context, evaluated answer, and common rubric. Reference labels, variant names, source IDs, and correct options are excluded from requests. No model-specific prompt tuning is performed.

One model runs at a time. Each resumed model session receives five successful HTTP warm-up calls before benchmark requests. Initial model loads remain in the warm-up log. Timing uses wall-clock HTTP request duration; download time is excluded. Tev1's active context length is recorded in the load events. Observed System One errors explicitly reject inputs above that window and state that input is never truncated.

Noise length is estimated as characters divided by four. It is not an exact tokenizer count. Original evidence remains intact in the submitted payload, surrounded by synthetic administrative text. System One capacity rejections remain recorded failures. The context-noise plots count failed evaluations as unsuccessful. Generative truncation is not independently verified, and interface context budgets differ.

## Resume and errors

```powershell
python run_study.py
python run_study.py --models nimble:9b --retry-failed
python run_study.py --export-only
```

Each request attempt, including errors, is appended and flushed to `results/requests.jsonl`. CSV exports use the most recent attempt per job. Completed jobs are skipped. A failed request receives a bounded retry; rerun with `--retry-failed` to retry terminal errors while preserving earlier attempts. Consecutive infrastructure failures or unsuccessful HTTP warm-up stop that model for inspection. Served but malformed predictions remain recorded evaluation failures and do not stop a model's entire benchmark.

Do not run multiple evaluators concurrently because they would contend for hardware and distort latency. If the machine loses power during a log write, preserve the original log before repairing an incomplete final line. The loader refuses corrupt JSONL instead of silently deleting it.

## Metric definitions

Exact agreement and F1 are computed on valid responses with the valid count and failure rate shown. Additional `agreement_all_attempted` columns count failed predictions as unsuccessful. No prediction or probability is imputed. AUROC requires observed probability data and both reference classes. Generative outputs have no reported probabilities, so their calibration metrics remain unavailable.

Ordinal predictions use the maximum-probability class, not a rounded expected score. Linear weighted kappa is the main ordinal summary; quadratic kappa, MAE, Spearman correlation, and confusion matrices are also retained. Spearman and some kappas are undefined for constant reference labels within a variant, and stay missing.

Binary Brier uses the squared error of the positive-class probability. Ordinal Brier is the sum of squared errors across classes, averaged across cases. Top-label ECE compares selected-label probability with observed correctness across ten bins covering the entire probability range. Binary event-probability ECE is reported separately. API `confidence` is retained separately from selected-label probability.

Bootstrap intervals resample source questions with all their variants together. The analysis uses two thousand resamples with a fixed seed. This accounts for the five related cases per question. It does not remove label-construction bias or establish generalization to natural RAG answers.

Selective escalation is an offline replay of paired primary predictions. A case stays with the decision model only when the minimum selected-label probability across all three dimensions meets the threshold. Estimated latency adds the measured decision latency and, when escalated, the measured fallback latency. Model swaps, cold reloads, and a deployed queue are not measured. Avoided generative calls are reported separately from total request count and token estimates.

Injection success is an attack-caused score increase on cases whose reference is below the maximum. New-maximum attacks and deterioration from initially correct predictions are also reported. Both clean and attacked outputs are retained. Embedded instructions are treated as untrusted text, and the underlying substantive-answer labels remain unchanged.

Resource usage is sampled periodically for every model. Ollama process RSS can double-count shared memory. GPU memory is device-wide and includes other applications. The machine is not a dedicated isolated benchmark server; download overlap and active-process conditions are recorded as limitations.

## Final artifacts

`RESULTS.md` is produced only when the complete request matrix has terminal recorded outcomes. Its tables are rendered directly from result CSVs. `results/report_provenance.json` maps table values to source cells, while `results/report_consistency.json` records verification. Missing or failed measurements remain explicit. The report contains the requested sections and does not rewrite the article.
