# Local decision-model RAG evaluation

This experiment compares Tev1 and Nimble with approximately similar-size Qwen3.5 generative judges, using the local Ollama service. The dataset contains sixty source questions with five controlled variants each.

Follow [RUN_EXPERIMENT.md](RUN_EXPERIMENT.md) for the authoritative commands, metric definitions, settings, resume behavior, and methodological limitations. The current workflow is implemented in `setup_study.py`, `study_judges.py`, `run_study.py`, `analyze_study.py`, `plot_study.py`, and `report_study.py`. `run_all.py` orchestrates those steps after dataset preparation.

Live progress is saved in `results/progress.json` and `results/orchestration_status.json`. Raw request attempts, including responses and errors, are preserved in `results/requests.jsonl`. Completed primary predictions are exported to `results/raw_predictions.csv`.

The final `RESULTS.md` is generated only after the complete request matrix has recorded outcomes. `results/final_integrity_check.json` and `results/report_consistency.json` verify its inputs and quoted table values. Interim tables and figures are preliminary while experiments remain incomplete.

The supplied article and legacy scripts are retained in this folder; historic pilot and published-benchmark files are in `results/`. No edits were made to the original article. Its published benchmark figures are external context, not results from this local study. Construction labels are retained and their known semantic limitations are documented in `results/dataset_validation.json`.
