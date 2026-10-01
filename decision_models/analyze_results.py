from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.metrics import (
    accuracy_score,
    brier_score_loss,
    cohen_kappa_score,
    mean_absolute_error,
    precision_recall_fscore_support,
)


def expected_calibration_error(y_true: np.ndarray, prob: np.ndarray, bins: int = 10) -> float:
    edges = np.linspace(0, 1, bins + 1)
    ece = 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        mask = (prob >= lo) & (prob < hi if hi < 1 else prob <= hi)
        if not mask.any():
            continue
        acc = y_true[mask].mean()
        conf = prob[mask].mean()
        ece += mask.mean() * abs(acc - conf)
    return float(ece)


def binary_metrics(g: pd.DataFrame) -> dict:
    y = g["gold_answerable"].astype(int).to_numpy()
    p = g["answerable_pred"].astype(int).to_numpy()
    precision, recall, f1, _ = precision_recall_fscore_support(y, p, average="binary", zero_division=0)
    d = {
        "answerability_accuracy": accuracy_score(y, p),
        "answerability_precision": precision,
        "answerability_recall": recall,
        "answerability_f1": f1,
    }
    if g["answerable_prob"].notna().all():
        prob = g["answerable_prob"].astype(float).to_numpy()
        d["answerability_brier"] = brier_score_loss(y, prob)
        d["answerability_ece10"] = expected_calibration_error(y, prob, bins=10)
    return d


def ordinal_metrics(g: pd.DataFrame, task: str) -> dict:
    y = g[f"gold_{task}"].astype(int).to_numpy()
    p = g[f"{task}_pred"].astype(int).to_numpy()
    rho = spearmanr(y, p).statistic if len(np.unique(p)) > 1 else np.nan
    return {
        f"{task}_exact": accuracy_score(y, p),
        f"{task}_mae": mean_absolute_error(y, p),
        f"{task}_qwk": cohen_kappa_score(y, p, weights="quadratic"),
        f"{task}_spearman": rho,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--predictions", default="results/predictions.csv")
    p.add_argument("--outdir", default="results")
    args = p.parse_args()

    df = pd.read_csv(args.predictions)
    df = df[df["error"].isna()].copy()
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    rows = []
    for model, g in df.groupby("model", sort=False):
        d = {"model": model, "judge_type": g["judge_type"].iloc[0], "n": len(g)}
        d.update(binary_metrics(g))
        d.update(ordinal_metrics(g, "groundedness"))
        d.update(ordinal_metrics(g, "relevance"))
        d.update({
            "latency_median_s": g["latency_s"].median(),
            "latency_p90_s": g["latency_s"].quantile(0.90),
            "latency_p95_s": g["latency_s"].quantile(0.95),
            "input_tokens_mean": g["input_tokens"].mean(),
            "output_tokens_mean": g["output_tokens"].mean(),
        })
        rows.append(d)

    summary = pd.DataFrame(rows)
    summary.to_csv(outdir / "summary.csv", index=False)
    print("\nOVERALL SUMMARY\n")
    print(summary.to_string(index=False))

    by_variant = (
        df.assign(
            answerability_correct=lambda x: (x.gold_answerable == x.answerable_pred).astype(int),
            groundedness_abs_error=lambda x: (x.gold_groundedness - x.groundedness_pred).abs(),
            relevance_abs_error=lambda x: (x.gold_relevance - x.relevance_pred).abs(),
        )
        .groupby(["model", "variant"], as_index=False)
        .agg(
            n=("case_id", "size"),
            answerability_accuracy=("answerability_correct", "mean"),
            groundedness_mae=("groundedness_abs_error", "mean"),
            relevance_mae=("relevance_abs_error", "mean"),
            median_latency_s=("latency_s", "median"),
        )
    )
    by_variant.to_csv(outdir / "by_variant.csv", index=False)
    print("\nBY VARIANT\n")
    print(by_variant.to_string(index=False))


if __name__ == "__main__":
    main()
