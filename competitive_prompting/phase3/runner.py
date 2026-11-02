"""Phase 3 preparation and staged execution. Offline unless --execute is explicit."""
import argparse
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path

from .. import runner as shared
from .. import analysis as original_analysis
from ..budget import Budget, BudgetError, RunLock, replay_ledger, money
from .tasks import BY_ID, VERSION
from .evaluation import OracleResponses, evaluate
from .preparation import (ROOT, MODELS, LIMITS, DEV_CAP, PHASE_CAP, build_manifest,
                         load_manifest, prepare, source_hash)


class Phase3Budget(Budget):
    """Same persistent ledger and global cap, plus cumulative Phase 3/dev ceilings."""
    def __init__(self, path, collection):
        if collection not in ("development", "heldout"):
            raise ValueError("Unknown Phase 3 collection")
        self.collection = collection
        super().__init__(path, stage="main", total_cap=9, remaining=6)

    def reserve(self, allowance, episode_id):
        if not episode_id.startswith(f"phase3:{self.collection}:"):
            raise BudgetError("Phase 3 reservation must use its frozen collection namespace")
        return super().reserve(allowance, episode_id)

    def phase_exposure(self, development_only=False):
        prefix = "phase3:development:" if development_only else "phase3:"
        return sum((money(e["cost"] if e["state"] == "settled" else e["allowance"])
                    for e in replay_ledger(self.entries).values() if e["episode_id"].startswith(prefix)), Decimal(0))

    def require_available(self, allowance):
        super().require_available(allowance)
        allowance = money(allowance)
        if self.phase_exposure() + allowance > PHASE_CAP:
            raise BudgetError("Cumulative Phase 3 additional exposure cap ($6) would be exceeded")
        if self.collection == "development" and self.phase_exposure(True) + allowance > DEV_CAP:
            raise BudgetError("Cumulative neutral development cap ($0.50) would be exceeded")


def audit_row(case, row):
    # Keep all original metric fields and definitions; an independent disagreement
    # becomes infrastructure failure, never an optimized or relabeled model result.
    try:
        score = evaluate(case, row["action_trace"], row["final_state"], row["final_text"], row["termination_reason"])
        for metric in ("constraint_violations", "duplicate_operations", "valid_final_report"):
            if score[metric] != row[metric]:
                raise ValueError(f"Independent metric disagreement: {metric}")
        if row["termination_reason"] == "completed" and score["verified_task_success"] != row["verified_task_success"]:
            raise ValueError("Independent primary outcome disagrees with simulator")
        row["independent_evaluation"] = score
        row["uninspected_write_retries"] = score["uninspected_write_retries"]
        row["independent_audit_passed"] = True
    except ValueError as exc:
        row["original_termination_reason"] = row["termination_reason"]
        row["termination_reason"] = "evaluator_error"
        row["verified_task_success"] = None
        row["independent_audit_passed"] = False
        row["api_errors"].append({"type": "EvaluatorDisagreement", "message": str(exc)})
    return row


def infrastructure_complete(row):
    return (row["independent_audit_passed"] and row["termination_reason"] not in shared.INFRASTRUCTURE_STOPS
            and original_analysis.eligible(row))


def check_development_release(path, manifest):
    """Explicit human review, bound to the exact heldout plan and dev records."""
    approval = json.loads(Path(path).read_text(encoding="utf-8"))
    if (approval.get("decision") != "freeze" or approval.get("heldout_manifest_sha256") != manifest["sha256"]
            or not approval.get("reviewer") or not approval.get("difficulty_review")):
        raise ValueError("Heldout release requires explicit, documented development difficulty review")
    files = [Path(p) for p in approval["development_results"]]
    digest = hashlib.sha256(b"".join(p.read_bytes() for p in files)).hexdigest()
    if digest != approval["development_results_sha256"]:
        raise ValueError("Development review/result hash mismatch")
    rows = [json.loads(line) for path in files for line in path.read_text(encoding="utf-8").splitlines()]
    dev = build_manifest("development")
    if {r["episode_id"] for r in rows} != {p["episode_id"] for p in dev["plan"]} or len(rows) != 12:
        raise ValueError("All 12 neutral development episodes are required for the heldout release review")
    if any(r["dry_run"] or r["condition"] != "A" or r["collection"] != "development" or
           r["benchmark_manifest_sha256"] != dev["sha256"] or not infrastructure_complete(r) for r in rows):
        raise ValueError("Development must be live, neutral-only, complete and independently audited")
    return approval


def run_stage(manifest, stage, directory, *, execute=False, release=None):
    plan = [p for p in manifest["plan"] if p["stage"] == stage]
    if not plan:
        raise ValueError("Unknown stage")
    collection = manifest["collection"]
    if execute and collection == "heldout":
        if release is None:
            raise ValueError("Paid heldout evaluation is blocked pending a documented development review")
        check_development_release(release, manifest)
    if execute and (not os.environ.get("OPENAI_API_KEY") or not os.environ.get("OPENAI_PROJECT_ID")):
        raise ValueError("Set OPENAI_API_KEY and OPENAI_PROJECT_ID securely before paid execution")
    directory = Path(directory) / f"{VERSION}-{collection}-stage{stage}-{'live' if execute else 'offline'}"
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    rows = []

    def episodes(budget=None, live=None):
        for item in plan:
            case = BY_ID[item["task_id"]]
            row = shared.run_episode(case.task, item["condition"], item["model"], item["repetition"], item["seed"],
                live or OracleResponses(case), limits=LIMITS, effort="low", dry_run=not execute,
                budget=budget, pricing=manifest["pricing"]["models"][item["model"]],
                episode_id=item["episode_id"], run_id=f"phase3-{VERSION}-{collection}-stage{stage}")
            row.update(benchmark=VERSION, collection=collection, stage=stage, execution_order=item["order"],
                       benchmark_manifest_sha256=manifest["sha256"], source_sha256=source_hash())
            audit_row(case, row)
            shared.write_result(directory / "episodes.jsonl", row)
            rows.append(row)
            print(f"{len(rows)}/{len(plan)} {case.task.id} {item['model']} {item['condition']} {row['termination_reason']}")
            if not row["independent_audit_passed"] or row["termination_reason"] in shared.INFRASTRUCTURE_STOPS:
                break

    if execute:
        ledger = shared.ROOT / "results" / "spend.jsonl"  # Never a separate/reset Phase 3 ledger.
        with RunLock(ledger):
            budget = Phase3Budget(ledger, collection)
            used = {e["episode_id"] for e in budget.entries if e["event"] == "reserve"}
            if used.intersection(p["episode_id"] for p in plan):
                raise BudgetError("Stage already attempted; no automatic reruns or selective replacement")
            # Check previous stage completeness before later stages; preserve raw
            # failures, and never spend more to force an incomplete block to finish.
            for previous in range(1, stage):
                path = directory.parent / f"{VERSION}-{collection}-stage{previous}-live" / "validation.json"
                if not json.loads(path.read_text())["passed"]:
                    raise ValueError("Previous stage has incomplete/infrastructure records")
            remaining_forecast = sum(p["forecast"]["nominal_usd"] for p in manifest["plan"] if p["stage"] >= stage)
            if collection == "heldout" and money(remaining_forecast) > PHASE_CAP - budget.phase_exposure():
                raise BudgetError("Full proposed evaluation exceeds remaining Phase 3 exposure; explicitly freeze a smaller balanced matrix first")
            estimate = sum(p["forecast"]["nominal_usd"] for p in plan) * 1.25
            budget.require_available(estimate)
            live = shared.LiveResponses(execute=True)
            try:
                access = {"project_id": os.environ["OPENAI_PROJECT_ID"]}
                try:
                    access["models"] = live.check_access(MODELS)
                except Exception as exc:
                    access["error"] = shared.api_error_details(exc, None, 0)
                (directory / "access.json").write_text(json.dumps(access), encoding="utf-8")
                if "error" in access:
                    raise ValueError("Model access lookup failed; no episode requests issued")
                if any(access["models"][m]["returned_id"] != m for m in MODELS):
                    raise ValueError("Model lookup returned a different model ID; no substitutions allowed")
                for previous in range(1, stage):
                    previous_dir = directory.parent / f"{VERSION}-{collection}-stage{previous}-live"
                    if json.loads((previous_dir / "access.json").read_text()) != access:
                        raise ValueError("Project/model configuration changed between stages")
                    if json.loads((previous_dir / "manifest.json").read_text())["sha256"] != manifest["sha256"]:
                        raise ValueError("Frozen plan changed between stages")
                episodes(budget, live)
            finally:
                live.close()
    else:
        episodes()
    validation = {"passed": len(rows) == len(plan) and all(infrastructure_complete(r) for r in rows),
                  "dry_run": not execute, "planned": len(plan), "recorded": len(rows),
                  "note": "Legitimate agent failures do not fail infrastructure audit. Limits remain operational failures in analysis."}
    (directory / "validation.json").write_text(json.dumps(validation, indent=2), encoding="utf-8")
    from .analysis import write_reports
    write_reports(rows, directory / "analysis", include_development=collection == "development", include_dry_run=not execute)
    return directory


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--collection", choices=["development", "heldout"], default="development")
    parser.add_argument("--stage", type=int, default=1)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--freeze-approval", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "results")
    args = parser.parse_args(argv)
    if args.prepare:
        if args.execute:
            parser.error("Preparation is offline only")
        dev, main_plan = prepare()
        print(f"Prepared {len(dev['plan'])} neutral development and {len(main_plan['plan'])} heldout episodes.")
        return
    path = args.manifest or ROOT / f"manifest_{args.collection}_{VERSION}.json"
    manifest = load_manifest(path)
    if manifest["collection"] != args.collection:
        parser.error("Collection does not match frozen manifest")
    print(run_stage(manifest, args.stage, args.output_dir, execute=args.execute, release=args.freeze_approval))


if __name__ == "__main__":
    main()
