from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

import pandas as pd
from tqdm import tqdm

from judges import generative_judge, systemone_judge

DEFAULT_MODELS = [
    ("tev1:0.8b", "decision"),
    ("qwen3.5:0.8b", "generative"),
    ("tev1:4b", "decision"),
    ("qwen3.5:4b", "generative"),
    ("nimble", "decision"),
    ("qwen3.5:9b", "generative"),
]


def load_cases(path: str) -> list[dict]:
    return pd.read_json(path, lines=True).to_dict(orient="records")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--data", default="data/eval_300.jsonl")
    p.add_argument("--out", default="results/predictions.csv")
    p.add_argument("--models", nargs="*", help="Optional model names. Type is inferred: tev1/nimble=decision.")
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args()

    cases = load_cases(args.data)
    if args.limit:
        cases = cases[: args.limit]

    models = DEFAULT_MODELS
    if args.models:
        models = [(m, "decision" if m.startswith("tev1") or m == "nimble" else "generative") for m in args.models]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    completed: set[tuple[str, str]] = set()
    existing = pd.DataFrame()
    if out.exists():
        existing = pd.read_csv(out)
        completed = set(zip(existing["case_id"].astype(str), existing["model"].astype(str)))

    records = []
    for model, kind in models:
        print(f"\n=== {model} ({kind}) ===")
        for case in tqdm(cases, desc=model):
            key = (str(case["case_id"]), model)
            if key in completed:
                continue
            try:
                result = systemone_judge(case, model) if kind == "decision" else generative_judge(case, model)
                rec = {
                    "case_id": case["case_id"],
                    "source_id": case["source_id"],
                    "variant": case["variant"],
                    "gold_answerable": case["answerable"],
                    "gold_groundedness": case["groundedness"],
                    "gold_relevance": case["relevance"],
                    **{k: v for k, v in asdict(result).items() if k != "raw"},
                    "error": None,
                }
            except Exception as e:
                rec = {
                    "case_id": case["case_id"],
                    "source_id": case["source_id"],
                    "variant": case["variant"],
                    "gold_answerable": case["answerable"],
                    "gold_groundedness": case["groundedness"],
                    "gold_relevance": case["relevance"],
                    "model": model,
                    "judge_type": kind,
                    "error": repr(e),
                }
            records.append(rec)

            # Save incrementally so a long local run can resume.
            if len(records) >= 10:
                new = pd.DataFrame(records)
                existing = pd.concat([existing, new], ignore_index=True)
                existing.to_csv(out, index=False)
                records.clear()

    if records:
        existing = pd.concat([existing, pd.DataFrame(records)], ignore_index=True)
        existing.to_csv(out, index=False)

    print(f"Saved predictions to {out}")


if __name__ == "__main__":
    main()
