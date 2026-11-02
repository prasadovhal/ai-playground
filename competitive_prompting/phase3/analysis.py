"""Phase 3 task-clustered descriptive analysis, separated from development data."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import random
from statistics import mean

from .. import analysis as original
from .tasks import BY_ID
from .evaluation import oracle

METRICS = ("operational_task_success", "completed_episode_task_success", "constraint_violations",
    "unnecessary_tool_calls", "tool_calls", "incorrect_retries", "duplicate_operations", "uninspected_write_retries",
    "recovery_accuracy", "stopping_accuracy", "total_tokens", "estimated_api_cost_usd", "latency_seconds")
CONTRASTS = {"primary_C_minus_B": {"C": 1, "B": -1}, "named_D_minus_C": {"D": 1, "C": -1},
    "pressure_B_minus_A": {"B": 1, "A": -1}, "generic_C_minus_A": {"C": 1, "A": -1},
    "named_D_minus_A": {"D": 1, "A": -1}, "exploratory_combined_minus_B": {"C": .5, "D": .5, "B": -1}}


def task_interval(task_values, seed=20261027, samples=3000):
    if len(task_values) < 8:
        return None, None
    strata = defaultdict(list)
    for task, value in task_values.items():
        strata[BY_ID[task].task.category].append(value)
    rng = random.Random(seed)
    draws = []
    for _ in range(samples):
        values = [v for category in sorted(strata) for v in rng.choices(strata[category], k=len(strata[category]))]
        draws.append(mean(values))
    draws.sort()
    return draws[int(samples * .025)], draws[int(samples * .975)]


def paired(rows):
    blocks = defaultdict(dict)
    for row in rows:
        if not original.eligible(row):
            continue
        row = original.analysis_metrics(row)
        key = (row["model"], row["task_id"], row["repetition"], row["seed"])
        if row["condition"] in blocks[key]:
            raise ValueError("Duplicate Phase 3 episode")
        blocks[key][row["condition"]] = row
    differences = defaultdict(lambda: defaultdict(list))
    for (model, task, _, _), conditions in blocks.items():
        for contrast, weights in CONTRASTS.items():
            if not weights.keys() <= conditions.keys():
                continue
            for metric in METRICS:
                if any(conditions[c].get(metric) is None for c in weights):
                    continue
                difference = sum(conditions[c][metric] * weight for c, weight in weights.items())
                for category in (BY_ID[task].task.category, "all"):
                    differences[(model, category, contrast, metric)][task].append(difference)
    result = []
    for (model, category, contrast, metric), tasks in sorted(differences.items()):
        values = {task: mean(repeated) for task, repeated in tasks.items()}
        low, high = task_interval(values)
        result.append({"model": model, "category": category, "comparison": contrast, "metric": metric,
            "paired_tasks": len(tasks), "paired_repeated_blocks": sum(len(v) for v in tasks.values()),
            "mean_task_paired_difference": mean(values.values()), "ci_low": low, "ci_high": high,
            "interval_basis": "task bootstrap, stratified by category; exploratory; no equivalence claim"})
    return result


def write_reports(rows, directory, *, include_development=False, include_dry_run=False):
    collection = "development" if include_development else "heldout"
    rows = [r for r in rows if r["collection"] == collection and (include_dry_run or not r["dry_run"])]
    signatures = {(r["benchmark_manifest_sha256"], r["source_sha256"], r["dry_run"]) for r in rows}
    if len(signatures) > 1:
        raise ValueError("Do not pool benchmark versions, execution settings or live/offline data")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    summaries = original.summarize(rows, 20261027)
    for summary in summaries:
        group = [r for r in rows if original.eligible(r) and r["model"] == summary["model"] and
                 r["condition"] == summary["condition"] and summary["category"] in ("all", r["category"])]
        for metric in ("incorrect_retries", "duplicate_operations", "uninspected_write_retries"):
            values = [r[metric] for r in group if r.get(metric) is not None]
            summary["mean_" + metric] = mean(values) if values else None
    original.write_csv(directory / "summary.csv", summaries)
    original.write_csv(directory / "task_summary.csv", original.summarize(rows, 20261027, by_task=True))
    original.write_csv(directory / "paired.csv", paired(rows) if collection == "heldout" else [])
    difficulty = []
    for tid in sorted({r["task_id"] for r in rows}):
        case = BY_ID[tid]
        difficulty.append({"task_id": tid, "category": case.task.category, "collection": collection,
            "records": len(case.task.records), "oracle_tool_calls": len(oracle(case)["actions"]),
            "scheduled_faults": len(case.task.failures), "expected_status": case.task.expected_status})
    original.write_csv(directory / "difficulty.csv", difficulty)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", nargs="+", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--development-only", action="store_true")
    parser.add_argument("--include-dry-run", action="store_true")
    args = parser.parse_args(argv)
    rows = [json.loads(line) for path in args.results for line in path.read_text().splitlines()]
    write_reports(rows, args.output_dir, include_development=args.development_only, include_dry_run=args.include_dry_run)


if __name__ == "__main__":
    main()
