"""Context-enriched JEV addendum. Default mode is offline preflight only.

Live requests require the explicit --live flag plus the existing JEV key,
JEV_LIVE_RUN=1, and positive budgets. Existing Experiment 5 responses are
never used to answer enriched request hashes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

import experiment_5_jev as base

ROOT = base.ROOT
OUT = ROOT / "experiment_5_jev_enriched"
MODEL_ID = base.MODEL_ID
API_URL = base.API_URL
QUESTION = (
    "Select the forecasting model expected to minimize future equal-node hierarchy-wide MASE after Bottom-Up reconciliation. "
    "Prioritize reliable historical evidence about forecasting performance, variability and hierarchical impact. "
    "Consider trade-offs with leaf-level forecasting accuracy. Do not prefer any model because it is newer or more complex."
)
CHOICES = base.OPTIONS
MODELS = ["AutoARIMA", "N-BEATS", "TimesFM-3", "SeasonalNaive"]
HARD_REQUEST_LIMIT = 2000
HARD_COST_LIMIT_USD = 2.0
_DATA: dict[str, tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]] = {}


def model_name(value: Any) -> str:
    return {"NBEATS": "N-BEATS", "TimesFM3": "TimesFM-3"}.get(str(value), str(value))


def model_file(value: Any) -> str:
    return {"N-BEATS": "NBEATS", "TimesFM-3": "TimesFM3"}.get(model_name(value), model_name(value))


def source_frames(dataset: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if dataset in _DATA:
        return _DATA[dataset]
    feature = base._load(base.DATASETS[dataset]["features"])
    if dataset == "Australian Labour":
        performance = feature[["year", "unique_id", "model", "mase"]].copy().rename(columns={"year": "origin"})
        cf = base._load("experiment_3/counterfactual_labels.csv")
        # This table has both a numeric year and an ISO origin date. Use the
        # numeric year, matching the feature and performance tables.
        cf = cf.copy()
    elif dataset == "M5 Walmart":
        performance = base._load("experiment_4/forecast_metrics.csv")[["origin", "unique_id", "model", "mase"]]
        cf = base._load("experiment_4/counterfactual_labels.csv")
    else:
        performance = base._load("experiment_5b/candidate_metrics.csv")[["origin", "unique_id", "model", "mase"]]
        cf = base._load("experiment_5b/counterfactual_labels_all_models.csv")
        cf = cf.loc[cf["scenario"].eq("all_models")]
    performance["model"] = performance["model"].map(model_name)
    cf["model"] = cf["model"].map(model_name)
    cf["origin_num"] = pd.to_numeric(cf["year"] if dataset == "Australian Labour" else cf["origin"], errors="coerce")
    performance["origin_num"] = pd.to_numeric(performance["origin"], errors="coerce")
    _DATA[dataset] = feature, performance, cf
    return _DATA[dataset]


def _historical_rows(frame: pd.DataFrame, uid: str, current_origin: Any) -> pd.DataFrame:
    origin_num = pd.to_numeric(pd.Series([current_origin]), errors="coerce").iloc[0]
    return frame.loc[frame["unique_id"].astype(str).eq(str(uid)) & frame["origin_num"].lt(origin_num)]


def candidate_evidence(performance: pd.DataFrame, cf: pd.DataFrame, uid: str, current_origin: Any) -> tuple[list[dict[str, Any]], dict[str, float]]:
    pm = _historical_rows(performance, uid, current_origin)
    cm = _historical_rows(cf, uid, current_origin)
    evidence, hierarchy_scores = [], {}
    for model in MODELS:
        hist = pm.loc[pm["model"].eq(model)].sort_values("origin_num")
        deltas = cm.loc[cm["model"].eq(model)].sort_values("origin_num")
        mase = pd.to_numeric(hist["mase"], errors="coerce").dropna().to_numpy(dtype=float)
        loss_change = pd.to_numeric(deltas["loss_change"], errors="coerce").dropna().to_numpy(dtype=float)
        last3_mase = mase[-3:].tolist()
        last3_delta = loss_change[-3:].tolist()
        slope = float(np.polyfit(np.arange(len(last3_mase)), last3_mase, 1)[0]) if len(last3_mase) >= 2 else None
        if len(loss_change):
            hierarchy_scores[model] = float(np.mean(loss_change))
        evidence.append({
            "model": model,
            "completed_window_mase_last3": last3_mase,
            "historical_mase_mean": float(np.mean(mase)) if len(mase) else None,
            "historical_mase_std": float(np.std(mase, ddof=1)) if len(mase) >= 2 else None,
            "recent_mase_slope": slope,
            "completed_hierarchy_loss_change_last3": last3_delta,
            "hierarchy_loss_change_mean": float(np.mean(loss_change)) if len(loss_change) else None,
            "hierarchy_loss_change_std": float(np.std(loss_change, ddof=1)) if len(loss_change) >= 2 else None,
            "hierarchy_evidence_windows": int(len(loss_change)),
            # Existing labels report whole-hierarchy counterfactual loss, not a
            # parent-specific decomposition. Null is deliberate where absent.
            "parent_aggregate_counterfactual_change": None,
        })
    return evidence, hierarchy_scores


def historical_best_for(dataset: str) -> pd.DataFrame:
    choices = base.selector_choices(dataset)
    return choices.loc[choices["selector"].eq("HistoricalBest"), ["origin", "unique_id", "model"]].copy()


def make_rows(dataset: str) -> list[dict[str, Any]]:
    features, performance, cf = source_frames(dataset)
    origins = base.origins_for(dataset)
    origins = origins.loc[origins["split"].isin(["validation", "test"])].copy()
    key_col = "year" if dataset == "Australian Labour" else "origin"
    origins["key"] = origins["origin"].astype(str)
    current_features = features.loc[features[key_col].astype(str).isin(set(origins["key"]))]
    if "split" in current_features:
        current_features = current_features.loc[current_features["split"].isin(["validation", "test"])]
    hbest = historical_best_for(dataset)
    hbest["origin"] = hbest["origin"].astype(str)
    rows = []
    for (origin, uid), group in current_features.groupby([key_col, "unique_id"], sort=True):
        row = group.iloc[0]
        meta = origins.loc[origins["key"].eq(str(origin))].iloc[0]
        hist_cutoff = origin
        state = base.build_state(row, base.HORIZON[dataset])
        base.add_local_hierarchy_context(dataset, row, state)
        char_fields = {k: state.get(k) for k in [
            "series_length", "trend_strength", "seasonal_strength", "volatility_std",
            "coefficient_of_variation", "lag_1_autocorrelation", "seasonal_autocorrelation",
            "forecast_horizon", "hierarchy_level", "hierarchy_contribution", "ancestor_count", "number_of_children"
        ]}
        evidence, rank_scores = candidate_evidence(performance, cf, str(uid), hist_cutoff)
        body = {
            "model": MODEL_ID,
            "state": {
                "series_characteristics": char_fields,
                "historical_candidate_evidence": evidence,
            },
            "questions": {
                "forecast_model": {
                    "type": "choice",
                    "instructions": QUESTION,
                    "criteria": CHOICES,
                }
            },
        }
        digest = base.request_hash(body)
        # Numerical control sees exactly the same candidate hierarchy evidence.
        if rank_scores:
            hist_hierarchy = min(rank_scores, key=lambda m: (rank_scores[m], m))
            control_fallback = False
        else:
            hist_hierarchy = None
            control_fallback = True
        hb_rows = hbest.loc[hbest["origin"].eq(str(origin)) & hbest["unique_id"].astype(str).eq(str(uid))]
        hb = model_name(hb_rows.iloc[0]["model"]) if not hb_rows.empty else None
        if hist_hierarchy is None:
            hist_hierarchy = hb
        rows.append({
            "dataset": dataset, "origin": str(origin), "split": meta["split"], "unique_id": str(uid),
            "request_hash": digest, "body": body, "historical_hierarchy_best": hist_hierarchy,
            "hierarchy_fallback_to_historical_best": control_fallback, "historical_best": hb,
            "evidence_models": len(evidence),
        })
    return rows


def prepare() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = [r for dataset in base.DATASETS for r in make_rows(dataset)]
    hashes = [r["request_hash"] for r in rows]
    if len(set(hashes)) != len(hashes):
        raise ValueError("Duplicate enriched request hashes detected")
    smoke = rows[0]
    old_hashes = set(pd.read_csv(ROOT / "experiment_5_jev/jev_decisions.csv")["request_hash"].astype(str))
    overlap = old_hashes.intersection(hashes)
    if overlap:
        raise ValueError("Enriched and original request hashes unexpectedly overlap")
    cache_dir = OUT / "response_cache"
    cached = {p.stem for p in cache_dir.glob("*.json")} if cache_dir.exists() else set()
    missing = [r for r in rows if r["request_hash"] not in cached]
    chars = sum(len(json.dumps(r["body"], ensure_ascii=False)) for r in missing)
    estimated_tokens = math.ceil(chars / 3)  # conservative planning estimate; bill by API usage only
    env_max = int(os.environ.get("JEV_MAX_REQUESTS", "0") or 0)
    env_usd = float(os.environ.get("JEV_MAX_USD", "0") or 0)
    hard_requests = min(env_max, HARD_REQUEST_LIMIT)
    hard_usd = min(env_usd, HARD_COST_LIMIT_USD)
    prior_requests = len(list((ROOT / "experiment_5_jev/response_cache").glob("*.json")))
    prior_cost = float(pd.read_csv(ROOT / "experiment_5_jev/jev_decisions.csv")["api_cost_usd"].sum())
    preflight = {
        "model_id": MODEL_ID,
        "official_endpoint": API_URL,
        "request_count_new": len(missing),
        "request_count_expected_total_with_original": len(missing) + prior_requests,
        "configured_request_cap": env_max,
        "hard_per_run_request_cap": hard_requests,
        "requests_fit_per_run_cap": len(missing) <= hard_requests,
        "original_responses_cached": prior_requests,
        "original_cost_usd": prior_cost,
        "estimated_new_input_tokens_conservative": estimated_tokens,
        "estimated_new_cost_usd_at_official_rate": estimated_tokens * base.PRICE_PER_MILLION_INPUT_TOKENS / 1_000_000,
        "configured_cost_cap_usd": env_usd,
        "hard_per_run_cost_cap_usd": hard_usd,
        "projected_cost_including_original_usd": prior_cost + estimated_tokens * base.PRICE_PER_MILLION_INPUT_TOKENS / 1_000_000,
        "new_cache_entries_already_present": len(cached),
        "original_request_hash_overlap": len(overlap),
        "api_key_present": bool(os.environ.get("JEV_API_KEY") or os.environ.get("TYPESAFE_API_KEY")),
        "live_run_enabled": os.environ.get("JEV_LIVE_RUN") == "1",
        "validation_test_decisions": {"|".join(map(str, k)): int(v) for k, v in pd.DataFrame(rows).groupby(["dataset", "split"]).size().items()},
        "per_run_budget_scope": "script/cache scoped; official TypeSafe docs describe rate limits, not a total account request allotment",
        "decision": "preflight_only_no_live_requests",
    }
    OUT.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{k: v for k, v in r.items() if k != "body"} for r in rows]).to_csv(OUT / "request_manifest.csv", index=False)
    with (OUT / "request_bodies.jsonl").open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps({"request_hash": row["request_hash"], "body": row["body"]}, ensure_ascii=False) + "\n")
    (OUT / "preflight.json").write_text(json.dumps(preflight, indent=2, default=str), encoding="utf-8")
    return rows, preflight


def run_live(rows: list[dict[str, Any]], info: dict[str, Any]) -> None:
    """Resumable live run; every successful response is atomically cached."""
    cache_dir = OUT / "response_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    decision_path = OUT / "enriched_decisions.csv"
    progress_path = OUT / "live_progress.json"
    prior = pd.read_csv(decision_path) if decision_path.exists() else pd.DataFrame()
    done = set(prior.get("request_hash", pd.Series(dtype=str)).astype(str))
    calls = len(list(cache_dir.glob("*.json")))
    spent = float(pd.to_numeric(prior.get("api_cost_usd", pd.Series(dtype=float)), errors="coerce").fillna(0).sum())
    request_cap = min(int(os.environ.get("JEV_MAX_REQUESTS", 0)), HARD_REQUEST_LIMIT)
    cost_cap = min(float(os.environ.get("JEV_MAX_USD", 0)), HARD_COST_LIMIT_USD)
    # Validation smoke call first, then the remaining deterministic order.
    ordered = sorted(rows, key=lambda r: (r["split"] != "validation", r["dataset"], r["origin"], r["unique_id"]))
    for item in ordered:
        digest = item["request_hash"]
        if digest in done:
            continue
        cache_path = cache_dir / f"{digest}.json"
        if cache_path.exists():
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            parsed = parse_enriched_response(payload)
            latency = None
            cache_hit = True
        else:
            if calls >= request_cap:
                raise RuntimeError(f"Stopping before request: request cap {request_cap} reached")
            est_tokens = max(1, math.ceil(len(json.dumps(item["body"], ensure_ascii=False).encode("utf-8")) / 4))
            est_cost = est_tokens * base.PRICE_PER_MILLION_INPUT_TOKENS / 1_000_000
            if spent + est_cost > cost_cap:
                raise RuntimeError(f"Stopping before request: projected spend ${spent + est_cost:.6f} exceeds ${cost_cap:.2f}")
            payload, latency = post_enriched_live(item["body"])
            # Preserve the complete real response before inspecting or parsing it.
            tmp = cache_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(cache_path)
            parsed = parse_enriched_response(payload)
            if parsed["jev_model_version"] != MODEL_ID:
                raise RuntimeError(f"API returned {parsed['jev_model_version']!r}, expected pinned {MODEL_ID!r}")
            calls += 1
            cache_hit = False
        actual_cost = (parsed["input_tokens"] or 0) * base.PRICE_PER_MILLION_INPUT_TOKENS / 1_000_000
        # A raw cached response without a decision row is a recovered previously
        # charged call; include it once in the spend ledger.
        spent += actual_cost
        record = {k: item[k] for k in ("dataset", "origin", "split", "unique_id", "request_hash", "historical_best", "historical_hierarchy_best")}
        record.update({"selected_model": parsed["selected_model"], "probabilities_json": json.dumps(parsed["probabilities"], sort_keys=True),
                       "confidence": parsed["confidence"], "margin": parsed["margin"], "jev_model_version": parsed["jev_model_version"],
                       "input_tokens": parsed["input_tokens"], "output_tokens": parsed["output_tokens"],
                       "request_latency_seconds": latency, "api_cost_usd": actual_cost, "cache_hit": cache_hit})
        pd.DataFrame([record]).to_csv(decision_path, mode="a", header=not decision_path.exists(), index=False)
        done.add(digest)
        progress = {"completed_decisions": len(done), "new_live_calls": calls, "actual_cost_usd": spent,
                    "request_cap": request_cap, "cost_cap_usd": cost_cap, "last_dataset": item["dataset"],
                    "last_origin": item["origin"], "last_split": item["split"], "last_hash": digest}
        progress_path.write_text(json.dumps(progress, indent=2), encoding="utf-8")
        if calls == 1:
            print("Validation smoke response cached and validated:", json.dumps({k: progress[k] for k in ("completed_decisions", "actual_cost_usd", "last_dataset", "last_origin", "last_split")} ), flush=True)
        if calls % 25 == 0:
            print(json.dumps(progress), flush=True)


def parse_enriched_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Use the API probabilities for direct routing, while retaining choice for audit."""
    answer = payload["answers"]["forecast_model"]
    probs = answer["probabilities"]
    if set(probs) != set(CHOICES):
        raise ValueError("JEV probabilities do not match the four allowed forecasters")
    if any(not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in probs.values()):
        raise ValueError("JEV probabilities must be finite, non-negative numbers")
    if abs(sum(probs.values()) - 1.0) > 1e-6:
        raise ValueError("JEV probabilities must sum to 1")
    api_choice = answer.get("choice")
    if api_choice not in CHOICES:
        raise ValueError("JEV returned a choice outside the allowed set")
    ordered = sorted(probs.values(), reverse=True)
    usage = payload.get("usage", {})
    return {"selected_model": max(probs, key=probs.get), "api_choice": api_choice,
            "probabilities": probs, "confidence": float(answer["confidence"]),
            "margin": float(ordered[0] - ordered[1]), "jev_model_version": payload.get("model"),
            "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens")}


def post_enriched_live(body: dict[str, Any]) -> tuple[dict[str, Any], float]:
    key = os.environ.get("JEV_API_KEY") or os.environ["TYPESAFE_API_KEY"]
    req = urllib.request.Request(API_URL, data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, method="POST")
    started = time.perf_counter()
    with urllib.request.urlopen(req, timeout=90) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload, time.perf_counter() - started


def cache_model(value: Any) -> str:
    return {"N-BEATS": "NBEATS", "TimesFM-3": "TimesFM3"}.get(str(value), str(value))


def finalize() -> None:
    """Score frozen selector choices against the shared cached candidate forecasts."""
    import experiment_5_jev as base_local
    new = pd.read_csv(OUT / "enriched_decisions.csv")
    manifest = pd.read_csv(OUT / "request_manifest.csv")
    if len(new) != len(manifest) or new.request_hash.nunique() != len(new):
        raise RuntimeError("Decision/request manifest coverage mismatch or duplicate hashes")
    if set(new.request_hash) != set(manifest.request_hash):
        raise RuntimeError("Cached decision hashes do not match the planned request set")
    if not new["jev_model_version"].eq(MODEL_ID).all():
        raise RuntimeError("At least one response did not match the pinned JEV model")
    assignments_all, node_all, origin_all, level_all, coherence_all = [], [], [], [], []
    original = pd.read_csv(ROOT / "experiment_5_jev/jev_decisions.csv")
    for dataset in base_local.DATASETS:
        baseline = base_local.selector_choices(dataset)
        baseline = baseline.loc[baseline.selector.isin(["GlobalBest", "HistoricalBest", "Ridge", "LightGBM regret", "HierarchyLoss"])].copy()
        baseline["model"] = baseline.model.map(cache_model)
        enriched = new.loc[new.dataset.eq(dataset)].copy()
        enriched["model"] = enriched.selected_model.map(cache_model)
        enriched["selector"] = "Enriched JEV"
        hh = enriched[["origin", "unique_id", "historical_hierarchy_best"]].rename(columns={"historical_hierarchy_best": "model"}).copy()
        hh["model"] = hh.model.map(cache_model); hh["selector"] = "HistoricalHierarchyBest"
        direct = enriched[["origin", "unique_id", "model"]].copy()
        old = original.loc[original.dataset.eq(dataset), ["origin", "unique_id", "selected_model"]].rename(columns={"selected_model": "model"}).copy()
        old["selector"] = "Original JEV"; old["model"] = old.model.map(cache_model)
        direct["selector"] = "Enriched JEV"
        # Select a confidence fallback threshold on validation only.
        val = enriched.loc[enriched.split.eq("validation")].copy()
        grid = [0.0, 0.25, 0.5, 0.75, 0.9, 1.01]
        threshold_scores = []
        for threshold in grid:
            temp = val[["origin", "unique_id", "selected_model", "historical_best", "confidence"]].copy()
            temp["model"] = pd.Series(np.where(temp.confidence.ge(threshold), temp.selected_model, temp.historical_best), index=temp.index).astype(str).map(cache_model)
            temp["selector"] = "validation_fallback"
            h = temp[["origin", "unique_id", "historical_best"]].rename(columns={"historical_best": "model"}).copy()
            h["selector"] = "validation_hbest"; h["model"] = h.model.map(cache_model)
            sc, ov, _, _ = base_local.score_assignments(dataset, pd.concat([temp[["origin", "unique_id", "selector", "model"]], h], ignore_index=True), *base_local.read_hierarchy(dataset))
            score = np.mean([r["mase"] for r in ov if r["selector"] == "validation_fallback"])
            threshold_scores.append({"dataset": dataset, "threshold": threshold, "validation_equal_node_mase": score})
        chosen_threshold = min(threshold_scores, key=lambda r: (r["validation_equal_node_mase"], r["threshold"]))["threshold"]
        enriched["fallback_model"] = np.where(enriched.confidence.ge(chosen_threshold), enriched.selected_model, enriched.historical_best)
        fallback = enriched[["origin", "unique_id", "fallback_model"]].rename(columns={"fallback_model": "model"}).copy()
        fallback["selector"] = "Enriched JEV confidence fallback"
        parts = [baseline, old, direct, hh[["origin", "unique_id", "selector", "model"]], fallback]
        assignments = pd.concat(parts, ignore_index=True)
        assignments["origin"] = assignments.origin.astype(str)
        assignments["model"] = assignments.model.map(cache_model)
        assignments["dataset"] = dataset
        assignments_all.append(assignments)
        summing, hierarchy, actual = base_local.read_hierarchy(dataset)
        nodes, origins, levels, coher = base_local.score_assignments(dataset, assignments, summing, hierarchy, actual)
        node_all.extend(nodes); origin_all.extend(origins); level_all.extend(levels); coherence_all.extend(coher)
        threshold_scores[-1]["chosen"] = False
        for row in threshold_scores:
            row["chosen"] = row["threshold"] == chosen_threshold
        threshold_scores_out = threshold_scores
        OUT.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(threshold_scores_out).to_csv(OUT / f"{dataset.replace(' ', '_').lower()}_fallback_validation.csv", index=False)
        print(f"{dataset}: validation-calibrated fallback threshold={chosen_threshold}", flush=True)
    assignments_df = pd.concat(assignments_all, ignore_index=True)
    nodes_df, origins_df, levels_df, coherence_df = map(pd.DataFrame, (node_all, origin_all, level_all, coherence_all))
    nodes_df.to_csv(OUT / "series_metrics.csv", index=False)
    origins_df.to_csv(OUT / "metrics_by_origin.csv", index=False)
    levels_df.to_csv(OUT / "metrics_by_level.csv", index=False)
    coherence_df.to_csv(OUT / "bottomup_coherence.csv", index=False)
    assignments_df.to_csv(OUT / "selector_assignments.csv", index=False)
    test_origins = origins_df.loc[origins_df.split.eq("test")]
    summary = test_origins.groupby(["dataset", "selector"], as_index=False).agg(
        mean_origin_mase=("mase", "mean"), mean_leaf_mase=("leaf_mase", "mean"),
        mean_aggregate_mase=("aggregate_mase", "mean"), mean_mae=("mae", "mean"),
        mean_rmse=("rmse", "mean"), test_origins=("origin", "nunique"))
    summary.to_csv(OUT / "summary_metrics.csv", index=False)
    paired = []
    for dataset, g in test_origins.groupby("dataset"):
        pivot = g.pivot(index="origin", columns="selector", values="mase")
        if "HistoricalBest" not in pivot:
            continue
        rng = np.random.default_rng(20261105)
        for selector in pivot.columns:
            if selector == "HistoricalBest": continue
            delta = (pivot[selector] - pivot["HistoricalBest"]).dropna().to_numpy()
            if not len(delta): continue
            boot = np.array([rng.choice(delta, size=len(delta), replace=True).mean() for _ in range(10000)])
            paired.append({"dataset": dataset, "selector": selector, "paired_delta_mase_vs_historical_best": float(delta.mean()),
                           "origins": len(delta), "bootstrap_2_5pct": float(np.quantile(boot,.025)),
                           "bootstrap_97_5pct": float(np.quantile(boot,.975)),
                           "interpretation": "paired origin bootstrap; exploratory, few correlated origins"})
    pd.DataFrame(paired).to_csv(OUT / "paired_origin_bootstrap.csv", index=False)
    d = new.copy()
    d["selected_model"] = d["selected_model"].map(model_name)
    switches = d.sort_values(["dataset", "unique_id", "origin"]).groupby(["dataset", "split", "unique_id"]).selected_model.apply(lambda x: int((x != x.shift()).iloc[1:].sum())).reset_index(name="switches")
    switches.to_csv(OUT / "selection_stability.csv", index=False)
    costs = new.groupby(["dataset", "split"], as_index=False).agg(decisions=("request_hash", "size"),
        mean_confidence=("confidence", "mean"), mean_margin=("margin", "mean"),
        mean_latency_seconds=("request_latency_seconds", "mean"), total_input_tokens=("input_tokens", "sum"),
        total_output_tokens=("output_tokens", "sum"), api_cost_usd=("api_cost_usd", "sum"),
        model_version=("jev_model_version", "first"))
    costs.to_csv(OUT / "jev_costs.csv", index=False)
    # Realized leaf regret uses candidate forecast errors only for retrospective
    # evaluation; these outcomes never enter the frozen request or routing rule.
    regret_frames = []
    for dataset in base_local.DATASETS:
        summing, hierarchy, actual = base_local.read_hierarchy(dataset)
        leaves = list(summing.columns.astype(str))
        test_origins = base_local.origins_for(dataset)
        test_origins = test_origins.loc[test_origins.split.eq("test"), "origin"].astype(str).tolist()
        candidate_assignments = pd.DataFrame([
            {"dataset": dataset, "origin": origin, "unique_id": leaf,
             "selector": f"candidate:{model}", "model": cache_model(model)}
            for origin in test_origins for model in MODELS for leaf in leaves
        ])
        candidate_nodes, _, _, _ = base_local.score_assignments(dataset, candidate_assignments, summing, hierarchy, actual)
        cand = pd.DataFrame(candidate_nodes)
        cand = cand.loc[cand.is_leaf].rename(columns={"selector": "candidate_selector"})
        cand["model"] = cand["candidate_selector"].str.replace("^candidate:", "", regex=True)
        regret_frames.append(cand[["dataset", "origin", "unique_id", "model", "mase"]])
    candidate = pd.concat(regret_frames, ignore_index=True)
    direct_leaf = nodes_df.loc[(nodes_df.selector.eq("Enriched JEV")) & nodes_df.is_leaf,
                               ["dataset", "origin", "unique_id", "mase"]].rename(columns={"mase": "selected_leaf_mase"})
    direct_leaf["origin"] = direct_leaf["origin"].astype(str)
    new["origin"] = new["origin"].astype(str)
    direct_leaf = direct_leaf.merge(new[["dataset", "origin", "unique_id", "selected_model", "confidence", "margin"]],
                                    on=["dataset", "origin", "unique_id"], how="left")
    candidate_oracle = candidate.groupby(["dataset", "origin", "unique_id"], as_index=False).mase.min().rename(columns={"mase": "oracle_leaf_mase"})
    direct_leaf = direct_leaf.merge(candidate_oracle, on=["dataset", "origin", "unique_id"], how="left")
    direct_leaf["realized_leaf_regret"] = direct_leaf.selected_leaf_mase - direct_leaf.oracle_leaf_mase
    direct_leaf["split"] = direct_leaf.apply(lambda r: new.loc[(new.dataset.eq(r.dataset)) & (new.origin.astype(str).eq(str(r.origin))), "split"].iloc[0], axis=1)
    direct_leaf.to_csv(OUT / "confidence_regret_audit.csv", index=False)
    confidence_rows = []
    for dataset, g in direct_leaf.loc[direct_leaf.split.eq("test")].groupby("dataset"):
        corr = g[["confidence", "realized_leaf_regret"]].corr(method="spearman").iloc[0, 1]
        for bin_id, part in g.groupby(pd.qcut(g.confidence, q=min(4, g.confidence.nunique()), duplicates="drop"), observed=False):
            confidence_rows.append({"dataset": dataset, "confidence_bin": str(bin_id), "decisions": len(part),
                                    "mean_confidence": float(part.confidence.mean()),
                                    "mean_realized_leaf_regret": float(part.realized_leaf_regret.mean()),
                                    "spearman_confidence_regret": float(corr)})
    pd.DataFrame(confidence_rows).to_csv(OUT / "confidence_regret_bins.csv", index=False)
    selection_frequency = d.groupby(["dataset", "split", "selected_model"], as_index=False).size().rename(columns={"size": "decisions"})
    selection_frequency.to_csv(OUT / "selection_frequency.csv", index=False)
    response_choice = []
    for row in d.itertuples():
        response = json.loads((OUT / "response_cache" / f"{row.request_hash}.json").read_text(encoding="utf-8"))
        response_choice.append(response["answers"]["forecast_model"].get("choice"))
    d["api_choice"] = response_choice
    pd.DataFrame([{"decisions": len(d), "probability_max_choice_mismatches": int((d.api_choice != d.selected_model).sum()),
                   "mean_confidence": float(d.confidence.mean()), "mean_margin": float(d.margin.mean()),
                   "new_response_input_tokens": int(d.input_tokens.sum()), "new_response_output_tokens": int(d.output_tokens.sum()),
                   "recorded_api_cost_usd": float(d.api_cost_usd.sum()), "latency_observed": int(d.request_latency_seconds.notna().sum()),
                   "latency_p50_seconds": float(d.request_latency_seconds.median()), "latency_p95_seconds": float(d.request_latency_seconds.quantile(.95)),
                   "responses_with_version_jev_1_14_0": int(d.jev_model_version.eq(MODEL_ID).sum())}]).to_csv(OUT / "api_audit.csv", index=False)
    print(summary.to_string(index=False), flush=True)
    print(f"Coherence checks passed: {int(coherence_df.coherent.sum())}/{len(coherence_df)}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="send new enriched requests after preflight")
    parser.add_argument("--score-only", action="store_true", help="score completed cached responses without API calls")
    args = parser.parse_args()
    if args.score_only:
        finalize()
        return
    rows, info = prepare()
    print(json.dumps(info, indent=2, default=str))
    if not args.live:
        print("Offline preflight only; no JEV calls sent.")
        return
    if not info["api_key_present"] or not info["live_run_enabled"]:
        raise RuntimeError("Live run requires a configured API key and JEV_LIVE_RUN=1")
    if not info["requests_fit_per_run_cap"] or info["estimated_new_cost_usd_at_official_rate"] > info["hard_per_run_cost_cap_usd"]:
        raise RuntimeError("Enriched request plan exceeds configured per-run budget")
    run_live(rows, info)
    if len(pd.read_csv(OUT / "enriched_decisions.csv")) == len(rows):
        finalize()


if __name__ == "__main__":
    main()
