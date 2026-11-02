"""Descriptive summaries and task-clustered paired differences; no LLM judging."""
import argparse
from collections import defaultdict
import csv
import json
from pathlib import Path
import random
from statistics import mean

INFRASTRUCTURE = {"api_error", "usage_missing", "budget_overrun", "protocol_error", "evaluator_error"}
METRICS = ("operational_task_success", "completed_episode_task_success", "verified_task_success",
           "tool_calls", "unnecessary_tool_calls", "estimated_api_cost_usd", "retries",
           "constraint_violations", "correct_stopping", "false_completion_claim", "recovery_accuracy",
           "stopping_accuracy", "total_tokens", "latency_seconds")


def cluster_ci(values, seed=20261027, samples=3000):
    """Bootstrap across task-level means; with <8 tasks, omit unreliable intervals."""
    if len(values) < 8:
        return None, None
    rng = random.Random(seed)
    draws = sorted(mean(rng.choices(values, k=len(values))) for _ in range(samples))
    return draws[int(samples * .025)], draws[int(samples * .975)]


def attempted(row):
    return row.get("model_calls", 0) > 0


def output_truncated(row):
    if row["termination_reason"] != "incomplete_response":
        return False
    interactions = row.get("interactions") or []
    response = (interactions[-1].get("response") or {}) if interactions else {}
    return (response.get("incomplete_details") or {}).get("reason") == "max_output_tokens"


def episode_outcome(row):
    """Operational outcomes, without altering the saved deterministic evaluation."""
    termination = row["termination_reason"]
    if termination == "budget_limit" and not attempted(row):
        return "budget_censored"
    if not attempted(row) or termination in INFRASTRUCTURE:
        return "infrastructure"
    if termination == "completed":
        if row["verified_task_success"] is None:
            return "infrastructure"
        return "success" if row["verified_task_success"] and row.get("valid_final_report", True) else "failure"
    if termination in ("model_limit", "tool_limit", "request_size_limit", "budget_limit") or output_truncated(row):
        return "failure"
    # An incomplete response is not necessarily output truncation: e.g. provider
    # cancellation/content filtering or an absent reason needs separate investigation.
    return "infrastructure"


def eligible(row):
    return episode_outcome(row) in ("success", "failure")


def analysis_metrics(row):
    outcome = episode_outcome(row)
    operational = outcome == "success" if outcome in ("success", "failure") else None
    completed = operational if row["termination_reason"] == "completed" else None
    return {**row, "operational_task_success": operational,
            "completed_episode_task_success": completed}


def summarize(rows, seed, by_task=False):
    groups = defaultdict(list)
    for row in rows:
        for category in ((row["task_id"],) if by_task else (row["category"], "all")):
            groups[(row["model"], row["condition"], category)].append(row)
    summaries = []
    for (model, condition, category), group in sorted(groups.items()):
        valid = [analysis_metrics(r) for r in group if eligible(r)]
        completed = [r for r in valid if r["completed_episode_task_success"] is not None]
        attempts = sum(attempted(r) for r in group)
        outcomes = [episode_outcome(r) for r in group]
        tasks = defaultdict(list)
        for row in valid:
            tasks[row["task_id"]].append(int(row["operational_task_success"]))
        task_means = [mean(v) for v in tasks.values()]
        low, high = cluster_ci(task_means, seed)
        successful = sum(r["operational_task_success"] for r in valid)
        completed_tasks = defaultdict(list)
        for row in completed:
            completed_tasks[row["task_id"]].append(int(row["completed_episode_task_success"]))
        limit_hits = sum(r["termination_reason"] in ("model_limit", "tool_limit") and attempted(r) for r in group)
        truncations = sum(output_truncated(r) and attempted(r) for r in group)
        known_cost = sum(r["known_api_cost_usd"] for r in group)
        cost_complete = all(r["estimated_api_cost_usd"] is not None for r in group)
        summaries.append({
            "model": model, "condition": condition, "task_id" if by_task else "category": category,
            "episodes_recorded": len(group), "episodes_evaluable": len(valid),
            "attempted_episodes": attempts, "operational_successes": successful,
            "operational_failures": len(valid) - successful,
            "infrastructure_exclusions": outcomes.count("infrastructure"),
            "budget_censored_episodes": outcomes.count("budget_censored"),
            "attempted_budget_interruptions": sum(r["termination_reason"] == "budget_limit" and attempted(r) for r in group),
            "excluded_episodes": len(group) - len(valid),
            "exclusion_rate": (len(group) - len(valid)) / len(group),
            "infrastructure_exclusion_rate": outcomes.count("infrastructure") / len(group),
            "budget_censoring_rate": outcomes.count("budget_censored") / len(group),
            "truncations": truncations, "truncation_rate": truncations / attempts if attempts else None,
            "limit_hits": limit_hits, "limit_hit_rate": limit_hits / attempts if attempts else None,
            "model_call_limit_hits": sum(r["termination_reason"] == "model_limit" and attempted(r) for r in group),
            "tool_call_limit_hits": sum(r["termination_reason"] == "tool_limit" and attempted(r) for r in group),
            "request_size_limit_hits": sum(r["termination_reason"] == "request_size_limit" and attempted(r) for r in group),
            "invalid_final_reports": sum(r.get("valid_final_report") is False for r in completed),
            "completed_episodes": len(completed),
            "unique_tasks": len(tasks),
            "success_rate": successful / len(valid) if valid else None,
            "operational_success_rate": successful / len(valid) if valid else None,
            "episode_success_rate": successful / len(valid) if valid else None,
            "completed_episode_success_rate": mean(r["completed_episode_task_success"] for r in completed) if completed else None,
            "completed_task_macro_success_rate": mean(mean(v) for v in completed_tasks.values()) if completed_tasks else None,
            "task_macro_success_rate": mean(task_means) if task_means else None,
            "task_macro_success_ci_low": low, "task_macro_success_ci_high": high,
            "tool_calls_per_episode": mean(r["tool_calls"] for r in valid) if valid else None,
            "retry_episode_rate": mean(r["retries"] > 0 for r in valid) if valid else None,
            "retries_per_episode": mean(r["retries"] for r in valid) if valid else None,
            "violation_episode_rate": mean(r["constraint_violations"] > 0 for r in valid) if valid else None,
            "false_completion_rate": mean(r["false_completion_claim"] for r in valid) if valid else None,
            "correct_stopping_rate": mean(r["correct_stopping"] for r in valid) if valid else None,
            "known_cost_usd": known_cost, "cost_complete": cost_complete,
            "cost_per_success_usd": known_cost / successful if successful and cost_complete else None,
        })
        for metric in ("unnecessary_tool_calls", "constraint_violations", "recovery_accuracy",
                       "stopping_accuracy", "total_tokens", "latency_seconds", "estimated_api_cost_usd"):
            values = [r[metric] for r in valid if r.get(metric) is not None]
            summaries[-1]["mean_" + metric] = mean(values) if values else None
    return summaries


def paired(rows, seed, by_task=False):
    # Require the same run, task, repetition and scheduling seed before pairing.
    blocks = defaultdict(dict)
    for row in rows:
        if not eligible(row):
            continue
        row = analysis_metrics(row)
        key = (row["model"], row["category"], row["run_id"], row["task_id"], row["repetition"], row["seed"])
        if row["condition"] in blocks[key]:
            raise ValueError("Duplicate episode in analysis input")
        blocks[key][row["condition"]] = row
    contrasts = {
        "exploratory_combined_rivalry_vs_pressure": {"C": .5, "D": .5, "B": -1},
        "primary_generic_vs_pressure": {"C": 1, "B": -1},
        "named_vs_pressure": {"D": 1, "B": -1},
        "named_vs_generic": {"D": 1, "C": -1},
        "pressure_vs_neutral": {"B": 1, "A": -1},
        "generic_vs_neutral": {"C": 1, "A": -1},
        "named_vs_neutral": {"D": 1, "A": -1},
    }
    differences = defaultdict(lambda: defaultdict(list))
    for (model, category, _, task, _, _), conditions in blocks.items():
        for contrast, weights in contrasts.items():
            if not weights.keys() <= conditions.keys():
                continue
            for metric in METRICS:
                if any(conditions[c].get(metric) is None for c in weights):
                    continue
                delta = sum(conditions[c][metric] * w for c, w in weights.items())
                for cat in ((task,) if by_task else (category, "all")):
                    differences[(model, cat, contrast, metric)][task].append(delta)
    result = []
    for (model, category, contrast, metric), tasks in sorted(differences.items()):
        values = [mean(repetitions) for repetitions in tasks.values()]
        low, high = cluster_ci(values, seed)
        result.append({"model": model, "task_id" if by_task else "category": category, "comparison": contrast,
                       "metric": metric, "paired_tasks": len(tasks),
                       "paired_blocks": sum(len(v) for v in tasks.values()),
                       "mean_paired_difference": mean(values), "ci_low": low, "ci_high": high})
    return result


def write_csv(path, rows):
    if not rows:
        Path(path).write_text("", encoding="utf-8")
        return
    with Path(path).open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_reports(rows, directory, *, include_dry_run=False, seed=20261027):
    rows = [r for r in rows if include_dry_run or not r["dry_run"]]
    # Pool only compatible execution settings within a model.
    configurations = defaultdict(set)
    for row in rows:
        configurations[row["model"]].add(json.dumps(
            [row.get("limits"), row.get("reasoning_effort"), row.get("source_sha256"),
             row.get("pilot_manifest_sha256"), row["dry_run"]], sort_keys=True))
    if any(len(configs) > 1 for configs in configurations.values()):
        raise ValueError("Analyze different execution configurations separately")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    write_csv(directory / "summary.csv", summarize(rows, seed))
    write_csv(directory / "paired.csv", paired(rows, seed))
    write_csv(directory / "task_summary.csv", summarize(rows, seed, by_task=True))
    write_csv(directory / "task_paired.csv", paired(rows, seed, by_task=True))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path, nargs="+")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--include-dry-run", action="store_true")
    parser.add_argument("--seed", type=int, default=20261027)
    args = parser.parse_args(argv)
    rows = [json.loads(line) for path in args.results for line in path.read_text(encoding="utf-8").splitlines()]
    write_reports(rows, args.output_dir, include_dry_run=args.include_dry_run, seed=args.seed)
    print("Wrote summary.csv, paired.csv, task_summary.csv and task_paired.csv. Descriptive only; inspect exclusions.")


if __name__ == "__main__":
    main()
