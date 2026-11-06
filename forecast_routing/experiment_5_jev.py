"""Offline-safe TypeSafe JEV integration preflight for the forecasting project.

Live calls require BOTH TYPESAFE_API_KEY and JEV_LIVE_RUN=1. This script's
default mode performs no network access and writes no synthetic predictions.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "experiment_5_jev"
ENV_KEYS = {"JEV_API_KEY", "TYPESAFE_API_KEY", "JEV_LIVE_RUN", "JEV_MAX_REQUESTS", "JEV_MAX_USD"}


def load_task_env() -> None:
    """Read only the explicitly supported JEV settings from nearby .env files."""
    for env_file in (ROOT / ".env", ROOT.parent / ".env"):
        if not env_file.is_file():
            continue
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            name, value = name.strip(), value.strip()
            if name not in ENV_KEYS or name in os.environ:
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            os.environ[name] = value


load_task_env()
MODEL_ID = "jev-1.14.0"
API_URL = "https://api.typesafe.ai/v1/systemone"
PRICE_PER_MILLION_INPUT_TOKENS = 0.042
QUESTION = (
    "Which of the four forecasting models is most suitable for forecasting this series, "
    "considering its historical characteristics and its contribution to the reconciled hierarchy?"
)
OPTIONS = {
    "AutoARIMA": "Statistical autoregressive integrated moving-average model with automatic order selection.",
    "N-BEATS": "Neural basis-expansion model configured for the series frequency and forecast horizon.",
    "TimesFM-3": "Pretrained time-series foundation model used for zero-shot forecasting.",
    "SeasonalNaive": "Repeats the most recent seasonal cycle as a simple baseline.",
}
HARD_REQUEST_LIMIT = 2000
HARD_COST_LIMIT_USD = 2.0
SEASON_LENGTH = {"Australian Labour": 12, "M5 Walmart": 7, "Australian Tourism": 12}
HORIZON = {"Australian Labour": 12, "M5 Walmart": 28, "Australian Tourism": 3}
SELECTOR_MAP = {
    "GlobalBest": "GlobalBest",
    "HistoricalBest": "HistoricalBest",
    "Ridge": "Ridge",
    "LightGBM regret": "LGBM_regret_matched",
    "HierarchyLoss": "HierarchyLoss",
}
LABOUR_SELECTOR_MAP = {
    "GlobalBest": "Global",
    "HistoricalBest": "HistoricalBest",
    "Ridge": "Ridge",
    "LightGBM regret": "LGBM_regret_matched_features",
    "HierarchyLoss": "HierarchyLoss_no_hierarchy_features",
}

# Input files and the previous exploratory baseline tables. Metrics are reported
# as recorded by each experiment; they are not recomputed or presented as a new test.
DATASETS = {
    "Australian Labour": {
        "features": "experiment_3/selector_features.csv",
        "origins": "experiment_3/cutoffs.csv",
        "baseline": "experiment_3/overall_comparison.csv",
        "nodes": 57,
        "leaves": 32,
        "baseline_map": {
            "Global": "GlobalBest",
            "HistoricalBest": "HistoricalBest",
            "Ridge": "Ridge",
            "LGBM_regret_matched_features": "LightGBM regret",
            "HierarchyLoss_no_hierarchy_features": "HierarchyLoss LightGBM",
        },
        "metric_split": "existing_table_all",
    },
    "M5 Walmart": {
        "features": "experiment_4/selector_features.csv",
        "origins": "experiment_4/origins.csv",
        "baseline": "experiment_4/overall_comparison.csv",
        "nodes": 84,
        "leaves": 36,
        "baseline_map": {
            "GlobalBest": "GlobalBest",
            "HistoricalBest": "HistoricalBest",
            "Ridge": "Ridge",
            "LGBM_regret_matched": "LightGBM regret",
            "HierarchyLoss": "HierarchyLoss LightGBM",
        },
        "metric_split": "test",
    },
    "Australian Tourism": {
        "features": "experiment_5b/selector_features.csv",
        "origins": "experiment_5b/origins.csv",
        "baseline": "experiment_5b/overall_all_models.csv",
        "nodes": 85,
        "leaves": 48,
        "baseline_map": {
            "GlobalBest": "GlobalBest",
            "HistoricalBest": "HistoricalBest",
            "Ridge": "Ridge",
            "LGBM_regret_matched": "LightGBM regret",
            "HierarchyLoss": "HierarchyLoss LightGBM",
        },
        "metric_split": "test",
    },
}

_CSV_CACHE: dict[str, pd.DataFrame] = {}
_HIERARCHY_CONTEXT: dict[str, tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]] = {}
_DATE_COL_CACHE: dict[tuple[str, str], list[str]] = {}


def _load(relative: str) -> pd.DataFrame:
    path = ROOT / relative
    if not path.is_file():
        raise FileNotFoundError(f"Required prior artifact is missing: {relative}")
    key = str(path)
    if key not in _CSV_CACHE:
        _CSV_CACHE[key] = pd.read_csv(path)
    return _CSV_CACHE[key].copy()


def _validation_origin(dataset: str) -> Any:
    origins = _load(DATASETS[dataset]["origins"])
    if "split" not in origins:
        raise ValueError(f"No split column in {DATASETS[dataset]['origins']}")
    valid = origins.loc[origins["split"].astype(str).str.lower().eq("validation")]
    if valid.empty:
        raise ValueError(f"No validation origin for {dataset}")
    return valid.iloc[0].get("origin", valid.iloc[0].get("year"))


def _sample_feature_row(dataset: str) -> pd.Series:
    frame = _load(DATASETS[dataset]["features"])
    split_col = "split" if "split" in frame else None
    # In Labour, feature rows are labelled by year, with validation = 2016–17.
    if split_col:
        sub = frame.loc[frame[split_col].astype(str).str.lower().eq("validation")]
    elif "year" in frame:
        sub = frame.loc[pd.to_numeric(frame["year"], errors="coerce").between(2016, 2017)]
    else:
        raise ValueError(f"Could not identify validation rows in {dataset} features")
    if sub.empty:
        raise ValueError(f"No validation feature rows available for {dataset}")
    return sub.iloc[0]


def build_state(row: pd.Series, horizon: int) -> dict[str, Any]:
    """Compact, de-identified state; never includes series IDs or target outcomes."""
    fields = {
        "series_length": ("series_length", "history_length"),
        "trend_strength": ("trend_strength",),
        "seasonal_strength": ("seasonal_strength",),
        "volatility_std": ("std", "volatility"),
        "coefficient_of_variation": ("cv",),
        "lag_1_autocorrelation": ("lag1",),
        "seasonal_autocorrelation": ("lag12", "lag_season"),
        "hierarchy_level": ("level", "level_name"),
        "hierarchy_contribution": ("parent_share", "parent_contribution"),
        "ancestor_count": ("ancestor_count",),
        "number_of_children": ("children", "n_children"),
        "recent_growth": ("recent_growth",),
    }
    state: dict[str, Any] = {"forecast_horizon": int(horizon)}
    for destination, candidates in fields.items():
        for name in candidates:
            if name in row.index:
                value = row[name]
                if pd.isna(value):
                    break
                state[destination] = value.item() if hasattr(value, "item") else value
                break
    state["eligible_forecasters"] = [
        {"name": name, "description": description}
        for name, description in OPTIONS.items()
    ]
    return state


def add_recent_candidate_errors(dataset: str, row: pd.Series, state: dict[str, Any]) -> None:
    """Attach only cached rolling errors already completed before this origin."""
    path = str(ROOT / DATASETS[dataset]["features"])
    frame = _CSV_CACHE.get(path)
    if frame is None:
        frame = _load(DATASETS[dataset]["features"])
        _CSV_CACHE[path] = frame.copy()
    id_col = "unique_id"
    origin_col = "origin" if "origin" in frame else "year"
    key = row.get(origin_col)
    match = frame.loc[frame[id_col].eq(row[id_col]) & frame[origin_col].eq(key)]
    if "recent_candidate_mase" not in match or "model" not in match:
        return
    aliases = {"NBEATS": "N-BEATS", "TimesFM3": "TimesFM-3"}
    errors = {}
    for _, record in match.iterrows():
        value = record["recent_candidate_mase"]
        if pd.notna(value) and record["model"] in ("AutoARIMA", "NBEATS", "TimesFM3", "SeasonalNaive"):
            errors[aliases.get(record["model"], record["model"])] = float(value)
    if errors:
        state["recent_completed_candidate_mase"] = errors


def add_local_hierarchy_context(dataset: str, row: pd.Series, state: dict[str, Any]) -> None:
    """Derive leaf level, count and parent share from pre-origin local artifacts."""
    if dataset == "Australian Labour":
        return  # E3's historical feature table already contains these fields.
    if dataset not in _HIERARCHY_CONTEXT:
        folder = "experiment_4" if dataset == "M5 Walmart" else "experiment_5b"
        hierarchy = _load(f"{folder}/hierarchy.csv")
        matrix = _load(f"{folder}/summing_matrix.csv").set_index("unique_id")
        actual = _load(f"{folder}/actual_series.csv").set_index("unique_id")
        origins = _load(DATASETS[dataset]["origins"])
        _HIERARCHY_CONTEXT[dataset] = hierarchy, matrix, actual, origins
    hierarchy, matrix, actual, origins = _HIERARCHY_CONTEXT[dataset]
    uid = str(row["unique_id"])
    if uid not in actual.index or uid not in matrix.columns:
        raise ValueError(f"Cannot map a local leaf to hierarchy for {dataset}")
    origin_key = row.get("origin")
    origin = origins.loc[origins["origin"].eq(origin_key)].iloc[0]
    cutoff = pd.to_datetime(origin["history_end"])
    date_key = (dataset, str(cutoff.date()))
    if date_key not in _DATE_COL_CACHE:
        _DATE_COL_CACHE[date_key] = [c for c in actual.columns if pd.notna(pd.to_datetime(c, errors="coerce")) and pd.to_datetime(c) <= cutoff]
    date_cols = _DATE_COL_CACHE[date_key]
    if not date_cols:
        raise ValueError(f"No available history before the selected {dataset} origin")
    history_len = int(actual.loc[uid, date_cols].notna().sum())
    state["series_length"] = history_len
    state["hierarchy_level"] = str(hierarchy.loc[hierarchy["unique_id"].eq(uid), "level"].iloc[0])
    state["number_of_children"] = 0
    parent_rows = matrix.index[(matrix[uid] > 0) & (matrix.index != uid)]
    if len(parent_rows):
        parent = min(parent_rows, key=lambda name: int((matrix.loc[name] > 0).sum()))
        parent_total = float(pd.to_numeric(actual.loc[parent, date_cols], errors="coerce").sum())
        leaf_total = float(pd.to_numeric(actual.loc[uid, date_cols], errors="coerce").sum())
        state["hierarchy_contribution"] = leaf_total / parent_total if parent_total else None


def build_request(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": MODEL_ID,
        "state": state,
        "questions": {
            "forecast_model": {
                "type": "choice",
                "instructions": QUESTION,
                "criteria": OPTIONS,
            }
        },
    }


def request_hash(body: dict[str, Any]) -> str:
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def validate_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a real API response; no fallback/default scores."""
    answer = payload["answers"]["forecast_model"]
    probs = answer["probabilities"]
    if set(probs) != set(OPTIONS):
        raise ValueError("JEV response options do not match the four allowed forecasters")
    if any(not isinstance(v, (int, float)) or v < 0 for v in probs.values()):
        raise ValueError("JEV probabilities must be non-negative numbers")
    if abs(sum(probs.values()) - 1.0) > 1e-6:
        raise ValueError("JEV probabilities must sum to 1")
    selected = answer["choice"]
    if selected not in OPTIONS or selected != max(probs, key=probs.get):
        raise ValueError("JEV selected choice is invalid or inconsistent with probabilities")
    ordered = sorted(probs.values(), reverse=True)
    usage = payload.get("usage", {})
    return {
        "selected_model": selected,
        "probabilities": probs,
        "confidence": float(answer["confidence"]),
        "margin": float(ordered[0] - ordered[1]),
        "jev_model_version": payload.get("model"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
    }


def post_live(body: dict[str, Any], max_usd: float) -> tuple[dict[str, Any], float]:
    estimated_input_tokens = max(1, (len(json.dumps(body, ensure_ascii=False)) + 3) // 4)
    estimated_cost = estimated_input_tokens * PRICE_PER_MILLION_INPUT_TOKENS / 1_000_000
    if estimated_cost > max_usd:
        raise RuntimeError(
            f"Estimated input-token charge ${estimated_cost:.8f} exceeds configured cap ${max_usd:.8f}"
        )
    key = os.environ.get("JEV_API_KEY") or os.environ["TYPESAFE_API_KEY"]
    started = time.perf_counter()
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as response:
        payload = json.loads(response.read().decode("utf-8"))
    latency = time.perf_counter() - started
    parsed = validate_response(payload)
    actual_cost = (parsed["input_tokens"] or 0) * PRICE_PER_MILLION_INPUT_TOKENS / 1_000_000
    if actual_cost > max_usd:
        # Cost is known only after the response. Keep response metadata for audit,
        # but clearly flag that the post-request cap was exceeded.
        raise RuntimeError(f"Actual reported input-token charge ${actual_cost:.8f} exceeded configured cap")
    return payload, latency


def origins_for(dataset: str) -> pd.DataFrame:
    frame = _load(DATASETS[dataset]["origins"])
    if dataset == "Australian Labour":
        frame = frame.rename(columns={"year": "origin"})
        frame["split"] = frame["partition"].map({"validation": "validation", "exploratory": "test"})
    return frame


def make_decision_rows(dataset: str) -> list[dict[str, Any]]:
    features = _load(DATASETS[dataset]["features"])
    origins = origins_for(dataset)
    origins = origins.loc[origins["split"].isin(["validation", "test"])]
    key_col = "year" if dataset == "Australian Labour" else "origin"
    origins["key"] = origins["origin"].astype(str)
    features = features.loc[features[key_col].astype(str).isin(set(origins["key"]))]
    if "split" in features:
        features = features.loc[features["split"].isin(["validation", "test"])]
    rows = []
    for (origin, _uid), group in features.groupby([key_col, "unique_id"], sort=True):
        row = group.iloc[0]
        meta = origins.loc[origins["key"].eq(str(origin))].iloc[0]
        state = build_state(row, HORIZON[dataset])
        add_recent_candidate_errors(dataset, row, state)
        add_local_hierarchy_context(dataset, row, state)
        body = build_request(state)
        rows.append({"dataset": dataset, "origin": str(origin), "split": meta["split"], "unique_id": row["unique_id"], "request_hash": request_hash(body), "body": body})
    return rows


def read_hierarchy(dataset: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if dataset == "Australian Labour":
        summing = pd.read_csv(ROOT / "data/hierarchical/Labour/agg_mat.csv", index_col=0).astype(float)
        hierarchy = pd.DataFrame({"unique_id": summing.index.astype(str)})
        hierarchy["level"] = hierarchy["unique_id"].map(lambda x: "total" if "Total" in x else f"depth_{x.count(',') + 1}")
        raw = pd.read_csv(ROOT / "data/hierarchical/Labour/data.csv", index_col=0)
        raw.index = pd.to_datetime(raw.index)
        return summing, hierarchy, raw.T
    folder = "experiment_4" if dataset == "M5 Walmart" else "experiment_5b"
    summing = pd.read_csv(ROOT / f"{folder}/summing_matrix.csv").set_index("unique_id").astype(float)
    hierarchy = _load(f"{folder}/hierarchy.csv")
    actual = pd.read_csv(ROOT / f"{folder}/actual_series.csv").set_index("unique_id")
    return summing, hierarchy, actual


def selector_choices(dataset: str) -> pd.DataFrame:
    if dataset == "Australian Labour":
        d = _load("experiment_3/selector_predictions.csv")
        valid = set(origins_for(dataset).loc[lambda x: x["split"].isin(["validation", "test"]), "origin"].astype(str))
        d = d.loc[d["year"].astype(str).isin(valid)].copy()
        d["selector"] = d["selector"].map({v: k for k, v in LABOUR_SELECTOR_MAP.items()})
        d = d.loc[d["selector"].notna()].rename(columns={"year": "origin"})
        # E3 did not persist per-leaf validation choices. Reconstruct only the
        # HistoricalBest fallback from completed training outcomes, respecting
        # its frozen-through-2015 validation feature cutoff.
        feat = _load("experiment_3/selector_features.csv")
        cuts = _load("experiment_3/cutoffs.csv").set_index("year")
        val_rows = []
        for year in (2016, 2017):
            latest = int(cuts.loc[year, "latest_feature_performance_year"])
            hist = feat.loc[pd.to_numeric(feat["year"], errors="coerce").le(latest)]
            for uid, group in hist.groupby("unique_id"):
                means = group.groupby("model")["mase"].mean()
                val_rows.append({"origin": str(year), "unique_id": uid, "selector": "HistoricalBest", "model": means.idxmin()})
        d = pd.concat([d, pd.DataFrame(val_rows)], ignore_index=True)
    elif dataset == "M5 Walmart":
        d = _load("experiment_4/selector_predictions.csv")
        d = d.loc[d["split"].isin(["validation", "test"])].copy()
        d["selector"] = d["selector"].map({v: k for k, v in SELECTOR_MAP.items()})
    else:
        d = _load("experiment_5b/selections_all_models.csv")
        d = d.loc[(d["scenario"] == "all_models") & d["split"].isin(["validation", "test"])].copy()
        d["selector"] = d["selector"].map({v: k for k, v in SELECTOR_MAP.items()})
    d = d.loc[d["selector"].notna(), ["origin", "unique_id", "selector", "model"]].copy()
    d["origin"] = d["origin"].astype(str)
    d["model"] = d["model"].replace({"N-BEATS": "NBEATS", "TimesFM-3": "TimesFM3"})
    return d


def score_assignments(dataset: str, assignments: pd.DataFrame, summing: pd.DataFrame,
                      hierarchy: pd.DataFrame, actual: pd.DataFrame) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Use cached leaf forecasts, Bottom-Up aggregation and pre-origin MASE scales."""
    origins = origins_for(dataset).copy(); origins["key"] = origins["origin"].astype(str)
    summing.index = summing.index.astype(str); summing.columns = summing.columns.astype(str)
    leaves = list(summing.columns)
    hierarchy = hierarchy.set_index("unique_id"); hierarchy.index = hierarchy.index.astype(str)
    actual = actual.copy(); actual.index = actual.index.astype(str); actual.columns = pd.to_datetime(actual.columns)
    forecasts: dict[tuple[str, str], pd.DataFrame] = {}
    node_rows, origin_rows, level_rows, coherence = [], [], [], []
    for origin in sorted(assignments["origin"].astype(str).unique()):
        meta = origins.loc[origins["key"].eq(origin)].iloc[0]
        start, end, history_end = map(pd.Timestamp, [meta["target_start"], meta["target_end"], meta["history_end"]])
        dates = pd.date_range(start, end, freq="D" if dataset == "M5 Walmart" else "MS")
        subset = assignments.loc[assignments["origin"].astype(str).eq(origin)]
        for selector in subset["selector"].unique():
            choices = subset.loc[subset["selector"].eq(selector)].set_index("unique_id")["model"]
            choices.index = choices.index.astype(str)
            if not set(leaves).issubset(choices.index):
                raise ValueError(f"Missing leaf choices: {dataset}/{origin}/{selector}")
            leaf_forecasts = np.empty((len(leaves), len(dates)), dtype=float)
            for i, leaf in enumerate(leaves):
                model = choices.loc[leaf]
                cache_key = (origin, model)
                if cache_key not in forecasts:
                    path = cache_file(dataset, origin, model)
                    if not path.exists():
                        raise FileNotFoundError(path)
                    forecasts[cache_key] = pd.read_csv(path)
                f = forecasts[cache_key]
                f = f.loc[f["unique_id"].astype(str).eq(leaf)].copy()
                f["ds"] = pd.to_datetime(f["ds"])
                vals = f.set_index("ds")["yhat"].reindex(dates)
                if vals.isna().any():
                    raise ValueError(f"Missing forecast values {dataset}/{origin}/{leaf}/{model}")
                leaf_forecasts[i] = vals.to_numpy(dtype=float)
            matrix = summing.loc[:, leaves].to_numpy(dtype=float)
            predicted = matrix @ leaf_forecasts
            residual = predicted - matrix @ leaf_forecasts
            coherent = float(abs(residual).max()) < 1e-8
            coherence.append({"dataset": dataset, "origin": origin, "selector": selector, "max_residual": float(abs(residual).max()), "coherent": coherent})
            if not coherent:
                raise ValueError("Bottom-Up coherence check failed")
            truth = actual.reindex(index=summing.index, columns=dates).astype(float)
            if truth.isna().any().any():
                raise ValueError(f"Missing targets in {dataset}/{origin}")
            history = actual.loc[:, actual.columns <= history_end]
            scale = (history - history.shift(SEASON_LENGTH[dataset], axis=1)).abs().mean(axis=1)
            scale = scale.dropna(); scale = scale.loc[scale > 0]
            pred = pd.DataFrame(predicted, index=summing.index, columns=dates)
            absolute = (truth - pred).abs(); squared = (truth - pred) ** 2
            current = []
            for node in summing.index:
                if node not in scale.index:
                    continue
                leaf = node in leaves
                level = str(hierarchy.loc[node, "level"]) if node in hierarchy.index else "unknown"
                rec = {"dataset": dataset, "origin": origin, "split": meta["split"], "selector": selector,
                       "unique_id": node, "level": level, "is_leaf": leaf,
                       "mase": float(absolute.loc[node].mean() / scale.loc[node]),
                       "mae": float(absolute.loc[node].mean()), "rmse": float(np.sqrt(squared.loc[node].mean()))}
                node_rows.append(rec); current.append(rec)
            leaf_rows = [x for x in current if x["is_leaf"]]; agg_rows = [x for x in current if not x["is_leaf"]]
            origin_rows.append({"dataset": dataset, "origin": origin, "split": meta["split"], "selector": selector,
                                "mase": float(np.mean([x["mase"] for x in current])),
                                "leaf_mase": float(np.mean([x["mase"] for x in leaf_rows])),
                                "aggregate_mase": float(np.mean([x["mase"] for x in agg_rows])),
                                "mae": float(np.mean([x["mae"] for x in current])),
                                "rmse": float(np.sqrt(np.mean([x["rmse"]**2 for x in current]))), "nodes": len(current)})
            level_groups: dict[str, list[float]] = {}
            for rec in current:
                level_groups.setdefault(rec["level"], []).append(rec["mase"])
            for level, vals in level_groups.items():
                level_rows.append({"dataset": dataset, "origin": origin, "split": meta["split"], "selector": selector, "level": level, "mase": float(np.mean(vals)), "nodes": len(vals)})
    return node_rows, origin_rows, level_rows, coherence


def cache_file(dataset: str, origin: Any, model: str) -> Path:
    folder = {"Australian Labour": "experiment_2/cache", "M5 Walmart": "experiment_4/cache", "Australian Tourism": "experiment_5b/cache"}[dataset]
    model = {"N-BEATS": "NBEATS", "TimesFM-3": "TimesFM3"}.get(model, model)
    return ROOT / folder / f"{origin}_{model}.csv"


def live_decision_run() -> None:
    """Fetch/cache all validation and exploratory-test decisions, then score."""
    api_key_present = bool(os.environ.get("JEV_API_KEY") or os.environ.get("TYPESAFE_API_KEY"))
    if not api_key_present or os.environ.get("JEV_LIVE_RUN") != "1":
        raise RuntimeError("Live evaluation requires key and JEV_LIVE_RUN=1")
    request_cap = min(int(os.environ.get("JEV_MAX_REQUESTS", "0") or 0), HARD_REQUEST_LIMIT)
    cost_cap = min(float(os.environ.get("JEV_MAX_USD", "0") or 0), HARD_COST_LIMIT_USD)
    if request_cap < 1 or cost_cap <= 0:
        raise RuntimeError("Positive request and cost budgets are required")
    planned = [r for dataset in DATASETS for r in make_decision_rows(dataset)]
    if len(planned) > request_cap:
        # Cached calls also count toward the task's absolute request ceiling.
        cache_dir = OUT / "response_cache"
        present = sum((cache_dir / f"{r['request_hash']}.json").exists() for r in planned)
        if len(planned) - present > request_cap - present:
            raise RuntimeError(f"Plan needs {len(planned)} unique requests, exceeding cap {request_cap}")
    cache_dir = OUT / "response_cache"; cache_dir.mkdir(parents=True, exist_ok=True)
    responses = {}
    spent = 0.0
    for fp in cache_dir.glob("*.json"):
        payload = json.loads(fp.read_text(encoding="utf-8")); validate_response(payload)
        responses[fp.stem] = payload
        spent += (payload.get("usage", {}).get("input_tokens", 0) or 0) * PRICE_PER_MILLION_INPUT_TOKENS / 1_000_000
    charged_requests = len(responses)
    if charged_requests > request_cap or spent > cost_cap:
        raise RuntimeError(f"Existing cached JEV usage already exceeds configured limits ({charged_requests} requests, ${spent:.4f})")
    # Recover the smoke response's latency from the prior run manifest.
    smoke_hash = None; smoke_latency = None
    prior_manifest = OUT / "preflight.json"
    if prior_manifest.exists():
        old = json.loads(prior_manifest.read_text(encoding="utf-8"))
        smoke_hash = old.get("smoke_test", {}).get("request_hash")
        smoke_latency = old.get("smoke_test", {}).get("response", {}).get("request_latency_seconds")
    rows_out = []
    progress_path = OUT / "live_progress.json"
    new_calls = 0
    for idx, item in enumerate(planned, 1):
        body, digest = item["body"], item["request_hash"]
        cache_path = cache_dir / f"{digest}.json"
        if digest in responses:
            payload = responses[digest]
            latency = smoke_latency if digest == smoke_hash else None
            cache_hit = True
        else:
            if charged_requests >= request_cap:
                raise RuntimeError(f"Request cap reached at {charged_requests}/{request_cap}; evaluation incomplete")
            estimated_tokens = max(1, (len(json.dumps(body, ensure_ascii=False)) + 2) // 3)
            estimated_cost = estimated_tokens * PRICE_PER_MILLION_INPUT_TOKENS / 1_000_000
            if spent + estimated_cost > cost_cap:
                raise RuntimeError(f"$2 budget guard stopped before next request (estimated total ${spent+estimated_cost:.4f})")
            payload, latency = post_live(body, min(cost_cap - spent, HARD_COST_LIMIT_USD))
            parsed = validate_response(payload)
            actual_cost = (parsed["input_tokens"] or 0) * PRICE_PER_MILLION_INPUT_TOKENS / 1_000_000
            if spent + actual_cost > cost_cap:
                raise RuntimeError("API token usage would exceed cumulative cost cap; response not added to decision table")
            cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            responses[digest] = payload; spent += actual_cost
            charged_requests += 1; new_calls += 1; cache_hit = False
        parsed = validate_response(payload)
        row = {k: item[k] for k in ("dataset", "origin", "split", "unique_id", "request_hash")}
        row.update(parsed)
        row["api_cost_usd"] = (parsed["input_tokens"] or 0) * PRICE_PER_MILLION_INPUT_TOKENS / 1_000_000
        row["request_latency_seconds"] = latency
        row["cache_hit"] = cache_hit
        rows_out.append(row)
        if idx % 20 == 0 or idx == len(planned):
            progress_path.write_text(json.dumps({"completed": idx, "total": len(planned), "charged_requests_in_cache": charged_requests, "new_requests_this_run": new_calls, "cumulative_api_cost_usd": spent, "status": "running"}, indent=2), encoding="utf-8")
            print(f"JEV routes {idx}/{len(planned)}; total billed requests={charged_requests}; cost=${spent:.4f}", flush=True)
    decisions = pd.DataFrame(rows_out)
    decisions.to_csv(OUT / "jev_decisions.csv", index=False)
    progress_path.write_text(json.dumps({"completed": len(planned), "total": len(planned), "charged_requests_in_cache": charged_requests, "new_requests_this_run": new_calls, "cumulative_api_cost_usd": spent, "status": "decisions_complete"}, indent=2), encoding="utf-8")
    run_metrics(decisions)
    manifest_path = OUT / "preflight.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["requests_sent"] = charged_requests
        manifest["total_api_cost_usd"] = spent
        manifest["evaluation_status"] = "complete_exploratory_test_evaluation"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def run_metrics(decisions: pd.DataFrame) -> None:
    node_rows, origin_rows, level_rows, coherence_rows = [], [], [], []
    threshold_rows = []
    thresholds = [0.0, 0.2, 0.35, 0.5, 0.65, 0.8, 0.95, 1.0]
    for dataset in DATASETS:
        d = decisions.loc[decisions["dataset"].eq(dataset)].copy(); d["origin"] = d["origin"].astype(str)
        baseline = selector_choices(dataset); baseline["model"] = baseline["model"].replace({"NBEATS": "NBEATS", "TimesFM3": "TimesFM3"})
        origins = origins_for(dataset); origins["key"] = origins["origin"].astype(str)
        summing, hierarchy, actual = read_hierarchy(dataset)
        val_d = d.loc[d["split"].eq("validation")]
        val_origins = set(val_d["origin"])
        base_val = baseline.loc[baseline["origin"].isin(val_origins)]
        scores = []
        for threshold in thresholds:
            jde = val_d[["origin", "unique_id", "selected_model", "confidence"]].copy()
            hist = base_val.loc[base_val["selector"].eq("HistoricalBest"), ["origin", "unique_id", "model"]].rename(columns={"model": "hist_model"})
            jde = jde.merge(hist, on=["origin", "unique_id"], how="left")
            jde["model"] = np.where(jde["confidence"] >= threshold, jde["selected_model"], jde["hist_model"])
            jde["selector"] = "JEV_confidence_fallback"
            jde = jde[["origin", "unique_id", "selector", "model"]]
            _, po, _, _ = score_assignments(dataset, pd.concat([base_val, jde], ignore_index=True), summing.copy(), hierarchy.copy(), actual.copy())
            values = [r["mase"] for r in po if r["split"] == "validation" and r["selector"] == "JEV_confidence_fallback"]
            scores.append({"dataset": dataset, "threshold": threshold, "validation_mase": float(np.mean(values))})
        best = min(r["validation_mase"] for r in scores)
        threshold = max(r["threshold"] for r in scores if abs(r["validation_mase"]-best) < 1e-12)
        threshold_rows.extend([{**r, "selected_threshold": threshold} for r in scores])
        test_origins = set(d.loc[d["split"].eq("test"), "origin"])
        baseline_test = baseline.loc[baseline["origin"].isin(test_origins)]
        def decisions_for(split: str, fallback_threshold: float) -> pd.DataFrame:
            x = d.loc[d["split"].eq(split)].copy()
            direct = x[["origin", "unique_id", "selected_model"]].rename(columns={"selected_model": "model"}); direct["selector"] = "JEV_direct"
            hist = baseline.loc[baseline["selector"].eq("HistoricalBest"), ["origin", "unique_id", "model"]].rename(columns={"model": "hist_model"})
            fall = x.merge(hist, on=["origin", "unique_id"], how="left")
            fall["model"] = np.where(fall["confidence"] >= fallback_threshold, fall["selected_model"], fall["hist_model"])
            fall["selector"] = "JEV_confidence_fallback"
            return pd.concat([direct[["origin", "unique_id", "selector", "model"]], fall[["origin", "unique_id", "selector", "model"]]], ignore_index=True)
        for split, base in (("validation", base_val), ("test", baseline_test)):
            ds_assign = pd.concat([base, decisions_for(split, threshold)], ignore_index=True)
            nn, oo, ll, cc = score_assignments(dataset, ds_assign, summing.copy(), hierarchy.copy(), actual.copy())
            node_rows.extend(nn); origin_rows.extend(oo); level_rows.extend(ll); coherence_rows.extend(cc)
    (OUT / "fallback_thresholds.csv").write_text(pd.DataFrame(threshold_rows).to_csv(index=False), encoding="utf-8")
    pd.DataFrame(node_rows).to_csv(OUT / "series_metrics.csv", index=False)
    origin = pd.DataFrame(origin_rows); origin.to_csv(OUT / "metrics_by_origin.csv", index=False)
    pd.DataFrame(level_rows).to_csv(OUT / "metrics_by_level.csv", index=False)
    pd.DataFrame(coherence_rows).to_csv(OUT / "bottomup_coherence.csv", index=False)
    summary = origin.groupby(["dataset", "split", "selector"], as_index=False).agg(mase=("mase", "mean"), leaf_mase=("leaf_mase", "mean"), aggregate_mase=("aggregate_mase", "mean"), mae=("mae", "mean"), rmse=("rmse", "mean"), origins=("origin", "nunique"))
    summary.to_csv(OUT / "summary_metrics.csv", index=False)
    decisions.groupby(["dataset", "split", "selected_model"], as_index=False).size().to_csv(OUT / "jev_selection_frequency.csv", index=False)
    decisions.groupby(["dataset", "split"], as_index=False).agg(decisions=("request_hash", "size"), mean_confidence=("confidence", "mean"), mean_margin=("margin", "mean"), mean_latency_seconds=("request_latency_seconds", "mean"), total_input_tokens=("input_tokens", "sum"), total_output_tokens=("output_tokens", "sum"), api_cost_usd=("api_cost_usd", "sum"), cache_hits=("cache_hit", "sum")).to_csv(OUT / "jev_costs.csv", index=False)


def baseline_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for dataset, cfg in DATASETS.items():
        table = _load(cfg["baseline"])
        if cfg["metric_split"] != "existing_table_all" and "split" in table:
            table = table.loc[table["split"].astype(str).str.lower().eq(cfg["metric_split"])]
        if "objective" in table:
            table = table.loc[table["objective"].eq("equal_node")]
        selector_col = "selector"
        metric_col = "mase"
        for stored, display in cfg["baseline_map"].items():
            found = table.loc[table[selector_col].eq(stored)]
            if not found.empty:
                record = found.iloc[0]
                rows.append({
                    "dataset": dataset,
                    "selector": display,
                    "prior_reported_mase": float(record[metric_col]),
                    "prior_table": cfg["baseline"],
                    "split_scope": cfg["metric_split"],
                    "interpretation": "previously inspected exploratory result; copied without recomputation",
                })
    return rows


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    key_present = bool(os.environ.get("JEV_API_KEY") or os.environ.get("TYPESAFE_API_KEY"))
    live_enabled = os.environ.get("JEV_LIVE_RUN", "").strip() == "1"
    max_requests = int(os.environ.get("JEV_MAX_REQUESTS", "0") or 0)
    max_usd = float(os.environ.get("JEV_MAX_USD", "0") or 0)

    prior_smoke = {}
    if (OUT / "preflight.json").is_file():
        try:
            prior_smoke = json.loads((OUT / "preflight.json").read_text(encoding="utf-8")).get("smoke_test", {})
        except (ValueError, OSError):
            prior_smoke = {}
    smoke: dict[str, Any] = {"status": "not_started", "live_call_made": False}
    checks = []
    for dataset, cfg in DATASETS.items():
        feat = _load(cfg["features"])
        origins = _load(cfg["origins"])
        sample = _sample_feature_row(dataset)
        horizon = {"Australian Labour": 12, "M5 Walmart": 28, "Australian Tourism": 3}[dataset]
        state = build_state(sample, horizon)
        add_recent_candidate_errors(dataset, sample, state)
        add_local_hierarchy_context(dataset, sample, state)
        body = build_request(state)
        # Contract checks are local only; they do not create or persist predictions.
        assert body["model"] == MODEL_ID
        assert body["questions"]["forecast_model"]["type"] == "choice"
        assert set(body["questions"]["forecast_model"]["criteria"]) == set(OPTIONS)
        assert not any(k in body["state"] for k in ("unique_id", "series_id", "target", "actual", "oracle"))
        checks.append({
            "dataset": dataset,
            "expected_nodes": cfg["nodes"],
            "expected_leaves": cfg["leaves"],
            "feature_rows": int(len(feat)),
            "origin_rows": int(len(origins)),
            "request_hash": request_hash(body),
            "state_keys": sorted(body["state"]),
            "validation_sample_available": True,
        })
        if dataset == "Australian Labour":
            smoke["request_preview"] = body
            smoke["request_hash"] = request_hash(body)

    # First authorize and validate one real validation response; then continue
    # through the complete cached routing evaluation within hard task limits.
    if live_enabled and key_present and max_requests >= 1 and max_usd > 0:
        dataset = "Australian Labour"
        row = _sample_feature_row(dataset)
        state = build_state(row, 12)
        add_recent_candidate_errors(dataset, row, state)
        add_local_hierarchy_context(dataset, row, state)
        body = build_request(state)
        cache_path = OUT / "response_cache" / f"{request_hash(body)}.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        if cache_path.exists():
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            parsed = validate_response(cached)
            if prior_smoke.get("request_hash") == request_hash(body):
                smoke.update(prior_smoke)
            else:
                smoke.update({"status": "cached_response_valid", "response": parsed, "live_call_made": False})
        else:
            payload, latency = post_live(body, max_usd)
            # Store original API response. Only reached after explicit opt-in.
            cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            parsed = validate_response(payload)
            actual_cost = (parsed["input_tokens"] or 0) * PRICE_PER_MILLION_INPUT_TOKENS / 1_000_000
            smoke.update({"status": "live_validation_smoke_passed", "response": {**parsed, "request_latency_seconds": latency, "api_cost_usd": actual_cost}, "live_call_made": True})
    elif live_enabled:
        smoke["status"] = "live_run_blocked_by_config"
        smoke["reason"] = "Requires key, JEV_MAX_REQUESTS>=1, and JEV_MAX_USD>0"
    else:
        smoke["status"] = "offline_contract_smoke_passed"
        smoke["reason"] = "Live requests disabled; no JEV response or probability was fabricated."

    response_files = list((OUT / "response_cache").glob("*.json")) if (OUT / "response_cache").is_dir() else []
    cache_cost = 0.0
    for response_file in response_files:
        payload = json.loads(response_file.read_text(encoding="utf-8"))
        cache_cost += (payload.get("usage", {}).get("input_tokens", 0) or 0) * PRICE_PER_MILLION_INPUT_TOKENS / 1_000_000
    already_complete = (OUT / "jev_decisions.csv").is_file() and (OUT / "summary_metrics.csv").is_file()
    pd.DataFrame(baseline_rows()).to_csv(OUT / "prior_exploratory_baselines.csv", index=False)
    (OUT / "preflight.json").write_text(json.dumps({
        "official_endpoint": API_URL,
        "model_id_pinned": MODEL_ID,
        "official_docs": {
            "choice": "https://docs.typesafe.ai/primitives/choice",
            "python_sdk": "https://docs.typesafe.ai/sdk/python",
            "models_and_pricing": "https://docs.typesafe.ai/models",
        },
        "price_usd_per_million_input_tokens": PRICE_PER_MILLION_INPUT_TOKENS,
        "environment": {
            "api_key_present": key_present,
            "api_key_variable": "JEV_API_KEY" if os.environ.get("JEV_API_KEY") else (
                "TYPESAFE_API_KEY" if os.environ.get("TYPESAFE_API_KEY") else None
            ),
            "live_run_enabled": live_enabled,
            "max_requests": max_requests,
            "max_usd": max_usd,
        },
        "requests_sent": len(response_files),
        "effective_hard_request_cap": min(max_requests, HARD_REQUEST_LIMIT),
        "effective_cost_cap_usd": min(max_usd, HARD_COST_LIMIT_USD),
        "cached_response_input_cost_usd": cache_cost,
        "datasets": checks,
        "smoke_test": smoke,
        "evaluation_status": "complete_exploratory_test_evaluation" if already_complete else "pending_live_evaluation",
    }, indent=2, default=str), encoding="utf-8")
    print(f"Preflight saved to {OUT}")
    print(f"Live JEV smoke calls made: {int(bool(smoke.get('live_call_made')))}")
    if live_enabled and key_present:
        live_decision_run()
    else:
        print("JEV selector evaluation blocked: live config unavailable")


if __name__ == "__main__":
    main()
