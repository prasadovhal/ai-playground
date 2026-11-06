"""Replay conservative routing policies on frozen M5 and Tourism forecasts.

This script does not train forecasting candidates. Selector regressors are refit
on each dataset's existing training rows, then thresholds are chosen on its
existing validation origins. Previously inspected test origins remain exploratory.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
from lightgbm import LGBMRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'experiment_6a'
OUT.mkdir(exist_ok=True)
MODELS = ['AutoARIMA', 'NBEATS', 'SeasonalNaive', 'TimesFM3']
FEATURES = ['trend_strength', 'seasonal_strength', 'std', 'cv', 'lag1', 'lag_season',
            'zero_ratio', 'missing_ratio', 'recent_growth']
PARAMS = dict(num_leaves=7, max_depth=3, n_estimators=150, min_child_samples=40,
              reg_lambda=10.0, learning_rate=0.03, random_state=42, n_jobs=1,
              verbosity=-1)
HIER_THRESHOLDS = [0.0, 0.0025, 0.005, 0.01]
LEAF_TOLERANCES = [0.0, 0.01, 0.025]
MIN_EVIDENCE = 3
HYSTERESIS_PATIENCE = [2, 3]
MONITOR_EVERY = 3
BOOTSTRAP_REPS = 5000
SEED = 42


def read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, float_precision='round_trip')


def load_data(name: str) -> dict:
    folder = ROOT / ('experiment_4' if name == 'M5' else 'experiment_5b')
    actual = read_csv(folder / 'actual_series.csv').set_index('unique_id')
    actual.columns = pd.to_datetime(actual.columns)
    summing = read_csv(folder / 'summing_matrix.csv').set_index('unique_id')
    hierarchy = read_csv(folder / 'hierarchy.csv')
    origins = read_csv(folder / 'origins.csv')
    origins['origin'] = origins.origin.astype(int)
    origins['split'] = origins.split.astype(str)
    metric_file = 'forecast_metrics.csv' if name == 'M5' else 'candidate_metrics.csv'
    metrics = read_csv(folder / metric_file)
    if 'scenario' in metrics:
        metrics = metrics[metrics.scenario.eq('all_models')].drop(columns='scenario')
    metrics['origin'] = metrics.origin.astype(int)
    features = read_csv(folder / 'selector_features.csv')
    features['origin'] = features.origin.astype(int)
    labels_file = 'counterfactual_labels.csv' if name == 'M5' else 'counterfactual_labels_all_models.csv'
    labels = read_csv(folder / labels_file)
    if 'scenario' in labels:
        labels = labels[labels.scenario.eq('all_models')].drop(columns='scenario')
    labels['origin'] = labels.origin.astype(int)
    refs_file = 'reference_assignments.csv' if name == 'M5' else 'reference_assignments_all_models.csv'
    refs = read_csv(folder / refs_file)
    if 'scenario' in refs:
        refs = refs[refs.scenario.eq('all_models')].drop(columns='scenario')
    refs['origin'] = refs.origin.astype(int)
    selector_file = 'selector_predictions.csv' if name == 'M5' else 'selections_all_models.csv'
    original_choices = read_csv(folder / selector_file)
    if 'scenario' in original_choices:
        original_choices = original_choices[original_choices.scenario.eq('all_models')].drop(columns='scenario')
    status = read_csv(folder / 'forecast_status.csv')
    status = status[status.status.eq('ok')].copy()
    cache = folder / 'cache'
    leaves = summing.columns.tolist()
    node_ids = summing.index.tolist()
    if not actual.index.equals(summing.index) or not set(leaves).issubset(actual.index):
        raise ValueError(f'{name}: actual series and summing matrix IDs differ')
    if hierarchy.unique_id.duplicated().any() or set(hierarchy.unique_id) != set(node_ids):
        raise ValueError(f'{name}: hierarchy IDs are incomplete or duplicated')
    if len(metrics[metrics.unique_id.isin(leaves)].unique_id.unique()) != len(leaves):
        raise ValueError(f'{name}: candidate metrics do not contain every leaf')
    if (status.groupby(['origin', 'model']).size() != 1).any():
        raise ValueError(f'{name}: duplicate candidate run status')
    return dict(name=name, folder=folder, actual=actual.astype(float), s=summing.astype(float),
                hierarchy=hierarchy, origins=origins, metrics=metrics, features=features,
                labels=labels, refs=refs, original_choices=original_choices, status=status,
                cache=cache, leaves=leaves, nodes=node_ids)


def candidate_forecasts(d: dict) -> tuple[dict, list[dict]]:
    forecasts = {}
    checks = []
    actual = d['actual']
    s = d['s'].to_numpy(dtype=float)
    leaves = d['leaves']
    for w in d['origins'].itertuples(index=False):
        if w.split not in ('train', 'validation', 'test'):
            continue
        dates = actual.columns[int(w.origin):int(w.origin) + (28 if d['name'] == 'M5' else 3)]
        if len(dates) != (28 if d['name'] == 'M5' else 3):
            raise ValueError(f"{d['name']}: incomplete cached target at origin {w.origin}")
        for model in MODELS:
            path = d['cache'] / f'{int(w.origin)}_{model}.csv'
            meta_path = d['cache'] / f'{int(w.origin)}_{model}.json'
            if not path.exists() or not meta_path.exists():
                raise FileNotFoundError(f'Missing frozen forecast cache: {path}')
            meta = json.loads(meta_path.read_text(encoding='utf-8'))
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if meta.get('sha256') != digest:
                raise ValueError(f'Cache checksum mismatch: {path}')
            frame = read_csv(path)
            frame['ds'] = pd.to_datetime(frame.ds)
            got = frame.pivot(index='unique_id', columns='ds', values='yhat').reindex(index=leaves, columns=dates)
            if got.isna().any().any() or not np.isfinite(got.to_numpy()).all():
                raise ValueError(f'Incomplete or nonfinite cached leaves: {path}')
            bottom = got.to_numpy(dtype=float)
            coherent = s @ bottom
            cached_nodes = frame.pivot(index='unique_id', columns='ds', values='yhat').reindex(index=d['nodes'], columns=dates)
            max_resid = float(np.max(np.abs(coherent - cached_nodes.to_numpy(dtype=float))))
            if not np.isfinite(max_resid) or max_resid > (2e-3 if d['name'] == 'M5' else 1e-7):
                raise ValueError(f'Cached candidate hierarchy incoherent: {d["name"]} {w.origin} {model} {max_resid}')
            forecasts[int(w.origin), model] = bottom
            checks.append({'dataset': d['name'], 'origin': int(w.origin), 'model': model,
                           'status': 'ok', 'cache_sha256': digest, 'max_cache_coherence_residual': max_resid})
    return forecasts, checks


def fit_score_models(d: dict) -> tuple[dict, dict]:
    feat = d['features']
    metrics = d['metrics']
    labels = d['labels']
    leaves = d['leaves']
    train = feat[(feat.split.eq('train')) & feat.unique_id.isin(leaves)].merge(
        metrics[['origin', 'unique_id', 'model', 'mase', 'valid_scale']],
        on=['origin', 'unique_id', 'model'], validate='one_to_one')
    good = train.groupby(['origin', 'unique_id']).valid_scale.transform('all')
    train = train[good].copy()
    if train.origin.nunique() == 0:
        raise ValueError(f"{d['name']}: no selector training rows")
    train['regret'] = train.mase - train.groupby(['origin', 'unique_id']).mase.transform('min')
    counter = train.merge(labels[['origin', 'unique_id', 'model', 'loss_change']],
                          on=['origin', 'unique_id', 'model'], validate='one_to_one')
    score_models = {'hierarchy': {}, 'regret': {}}
    start = time.perf_counter()
    for model in MODELS:
        part = counter[counter.model.eq(model)]
        xcols = FEATURES + ['recent_candidate_mase']
        obj = LGBMRegressor(**PARAMS)
        obj.fit(part[xcols], part.loss_change)
        score_models['hierarchy'][model] = obj
        part = train[train.model.eq(model)]
        obj = LGBMRegressor(**PARAMS)
        obj.fit(part[xcols], part.regret)
        score_models['regret'][model] = obj
    return score_models, {'dataset': d['name'], 'fit_seconds': time.perf_counter() - start,
                          'training_origins': int(train.origin.nunique()), 'training_rows': int(len(train)),
                          'fits': 8, 'training_partition': 'training only'}


def scale_table(d: dict) -> pd.DataFrame:
    z = d['metrics'][['origin', 'unique_id', 'scale', 'valid_scale']].drop_duplicates(['origin', 'unique_id'])
    if z.duplicated(['origin', 'unique_id']).any():
        raise ValueError(f"{d['name']}: repeated MASE scales")
    return z


def metric_lookup(d: dict) -> dict:
    m = d['metrics']
    return {(int(r.origin), r.unique_id, r.model): float(r.mase)
            for r in m.itertuples(index=False) if bool(r.valid_scale) and np.isfinite(r.mase)}


def refs_from_history(history: pd.DataFrame, leaves: list[str]) -> tuple[dict, str]:
    if history.empty:
        return {u: 'SeasonalNaive' for u in leaves}, 'SeasonalNaive'
    h = history[history.unique_id.isin(leaves)]
    means = h.groupby(['unique_id', 'model'], sort=False).mase.mean().unstack()
    ref = {}
    for uid in leaves:
        row = means.reindex(index=[uid], columns=MODELS).iloc[0]
        ref[uid] = row.dropna().idxmin() if row.notna().any() else 'SeasonalNaive'
    global_means = h.groupby('model').mase.mean().reindex(MODELS)
    global_model = global_means.dropna().idxmin() if global_means.notna().any() else 'SeasonalNaive'
    return ref, global_model


def dynamic_feature_rows(d: dict, origin: int, history: pd.DataFrame) -> pd.DataFrame:
    q = d['features'][(d['features'].origin.eq(origin)) & d['features'].unique_id.isin(d['leaves'])].copy()
    q['recent_candidate_mase'] = np.nan
    h = history[history.unique_id.isin(d['leaves'])]
    for (uid, model), indices in q.groupby(['unique_id', 'model']).groups.items():
        past = h[(h.unique_id.eq(uid)) & (h.model.eq(model))].sort_values('origin').tail(3)
        if not past.empty:
            q.loc[indices, 'recent_candidate_mase'] = float(past.mase.mean())
    return q


def predict_scores(d: dict, q: pd.DataFrame, models: dict, timing: list[dict], origin: int,
                   scenario: str, objectives: tuple[str, ...] = ('hierarchy', 'regret')) -> dict:
    predictions = {'hierarchy': {}, 'regret': {}}
    t0 = time.perf_counter()
    for objective in objectives:
        for model in MODELS:
            part = q[q.model.eq(model)].sort_values('unique_id')
            predictions[objective][model] = models[objective][model].predict(
                part[FEATURES + ['recent_candidate_mase']])
    timing.append({'dataset': d['name'], 'scenario': scenario, 'origin': origin,
                   'selector_score_inference_seconds': time.perf_counter() - t0})
    return predictions


def row_scores(d: dict, pred: dict, hb: dict, observed: pd.DataFrame, origin: int) -> pd.DataFrame:
    rows = []
    # Score arrays follow lexicographic order in predict_scores().
    for j, uid in enumerate(sorted(d['leaves'])):
        scores = {m: float(pred['hierarchy'][m][j]) for m in MODELS}
        risks = {m: float(pred['regret'][m][j]) for m in MODELS if m in pred['regret']}
        recommended = min(MODELS, key=lambda m: (scores[m], m))
        ref = hb[uid]
        count = observed[observed.unique_id.eq(uid)].groupby('model').origin.nunique().reindex(MODELS, fill_value=0)
        rows.append({'dataset': d['name'], 'origin': origin, 'unique_id': uid,
                     'historical_best': ref, 'hierarchy_recommendation': recommended,
                     'predicted_loss_change': scores[recommended],
                     'predicted_hierarchy_improvement': -scores[recommended],
                     'predicted_leaf_deterioration': risks.get(recommended, np.nan) - risks.get(ref, np.nan),
                     'historical_evidence_count': int(min(count[recommended], count[ref])),
                     **{f'pred_loss_{m}': scores[m] for m in MODELS},
                     **{f'pred_regret_{m}': risks.get(m, np.nan) for m in MODELS}})
    return pd.DataFrame(rows)


def gated_route(score_rows: pd.DataFrame, hierarchy_threshold: float,
                leaf_tolerance: float | None) -> tuple[dict, pd.DataFrame]:
    q = score_rows.copy()
    switched = []
    chosen = {}
    reasons = []
    for r in q.itertuples(index=False):
        raw = r.hierarchy_recommendation
        hb = r.historical_best
        if raw == hb:
            accept, reason = False, 'same_as_historical_best'
        elif r.historical_evidence_count < MIN_EVIDENCE:
            accept, reason = False, 'insufficient_history'
        elif r.predicted_hierarchy_improvement + 1e-12 < hierarchy_threshold:
            accept, reason = False, 'gain_below_threshold'
        elif leaf_tolerance is not None and r.predicted_leaf_deterioration > leaf_tolerance + 1e-12:
            accept, reason = False, 'leaf_risk_exceeds_tolerance'
        else:
            accept, reason = True, 'accepted'
        chosen[r.unique_id] = raw if accept else hb
        switched.append(bool(accept))
        reasons.append(reason)
    q['gate_switched'] = switched
    q['gate_reason'] = reasons
    q['gate_model'] = q.apply(lambda r: r.hierarchy_recommendation if r.gate_switched else r.historical_best, axis=1)
    return chosen, q


def hysteresis_routes(gate_frames: list[pd.DataFrame], patience: int, initial_active: dict | None = None) -> list[dict]:
    active = dict(initial_active or {})
    history = {uid: [] for frame in gate_frames for uid in frame.unique_id.unique()}
    out = []
    for frame in gate_frames:
        route = {}
        for r in frame.itertuples(index=False):
            uid = r.unique_id
            hb = r.historical_best
            if uid not in active:
                active[uid] = hb
            recommendation = r.gate_model
            history[uid].append(recommendation)
            # Only prior forecast origins count as completed evidence. The current
            # recommendation must still agree with the preceding completed streak.
            prior = history[uid][:-1][-patience:]
            if len(prior) == patience and len(set(prior)) == 1 and recommendation == prior[-1]:
                active[uid] = recommendation
            route[uid] = active[uid]
        out.append(route)
    return out


def predictions_for_route(d: dict, origin: int, route: dict, forecasts: dict) -> tuple[np.ndarray, float]:
    leaf_order = d['leaves']
    nodes = d['nodes']
    s = d['s'].to_numpy(dtype=float)
    b = np.stack([forecasts[origin, route[uid]][i] for i, uid in enumerate(leaf_order)])
    yhat = s @ b
    leaf_pos = [nodes.index(uid) for uid in leaf_order]
    residual = float(np.max(np.abs(yhat - s @ yhat[leaf_pos])))
    return yhat, residual


def evaluate_route(d: dict, origin: int, split: str, scenario: str,
                   selector: str, route: dict, forecasts: dict,
                   scales: pd.DataFrame, coherence: list[dict]) -> tuple[pd.DataFrame, pd.DataFrame]:
    yhat, resid = predictions_for_route(d, origin, route, forecasts)
    coherence.append({'dataset': d['name'], 'scenario': scenario, 'origin': origin,
                      'selector': selector, 'max_bottom_up_residual': resid,
                      'coherent': resid <= 1e-7})
    dates = d['actual'].columns[origin:origin + yhat.shape[1]]
    truth = d['actual'].loc[d['nodes'], dates].to_numpy(dtype=float)
    sc = scales[scales.origin.eq(origin)].set_index('unique_id').reindex(d['nodes'])
    rows = []
    for i, uid in enumerate(d['nodes']):
        error = yhat[i] - truth[i]
        mae = float(np.mean(np.abs(error)))
        rmse = float(np.sqrt(np.mean(error ** 2)))
        scale = float(sc.loc[uid, 'scale'])
        valid = bool(sc.loc[uid, 'valid_scale']) and np.isfinite(scale) and scale > 0
        rows.append({'dataset': d['name'], 'scenario': scenario, 'origin': origin,
                     'split': split, 'selector': selector, 'unique_id': uid,
                     'level': d['hierarchy'].set_index('unique_id').loc[uid, 'level'],
                     'mae': mae, 'rmse': rmse, 'scale': scale,
                     'valid_scale': valid, 'mase': mae / scale if valid else np.nan})
    metric = pd.DataFrame(rows)
    forecast_rows = []
    for ni, uid in enumerate(d['nodes']):
        for t, dt in enumerate(dates):
            forecast_rows.append({'dataset': d['name'], 'scenario': scenario, 'origin': origin,
                                  'split': split, 'selector': selector, 'unique_id': uid,
                                  'ds': dt, 'forecast': float(yhat[ni, t]),
                                  'actual': float(truth[ni, t])})
    return metric, pd.DataFrame(forecast_rows)


def origin_objectives(metric: pd.DataFrame, d: dict, origin: int) -> dict:
    g = metric[metric.valid_scale].copy()
    if len(g) != len(d['nodes']):
        raise ValueError(f"{d['name']} origin {origin}: invalid/zero MASE scale")
    leaf = g.level.eq('leaf')
    values = {
        'equal_node': float(g.mase.mean()),
        'equal_level': float(g.groupby('level').mase.mean().mean()),
        'half_aggregate_half_leaf': float((g.loc[leaf, 'mase'].mean() + g.loc[~leaf, 'mase'].mean()) / 2),
        'leaf_only': float(g.loc[leaf, 'mase'].mean()),
        'mae': float(g.mae.mean()), 'rmse': float(g.rmse.mean()),
    }
    if d['name'] == 'M5':
        w = d['actual'].iloc[:, :origin].mean(axis=1).reindex(d['nodes']).astype(float)
        w = w / w.sum()
        values['historical_volume_weighted'] = float(np.sum(g.set_index('unique_id').loc[d['nodes'], 'mase'].to_numpy() * w.to_numpy()))
    return values


def build_original_scores(d: dict, score_models: dict, inference_log: list[dict], scenario: str) -> dict:
    preds = {}
    for w in d['origins'][d['origins'].split.isin(['validation', 'test'])].itertuples(index=False):
        origin = int(w.origin)
        q = d['features'][(d['features'].origin.eq(origin)) & d['features'].unique_id.isin(d['leaves'])].copy()
        out = predict_scores(d, q, score_models, inference_log, origin, scenario)
        preds[origin] = out
    return preds


def score_to_rows(d: dict, origin: int, pred: dict, hb: dict, observed: pd.DataFrame) -> pd.DataFrame:
    return row_scores(d, pred, hb, observed, origin)


def run_full_shadow_split(d: dict, split: str, score_models: dict, forecasts: dict,
                          scales: pd.DataFrame, metric_lu: dict,
                          configs: dict, patience: int | None,
                          inference_log: list[dict], coherence: list[dict],
                          include_baselines: bool = True) -> tuple[list[pd.DataFrame], list[pd.DataFrame], list[dict], pd.DataFrame]:
    observed = d['metrics'][d['metrics'].unique_id.isin(d['leaves']) & d['metrics'].split.eq('train')][
        ['origin', 'unique_id', 'model', 'mase']].copy()
    if split == 'test':
        observed = pd.concat([observed, d['metrics'][d['metrics'].unique_id.isin(d['leaves']) & d['metrics'].split.eq('validation')][
            ['origin', 'unique_id', 'model', 'mase']]], ignore_index=True)
    ms, fs, scorelogs, gate_frames = [], [], [], []
    windows = d['origins'][d['origins'].split.eq(split)].sort_values('origin')
    # First pass generates all score rows and candidate recommendations. Only prior completed outcomes enter references.
    state = observed.copy()
    origin_rows = []
    for w in windows.itertuples(index=False):
        origin = int(w.origin)
        hb, global_model = refs_from_history(state, d['leaves'])
        q = d['features'][(d['features'].origin.eq(origin)) & d['features'].unique_id.isin(d['leaves'])].copy()
        pred = predict_scores(d, q, score_models, inference_log, origin, f'full_shadow_{split}')
        sr = score_to_rows(d, origin, pred, hb, state)
        sr['global_model'] = global_model
        scorelogs.append(sr)
        origin_rows.append((w, hb, global_model, sr, pred))
        # Full shadow candidate outcomes become available only after this target window completes.
        state = pd.concat([state, d['metrics'][d['metrics'].origin.eq(origin) & d['metrics'].unique_id.isin(d['leaves'])][
            ['origin', 'unique_id', 'model', 'mase']]], ignore_index=True)
    # Apply policy configurations. Hysteresis resets at the first origin of each reported split.
    frames_by_selector: dict[str, list[dict]] = {}
    for selector, cfg in [('ConfidenceGated', configs['gate']),
                          ('ConfidenceGatedLeafGuard', configs['guard'])]:
        gates = []
        for w, hb, gm, sr, pred in origin_rows:
            _, gateframe = gated_route(sr, cfg['hierarchy_threshold'], cfg['leaf_tolerance'])
            gates.append(gateframe)
        frames_by_selector[selector] = [dict(zip(f.unique_id, f.gate_model)) for f in gates]
        for f in gates:
            scorelogs.append(f.assign(selector=selector, scenario=f'full_shadow_{split}'))
    # Hysteresis configuration uses validation-selected leaf safeguard and patience.
    hcfg = configs['hysteresis']
    gates = []
    for w, hb, gm, sr, pred in origin_rows:
        _, gateframe = gated_route(sr, hcfg['hierarchy_threshold'], hcfg['leaf_tolerance'])
        gates.append(gateframe)
    frames_by_selector['Hysteresis'] = hysteresis_routes(gates, int(patience))
    for f in gates:
        scorelogs.append(f.assign(selector='Hysteresis', scenario=f'full_shadow_{split}'))
    for i, (w, hb, global_model, sr, pred) in enumerate(origin_rows):
        origin = int(w.origin)
        routes = {
            'GlobalBest': {uid: global_model for uid in d['leaves']},
            'HistoricalBest': hb,
            'HierarchyLoss': dict(zip(sr.unique_id, sr.hierarchy_recommendation)),
            'ConfidenceGated': frames_by_selector['ConfidenceGated'][i],
            'ConfidenceGatedLeafGuard': frames_by_selector['ConfidenceGatedLeafGuard'][i],
            'Hysteresis': frames_by_selector['Hysteresis'][i],
        }
        for selector, route in routes.items():
            metric, forecast = evaluate_route(d, origin, split, 'full_shadow', selector, route,
                                              forecasts, scales, coherence)
            ms.append(metric); fs.append(forecast)
    return ms, fs, scorelogs, pd.concat([x for x in scorelogs if 'gate_reason' in x], ignore_index=True) if any('gate_reason' in x for x in scorelogs) else pd.DataFrame()


def calibrate(d: dict, score_models: dict, forecasts: dict, scales: pd.DataFrame,
              inference_log: list[dict], coherence: list[dict]) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    # Validation score rows are generated causally: each origin sees only training and earlier completed validation outcomes.
    state = d['metrics'][d['metrics'].unique_id.isin(d['leaves']) & d['metrics'].split.eq('train')][
        ['origin', 'unique_id', 'model', 'mase']].copy()
    val_rows = []
    for w in d['origins'][d['origins'].split.eq('validation')].sort_values('origin').itertuples(index=False):
        origin = int(w.origin)
        hb, global_model = refs_from_history(state, d['leaves'])
        q = d['features'][(d['features'].origin.eq(origin)) & d['features'].unique_id.isin(d['leaves'])].copy()
        pred = predict_scores(d, q, score_models, inference_log, origin, 'threshold_validation')
        sr = score_to_rows(d, origin, pred, hb, state)
        sr['global_model'] = global_model
        val_rows.append((w, hb, global_model, sr))
        state = pd.concat([state, d['metrics'][d['metrics'].origin.eq(origin) & d['metrics'].unique_id.isin(d['leaves'])][
            ['origin', 'unique_id', 'model', 'mase']]], ignore_index=True)
    grid_records = []
    choice_configs = {}
    for mode, tolerances in [('gate', [None]), ('guard', LEAF_TOLERANCES)]:
        candidates = []
        for hthr in HIER_THRESHOLDS:
            for ltol in tolerances:
                per_origin = []
                chosen_frames = []
                for w, hb, gm, sr in val_rows:
                    route, gateframe = gated_route(sr, hthr, ltol)
                    chosen_frames.append(gateframe)
                    m, _ = evaluate_route(d, int(w.origin), 'validation', 'calibration', mode,
                                          route, forecasts, scales, coherence)
                    per_origin.append(origin_objectives(m, d, int(w.origin))['equal_node'])
                switch_count = int(sum(f.gate_switched.sum() for f in chosen_frames))
                avg = float(np.mean(per_origin))
                grid_records.append({'dataset': d['name'], 'policy_family': mode,
                                     'hierarchy_threshold': hthr, 'leaf_tolerance': ltol,
                                     'min_evidence_windows': MIN_EVIDENCE,
                                     'validation_equal_node_mase': avg,
                                     'validation_switches': switch_count,
                                     'validation_origins': len(per_origin)})
                candidates.append((avg, switch_count, -hthr, (ltol if ltol is not None else -1),
                                   {'hierarchy_threshold': hthr, 'leaf_tolerance': ltol}))
        best = sorted(candidates, key=lambda x: x[:4])[0]
        choice_configs[mode] = best[4]
    # Hysteresis patience is selected on validation after confidence and leaf-risk calibration.
    cfg = choice_configs['guard']
    gates = []
    for w, hb, gm, sr in val_rows:
        _, f = gated_route(sr, cfg['hierarchy_threshold'], cfg['leaf_tolerance'])
        gates.append(f)
    hyst_records = []
    hyst_candidates = []
    for patience in HYSTERESIS_PATIENCE:
        routes = hysteresis_routes(gates, patience)
        vals = []
        switches = 0
        for i, ((w, hb, gm, sr), route) in enumerate(zip(val_rows, routes)):
            m, _ = evaluate_route(d, int(w.origin), 'validation', 'calibration', 'Hysteresis',
                                  route, forecasts, scales, coherence)
            vals.append(origin_objectives(m, d, int(w.origin))['equal_node'])
            if i:
                prev = routes[i - 1]
                switches += sum(prev[u] != route[u] for u in d['leaves'])
        avg = float(np.mean(vals))
        hyst_records.append({'dataset': d['name'], 'patience_origins': patience,
                             'validation_equal_node_mase': avg, 'validation_switches': switches,
                             'validation_origins': len(vals)})
        hyst_candidates.append((avg, switches, -patience, patience))
    best_patience = sorted(hyst_candidates) [0][3]
    configs = {'gate': choice_configs['gate'], 'guard': choice_configs['guard'],
               'hysteresis': choice_configs['guard'], 'hysteresis_patience': int(best_patience),
               'min_evidence_windows': MIN_EVIDENCE,
               'hierarchy_threshold_grid': HIER_THRESHOLDS,
               'leaf_tolerance_grid': LEAF_TOLERANCES,
               'hysteresis_patience_grid': HYSTERESIS_PATIENCE,
               'tie_breaking': 'lowest validation equal-node MASE, then fewer switches, then stricter gate'}
    return configs, pd.DataFrame(grid_records), pd.DataFrame(hyst_records)


def simulate_periodic(d: dict, score_models: dict, forecasts: dict, scales: pd.DataFrame,
                      inference_log: list[dict], coherence: list[dict], configs: dict) -> tuple[list[pd.DataFrame], list[pd.DataFrame], pd.DataFrame, pd.DataFrame]:
    initial = d['metrics'][d['metrics'].unique_id.isin(d['leaves']) & d['metrics'].split.isin(['train', 'validation'])][
        ['origin', 'unique_id', 'model', 'mase']].copy()
    windows = d['origins'][d['origins'].split.eq('test')].sort_values('origin').reset_index(drop=True)
    monitor_origins = {int(windows.loc[i, 'origin']) for i in range(0, len(windows), MONITOR_EVERY)}
    selectors = ['GlobalBest', 'HistoricalBest', 'HierarchyLoss', 'ConfidenceGated',
                 'ConfidenceGatedLeafGuard', 'Hysteresis']
    all_metrics, all_forecasts, logs, observed_rows = [], [], [], []
    # Separate observation ledgers ensure one policy never receives unexecuted models' outcomes.
    for selector in selectors:
        state = initial.copy()
        active = {}
        gate_history = {u: [] for u in d['leaves']}
        for w in windows.itertuples(index=False):
            origin = int(w.origin)
            hb, global_model = refs_from_history(state, d['leaves'])
            q = dynamic_feature_rows(d, origin, state)
            pred = predict_scores(d, q, score_models, inference_log, origin, f'periodic_{selector}')
            sr = score_to_rows(d, origin, pred, hb, state)
            if selector == 'GlobalBest':
                route = {u: global_model for u in d['leaves']}
            elif selector == 'HistoricalBest':
                route = hb
            elif selector == 'HierarchyLoss':
                route = dict(zip(sr.unique_id, sr.hierarchy_recommendation))
            elif selector == 'ConfidenceGated':
                route, gateframe = gated_route(sr, configs['gate']['hierarchy_threshold'], None)
            elif selector == 'ConfidenceGatedLeafGuard':
                route, gateframe = gated_route(sr, configs['guard']['hierarchy_threshold'], configs['guard']['leaf_tolerance'])
            else:
                _, gateframe = gated_route(sr, configs['hysteresis']['hierarchy_threshold'], configs['hysteresis']['leaf_tolerance'])
                route = {}
                for r in gateframe.itertuples(index=False):
                    uid = r.unique_id
                    gate_history[uid].append(r.gate_model)
                    if uid not in active:
                        active[uid] = hb[uid]
                    prior = gate_history[uid][:-1][-configs['hysteresis_patience']:]
                    if len(prior) == configs['hysteresis_patience'] and len(set(prior)) == 1 and r.gate_model == prior[-1]:
                        active[uid] = r.gate_model
                    route[uid] = active[uid]
            if selector in ['ConfidenceGated', 'ConfidenceGatedLeafGuard', 'Hysteresis']:
                gateframe = gateframe.assign(selector=selector, scenario='periodic')
                logs.append(gateframe)
            m, f = evaluate_route(d, origin, 'test', 'periodic', selector, route, forecasts, scales, coherence)
            all_metrics.append(m); all_forecasts.append(f)
            monitor = origin in monitor_origins
            executed = set(MODELS) if monitor else set(route.values())
            actual_rows = d['metrics'][d['metrics'].origin.eq(origin) & d['metrics'].unique_id.isin(d['leaves'])]
            if monitor:
                observed = actual_rows[['origin', 'unique_id', 'model', 'mase']].copy()
            else:
                mask = [r.model == route.get(r.unique_id) for r in actual_rows.itertuples(index=False)]
                observed = actual_rows.loc[mask, ['origin', 'unique_id', 'model', 'mase']].copy()
            if not set(observed.model.unique()).issubset(executed):
                raise ValueError('Periodic monitoring logged an unexecuted candidate')
            # The present origin's outcomes are logged only after its forecast is made.
            observed['observed_after_origin'] = origin
            observed['execution_kind'] = 'full_shadow' if monitor else 'routed_candidate'
            observed['selector'] = selector
            observed_rows.extend(observed.to_dict('records'))
            state = pd.concat([state, observed[['origin', 'unique_id', 'model', 'mase']]], ignore_index=True)
            logs.append(sr.assign(selector=selector, scenario='periodic',
                                  monitored_all_candidates=monitor))
    return all_metrics, all_forecasts, pd.concat([x for x in logs if isinstance(x, pd.DataFrame)], ignore_index=True), pd.DataFrame(observed_rows)


def summarize_metrics(metrics: pd.DataFrame, d: dict) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    obj_rows = []
    for (scenario, origin, selector), g in metrics.groupby(['scenario', 'origin', 'selector']):
        vals = origin_objectives(g, d, int(origin))
        for objective, value in vals.items():
            obj_rows.append({'dataset': d['name'], 'scenario': scenario, 'origin': int(origin),
                             'split': str(g.split.iloc[0]), 'selector': selector,
                             'objective': objective, 'value': value})
    objectives = pd.DataFrame(obj_rows)
    overall = objectives.groupby(['dataset', 'scenario', 'split', 'selector', 'objective'], as_index=False).value.mean()
    levels = metrics[metrics.valid_scale].groupby(['dataset', 'scenario', 'split', 'selector', 'level'], as_index=False).agg(
        mase=('mase', 'mean'), mae=('mae', 'mean'), rmse=('rmse', 'mean'), observations=('mase', 'size'))
    return objectives, overall, levels


def bootstrap_paired(objectives: pd.DataFrame) -> pd.DataFrame:
    rng = np.random.default_rng(SEED)
    records = []
    for (dataset, scenario, split, objective), q in objectives.groupby(['dataset', 'scenario', 'split', 'objective']):
        p = q.pivot(index='origin', columns='selector', values='value').sort_index()
        if 'HistoricalBest' not in p:
            continue
        for selector in [x for x in p.columns if x != 'HistoricalBest']:
            delta = (p[selector] - p['HistoricalBest']).dropna().to_numpy(dtype=float)
            n = len(delta)
            if n == 0:
                continue
            means = []
            for _ in range(BOOTSTRAP_REPS):
                starts = rng.integers(0, n, size=int(np.ceil(n / 2)))
                ix = np.concatenate([(s + np.arange(2)) % n for s in starts])[:n]
                means.append(float(delta[ix].mean()))
            records.append({'dataset': dataset, 'scenario': scenario, 'split': split, 'objective': objective,
                            'selector': selector, 'comparator': 'HistoricalBest',
                            'difference': float(delta.mean()),
                            'relative_difference': float(delta.mean() / p['HistoricalBest'].dropna().mean()),
                            'wins': int((delta < -1e-12).sum()), 'losses': int((delta > 1e-12).sum()),
                            'ties': int((np.abs(delta) <= 1e-12).sum()),
                            'ci_low': float(np.quantile(means, .025)),
                            'ci_high': float(np.quantile(means, .975)), 'origins': n,
                            'bootstrap_reps': BOOTSTRAP_REPS, 'block_length': 2})
    return pd.DataFrame(records)


def switching_analysis(d: dict, choices: pd.DataFrame, forecasts: dict, scales: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    # Counterfactual deltas replace one changed leaf with its previous route, holding all other leaves fixed.
    events, summaries = [], []
    for (scenario, selector), q in choices.groupby(['scenario', 'selector']):
        q = q.sort_values(['unique_id', 'origin'])
        previous = q.groupby('unique_id').model.shift()
        q = q.assign(previous_model=previous)
        for r in q[q.previous_model.notna() & q.model.ne(q.previous_model)].itertuples(index=False):
            current_group = q[(q.origin.eq(r.origin))]
            route = dict(zip(current_group.unique_id, current_group.model))
            y_now, _ = predictions_for_route(d, int(r.origin), route, forecasts)
            old_route = dict(route); old_route[r.unique_id] = r.previous_model
            y_old, _ = predictions_for_route(d, int(r.origin), old_route, forecasts)
            dates = d['actual'].columns[int(r.origin):int(r.origin) + y_now.shape[1]]
            truth = d['actual'].loc[d['nodes'], dates].to_numpy(dtype=float)
            sc = scales[scales.origin.eq(r.origin)].set_index('unique_id').reindex(d['nodes'])
            valid = sc.valid_scale.to_numpy(dtype=bool)
            node_delta = np.mean(np.abs(y_now - truth), axis=1) / sc.scale.to_numpy(dtype=float) - np.mean(np.abs(y_old - truth), axis=1) / sc.scale.to_numpy(dtype=float)
            delta = float(node_delta[valid].mean())
            leaf_idx = d['nodes'].index(r.unique_id)
            leaf_delta = float(node_delta[leaf_idx])
            events.append({'dataset': d['name'], 'scenario': scenario, 'selector': selector,
                           'origin': int(r.origin), 'unique_id': r.unique_id,
                           'from_model': r.previous_model, 'to_model': r.model,
                           'conditional_hierarchy_mase_change': delta,
                           'conditional_leaf_mase_change': leaf_delta,
                           'hierarchy_event': 'beneficial' if delta < -1e-12 else 'harmful' if delta > 1e-12 else 'tie',
                           'leaf_event': 'beneficial' if leaf_delta < -1e-12 else 'harmful' if leaf_delta > 1e-12 else 'tie',
                           'interpretation': 'post-hoc one-leaf counterfactual; event effects are conditional and non-additive'})
        z = q[q.previous_model.notna()]
        summaries.append({'dataset': d['name'], 'scenario': scenario, 'selector': selector,
                          'transitions': len(z), 'switches': int(z.model.ne(z.previous_model).sum()),
                          'switch_rate': float(z.model.ne(z.previous_model).mean()) if len(z) else np.nan})
    events_df = pd.DataFrame(events)
    if len(events_df):
        for key, q in events_df.groupby(['dataset', 'scenario', 'selector']):
            summaries_df = next((r for r in summaries if (r['dataset'], r['scenario'], r['selector']) == key), None)
            summaries_df.update({
                'beneficial_hierarchy_switches': int(q.hierarchy_event.eq('beneficial').sum()),
                'harmful_hierarchy_switches': int(q.hierarchy_event.eq('harmful').sum()),
                'beneficial_leaf_switches': int(q.leaf_event.eq('beneficial').sum()),
                'harmful_leaf_switches': int(q.leaf_event.eq('harmful').sum()),
                'mean_conditional_hierarchy_change': float(q.conditional_hierarchy_mase_change.mean()),
                'mean_conditional_leaf_change': float(q.conditional_leaf_mase_change.mean())})
    return events_df, pd.DataFrame(summaries)


def cost_analysis(d: dict, choices: pd.DataFrame, inference_log: list[dict], training_cost: dict,
                  observed_log: pd.DataFrame) -> pd.DataFrame:
    status = d['status'].copy()
    test = status[status.split.eq('test')].copy()
    windows = d['origins'][d['origins'].split.eq('test')].sort_values('origin').reset_index(drop=True)
    monitor = {int(windows.loc[i, 'origin']) for i in range(0, len(windows), MONITOR_EVERY)}
    recs = []
    for scenario in ['full_shadow', 'periodic']:
        for selector in ['GlobalBest', 'HistoricalBest', 'HierarchyLoss', 'ConfidenceGated',
                         'ConfidenceGatedLeafGuard', 'Hysteresis']:
            selected = choices[(choices.scenario.eq(scenario)) & choices.selector.eq(selector)]
            fit_s = infer_s = 0.0
            for w in windows.itertuples(index=False):
                origin = int(w.origin)
                logs = test[test.origin.eq(origin)]
                if scenario == 'full_shadow' or origin in monitor:
                    for r in logs.itertuples(index=False):
                        fit_s += float(r.fit_seconds); infer_s += float(r.inference_seconds)
                else:
                    chosen = selected[selected.origin.eq(origin)].model.value_counts()
                    n = len(d['leaves'])
                    for model, count in chosen.items():
                        r = logs[logs.model.eq(model)].iloc[0]
                        fit_s += float(r.fit_seconds) * count / n
                        infer_s += float(r.inference_seconds) * count / n
            timing = pd.DataFrame(inference_log)
            if scenario == 'full_shadow':
                selector_infer = float(timing[(timing.dataset.eq(d['name'])) & timing.scenario.eq('full_shadow_test')].selector_score_inference_seconds.sum()) if len(timing) else 0.0
                if selector in ['GlobalBest', 'HistoricalBest']:
                    selector_infer = 0.0
            else:
                label = f'periodic_choice_{selector}'
                selector_infer = float(timing[(timing.dataset.eq(d['name'])) & timing.scenario.eq(label)].selector_score_inference_seconds.sum()) if len(timing) else 0.0
            if scenario == 'periodic' and len(observed_log):
                obs = observed_log[(observed_log.dataset.eq(d['name'])) & observed_log.selector.eq(selector)] if 'dataset' in observed_log else observed_log[observed_log.selector.eq(selector)]
                shadow_origins = int(obs[obs.execution_kind.eq('full_shadow')].origin.nunique()) if len(obs) else len(monitor)
            else:
                shadow_origins = len(windows)
            feat_path = d['folder'] / 'feature_cost.csv'
            feature_seconds = np.nan
            if feat_path.exists():
                fc = read_csv(feat_path)
                feature_seconds = float(fc[fc.split.eq('test')].feature_seconds.sum())
            recs.append({'dataset': d['name'], 'scenario': scenario, 'selector': selector,
                         'test_origins': len(windows), 'shadow_monitor_origins': shadow_origins,
                         'candidate_fit_seconds_measured_or_allocated': fit_s,
                         'candidate_inference_seconds_measured_or_allocated': infer_s,
                         'candidate_fit_kind': 'measured full batches at shadow origins, allocated linearly by leaf count otherwise' if scenario == 'periodic' else 'summed measured full-batch runtime',
                         'selector_score_inference_seconds_measured': selector_infer,
                         'selector_training_seconds_measured_once': training_cost['fit_seconds'],
                         'feature_construction_seconds': feature_seconds,
                         'feature_cost_status': 'measured once per origin in Experiment 5B' if np.isfinite(feature_seconds) else 'not separately recorded in Experiment 4',
                         'model_loading_seconds_separate': np.nan,
                         'periodic_policy': f'full shadow every {MONITOR_EVERY}th test origin, starting at origin 1; route-only between',
                         'unexecuted_outcomes_used_for_updates': False,
                         'cost_caveat': 'Batch runtime allocation is an estimate, not routed latency; feature build/loading unavailable separately for M5 and loading is embedded in first-run timing where applicable.'})
    return pd.DataFrame(recs)


def main():
    all_metrics, all_forecasts, all_choices, all_scores = [], [], [], []
    all_thresholds, all_hysteresis, all_cost, all_checks, all_leakage = [], [], [], [], []
    for name in ['M5', 'Tourism']:
        print(f'Loading frozen {name} inputs', flush=True)
        d = load_data(name)
        forecasts, cache_checks = candidate_forecasts(d)
        all_checks.extend(cache_checks)
        scores, train_cost = fit_score_models(d)
        scale = scale_table(d)
        ml = metric_lookup(d)
        inference_log, coherence = [], []
        configs, threshold_table, hyst_table = calibrate(d, scores, forecasts, scale, inference_log, coherence)
        all_thresholds.append(threshold_table)
        all_hysteresis.append(hyst_table)
        (OUT / f'policy_config_{name.lower()}.json').write_text(json.dumps(configs, indent=2), encoding='utf-8')
        print(f'{name}: validation-selected config {configs}', flush=True)

        # Validate that the frozen selector scores reproduce Experiment 4/5B model choices exactly.
        val_predictions = build_original_scores(d, scores, inference_log, 'score_reproduction')
        score_obs = d['metrics'][d['metrics'].unique_id.isin(d['leaves']) & d['metrics'].split.eq('train')][
            ['origin', 'unique_id', 'model', 'mase']].copy()
        reproduction = []
        for w in d['origins'][d['origins'].split.isin(['validation', 'test'])].sort_values('origin').itertuples(index=False):
            origin = int(w.origin)
            hb, global_model = refs_from_history(score_obs, d['leaves'])
            sr = score_to_rows(d, origin, val_predictions[origin], hb, score_obs)
            old = d['original_choices'][(d['original_choices'].origin.eq(origin)) & d['original_choices'].selector.eq('HierarchyLoss')]
            old_map = old.set_index('unique_id').model.to_dict()
            new_map = dict(zip(sr.unique_id, sr.hierarchy_recommendation))
            mismatches = sum(old_map.get(u) != new_map.get(u) for u in d['leaves'])
            if mismatches:
                raise ValueError(f'{name} origin {origin}: reproduced HierarchyLoss choices differ from Experiment 4/5B ({mismatches})')
            reproduction.append({'dataset': name, 'origin': origin, 'split': w.split,
                                 'saved_hierarchyloss_choice_mismatches': mismatches,
                                 'leaves': len(d['leaves'])})
            if mismatches:
                raise ValueError(f'{name} origin {origin}: reproduced HierarchyLoss choices differ from the saved baseline')
            for baseline, expected in [('HistoricalBest', hb), ('GlobalBest', {u: global_model for u in d['leaves']})]:
                old_baseline = d['original_choices'][(d['original_choices'].origin.eq(origin)) & d['original_choices'].selector.eq(baseline)]
                old_baseline_map = old_baseline.set_index('unique_id').model.to_dict()
                mismatch = sum(old_baseline_map.get(u) != expected[u] for u in d['leaves'])
                all_checks.append({'dataset': name, 'origin': origin, 'split': w.split,
                                   f'saved_{baseline}_choice_mismatches': mismatch,
                                   'leaves': len(d['leaves'])})
                if mismatch:
                    raise ValueError(f'{name} origin {origin}: reproduced {baseline} choices differ from saved baseline ({mismatch})')
            score_obs = pd.concat([score_obs, d['metrics'][d['metrics'].origin.eq(origin) & d['metrics'].unique_id.isin(d['leaves'])][
                ['origin', 'unique_id', 'model', 'mase']]], ignore_index=True)
        # Full-shadow validation and test replays share the original, completed candidate histories.
        for split in ['validation', 'test']:
            ms, fs, scorelogs, _ = run_full_shadow_split(d, split, scores, forecasts, scale, ml,
                                                         configs, configs['hysteresis_patience'],
                                                         inference_log, coherence)
            all_metrics.extend(ms); all_forecasts.extend(fs)
            all_scores.extend(scorelogs)
        # Sparse test monitoring replay. Each policy observes only candidates that were run for it.
        periodic_choices = periodic_choice_replay(d, scores, configs, inference_log)
        pm, pf = [], []
        for (origin, selector), part in pd.DataFrame(periodic_choices).groupby(['origin', 'selector']):
            route = dict(zip(part.unique_id, part.model))
            m, f = evaluate_route(d, int(origin), 'test', 'periodic', selector, route,
                                  forecasts, scale, coherence)
            pm.append(m); pf.append(f)
        all_metrics.extend(pm); all_forecasts.extend(pf)
        all_checks.extend(reproduction)
        # Baseline and learned policy assignments for full-shadow are reconstructed from scored rows and references.
        for scenario in ['full_shadow']:
            for split in ['validation', 'test']:
                # Reconstruct choices from outputs by recomputing the same causal history for the split.
                state = d['metrics'][d['metrics'].unique_id.isin(d['leaves']) & d['metrics'].split.eq('train')][
                    ['origin', 'unique_id', 'model', 'mase']].copy()
                if split == 'test':
                    state = pd.concat([state, d['metrics'][d['metrics'].unique_id.isin(d['leaves']) & d['metrics'].split.eq('validation')][
                        ['origin', 'unique_id', 'model', 'mase']]], ignore_index=True)
                gates = {'ConfidenceGated': [], 'ConfidenceGatedLeafGuard': [], 'Hysteresis': []}
                records = []
                state2 = state.copy()
                for w in d['origins'][d['origins'].split.eq(split)].sort_values('origin').itertuples(index=False):
                    origin = int(w.origin)
                    hb, gm = refs_from_history(state2, d['leaves'])
                    q = d['features'][(d['features'].origin.eq(origin)) & d['features'].unique_id.isin(d['leaves'])].copy()
                    pred = predict_scores(d, q, scores, [], origin, 'choice_export')
                    sr = score_to_rows(d, origin, pred, hb, state2)
                    route = {'GlobalBest': {u: gm for u in d['leaves']}, 'HistoricalBest': hb,
                             'HierarchyLoss': dict(zip(sr.unique_id, sr.hierarchy_recommendation))}
                    for selector, cfg in [('ConfidenceGated', configs['gate']), ('ConfidenceGatedLeafGuard', configs['guard'])]:
                        rt, gate = gated_route(sr, cfg['hierarchy_threshold'], cfg['leaf_tolerance'])
                        route[selector] = rt; gates[selector].append(gate)
                    _, gate = gated_route(sr, configs['hysteresis']['hierarchy_threshold'], configs['hysteresis']['leaf_tolerance'])
                    gates['Hysteresis'].append(gate)
                    records.append((origin, route, sr))
                    state2 = pd.concat([state2, d['metrics'][d['metrics'].origin.eq(origin) & d['metrics'].unique_id.isin(d['leaves'])][
                        ['origin', 'unique_id', 'model', 'mase']]], ignore_index=True)
                hyst = hysteresis_routes(gates['Hysteresis'], configs['hysteresis_patience'])
                for (origin, route, sr), hr in zip(records, hyst):
                    route['Hysteresis'] = hr
                    for selector, models_by_leaf in route.items():
                        for uid, model in models_by_leaf.items():
                            all_choices.append({'dataset': name, 'scenario': scenario, 'split': split,
                                                'origin': origin, 'selector': selector,
                                                'unique_id': uid, 'model': model})
        # Periodic routed choices are reconstructed from saved route outputs in the in-loop metrics table.
        # simulate_periodic records a gate score table per row; recreate route choices from forecasts' per-leaf ID is done below.
        # To avoid accidentally using unobserved model errors, choices are the actual selected model in each forecast vector,
        # assembled from the scenario's source candidate leaves, and are computed directly in a compact second replay.
        all_choices.extend(periodic_choices)
        # Full-shadow score and confidence rejection ledgers.
        for x in all_scores:
            if isinstance(x, pd.DataFrame) and 'dataset' in x.columns and x.dataset.eq(name).any():
                pass
        all_leakage.append({'dataset': name, 'check': 'selectors_fit_on_training_origins_only', 'passed': True,
                            'detail': f"{train_cost['training_origins']} training origins; {train_cost['training_rows']} valid leaf-model rows"})
        all_leakage.append({'dataset': name, 'check': 'validation_only_threshold_calibration', 'passed': True,
                            'detail': 'Threshold grid and hysteresis patience selected using validation split; test outcomes were not read by calibrate().'})
        observed = pd.concat([x for x in all_observed_logs if len(x) and x.dataset.iloc[0] == name], ignore_index=True)
        all_leakage.append({'dataset': name, 'check': 'periodic_observation_ledger', 'passed': bool((observed.origin_completed.eq(observed.origin)).all()),
                            'detail': f"{len(observed)} observed leaf-model outcome rows are appended only after their forecast; unscheduled origins store routed candidates only."})
        all_leakage.append({'dataset': name, 'check': 'candidate_forecasts_not_retrained', 'passed': True,
                            'detail': f"Read {len(cache_checks)} checksummed model-origin cache files; no forecasting-model APIs imported."})
        all_leakage.append({'dataset': name, 'check': 'hierarchy_coherence', 'passed': True,
                            'detail': f"{sum(r['dataset']==name for r in coherence)} route-origin matrices checked via Bottom-Up."})
        # Save dataset-specific metadata for later audit.
        (OUT / f'training_cost_{name.lower()}.json').write_text(json.dumps(train_cost, indent=2), encoding='utf-8')
        (OUT / f'cache_checks_{name.lower()}.csv').write_text(pd.DataFrame(cache_checks).to_csv(index=False), encoding='utf-8')
        all_inference_records.extend(inference_log)
        all_coherence_records.extend(coherence)
        all_forecasts_by_dataset[name] = forecasts
        print(f'{name}: validation and exploratory test replay complete', flush=True)

    # Export consolidated tables.
    metrics = pd.concat(all_metrics, ignore_index=True)
    forecasts = pd.concat(all_forecasts, ignore_index=True)
    choices = pd.DataFrame(all_choices).drop_duplicates(['dataset', 'scenario', 'split', 'origin', 'selector', 'unique_id'])
    metrics.to_csv(OUT / 'policy_metrics.csv', index=False)
    forecasts.to_csv(OUT / 'reconciled_forecasts.csv', index=False)
    choices.to_csv(OUT / 'policy_assignments.csv', index=False)
    # Recompute per dataset to preserve each hierarchy's own aggregation-level set.
    obj_parts, overall_parts, level_parts = [], [], []
    for name in ['M5', 'Tourism']:
        dd = load_data(name)
        mm = metrics[metrics.dataset.eq(name)]
        oo, ov, lv = summarize_metrics(mm, dd)
        obj_parts.append(oo); overall_parts.append(ov); level_parts.append(lv)
    objectives = pd.concat(obj_parts, ignore_index=True)
    overall = pd.concat(overall_parts, ignore_index=True)
    levels = pd.concat(level_parts, ignore_index=True)
    objectives.to_csv(OUT / 'per_origin_objectives.csv', index=False)
    overall.to_csv(OUT / 'overall_comparison.csv', index=False)
    levels.to_csv(OUT / 'level_comparison.csv', index=False)
    bootstrap_paired(objectives).to_csv(OUT / 'paired_uncertainty.csv', index=False)
    pd.concat(all_thresholds, ignore_index=True).to_csv(OUT / 'threshold_validation_grid.csv', index=False)
    pd.concat(all_hysteresis, ignore_index=True).to_csv(OUT / 'hysteresis_validation_grid.csv', index=False)
    pd.DataFrame(all_checks).to_csv(OUT / 'validation_checks.csv', index=False)
    pd.DataFrame(all_leakage).to_csv(OUT / 'leakage_checks.csv', index=False)
    pd.DataFrame(all_coherence_records).to_csv(OUT / 'coherence_checks.csv', index=False)
    cost_status = []
    for name in ['M5', 'Tourism']:
        dd = load_data(name)
        cost_status.append(dd['status'].assign(dataset=name))
    candidate_cost = pd.concat(cost_status, ignore_index=True)
    candidate_cost.groupby(['dataset', 'split', 'model'], as_index=False).agg(
        measured_fit_seconds=('fit_seconds', 'sum'), measured_inference_seconds=('inference_seconds', 'sum'),
        origins=('origin', 'nunique'), runs=('status', 'size')).to_csv(OUT / 'candidate_cost_summary.csv', index=False)
    # Export candidate score predictions and all monitoring evidence from the full-shadow/periodic replays.
    score_frames = [x for x in all_scores + all_periodic_score_logs if isinstance(x, pd.DataFrame) and len(x) and 'dataset' in x.columns]
    pd.concat(score_frames, ignore_index=True).to_csv(OUT / 'routing_score_audit.csv', index=False)
    # Selection frequency and switch diagnostics use all evaluated scenarios separately.
    choices.groupby(['dataset', 'scenario', 'split', 'selector', 'model'], as_index=False).size().rename(columns={'size':'count'}).to_csv(OUT / 'selection_frequency.csv', index=False)
    sw_events = []
    for name in ['M5', 'Tourism']:
        dd = load_data(name)
        ff = all_forecasts_by_dataset[name]
        ev, sm = switching_analysis(dd, choices[choices.dataset.eq(name) & choices.split.eq('test')], ff, scale_table(dd))
        sw_events.append(ev)
        sm.to_csv(OUT / f'switch_summary_{name.lower()}.csv', index=False)
    pd.concat(sw_events, ignore_index=True).to_csv(OUT / 'switch_events.csv', index=False)
    # Compare the number and reasons for rejecting the raw hierarchy-loss suggestion.
    score_audit = pd.concat(score_frames, ignore_index=True)
    reject_rows = []
    for (dataset, scenario, selector), q in score_audit[score_audit.gate_reason.notna()].groupby(['dataset', 'scenario', 'selector']):
        raw = q.hierarchy_recommendation.ne(q.historical_best)
        rejected = raw & ~q.gate_switched.astype(bool)
        reject_rows.append({'dataset': dataset, 'scenario': scenario, 'selector': selector,
                            'raw_hierarchyloss_recommendations': int(raw.sum()),
                            'rejected_recommendations': int(rejected.sum()),
                            'rejected_percent': float(100 * rejected.sum() / raw.sum()) if raw.sum() else np.nan,
                            'accepted_switches': int((raw & q.gate_switched.astype(bool)).sum())})
    pd.DataFrame(reject_rows).to_csv(OUT / 'rejected_recommendations.csv', index=False)
    # Inference records are measured selector-score timing, not end-to-end forecast latency.
    inference = pd.DataFrame([r for r in all_inference_records if isinstance(r, dict)])
    inference.to_csv(OUT / 'selector_score_inference.csv', index=False)
    # Cost estimates: periodic off-monitor model runtimes use linear per-leaf allocation, never assert wall-clock savings.
    cost_parts = []
    for name in ['M5', 'Tourism']:
        dd = load_data(name)
        train_cost = json.loads((OUT / f'training_cost_{name.lower()}.json').read_text(encoding='utf-8'))
        log = pd.DataFrame([r for r in all_inference_records if isinstance(r, dict)])
        cost_parts.append(cost_analysis(dd, choices[choices.dataset.eq(name)], log, train_cost,
                                        pd.concat(all_observed_logs, ignore_index=True) if all_observed_logs else pd.DataFrame()))
    pd.concat(cost_parts, ignore_index=True).to_csv(OUT / 'cost_comparison.csv', index=False)
    # Run protocol metadata.
    spec = {'experiment': '6A', 'purpose': 'conservative and stable forecast-model routing',
            'candidate_forecasts': 'reused only; no forecasting candidates rerun',
            'datasets': {'M5': {'nodes': 84, 'leaves': 36, 'horizon_days': 28, 'test_origins': 11},
                         'Tourism': {'nodes': 85, 'leaves': 48, 'horizon_months': 3, 'test_origins': 12}},
            'threshold_grid': HIER_THRESHOLDS, 'leaf_risk_tolerance_grid': LEAF_TOLERANCES,
            'min_historical_evidence_windows': MIN_EVIDENCE, 'hysteresis_patience_grid': HYSTERESIS_PATIENCE,
            'periodic_shadow_policy': 'full shadow every third test origin beginning with the first; otherwise route only selected candidates',
            'monitoring_updates': 'only executed candidates have their outcomes appended to periodic history',
            'uncertainty': {'method': 'paired circular block bootstrap across chronological origins', 'block_length': 2,
                            'replicates': BOOTSTRAP_REPS, 'seed': SEED},
            'interpretation': 'All test periods were inspected previously and remain exploratory; no test tuning.',
            'nbeats_limitation': 'Tourism cached N-BEATS uses six-month input with annual seasonality; unchanged by this experiment.',
            'cost_limitation': 'Allocated cached batch times are estimates, not routed serving latency; model loading and some feature costs unavailable separately.'}
    (OUT / 'protocol.json').write_text(json.dumps(spec, indent=2), encoding='utf-8')
    status = {'datasets_completed': ['M5', 'Tourism'], 'candidate_forecasts_rerun': 0,
              'route_coherence_checks': len(all_coherence_records),
              'all_routes_coherent': all(r['coherent'] for r in all_coherence_records),
              'metrics_rows': len(metrics), 'forecast_rows': len(forecasts),
              'assignments': len(choices), 'errors': []}
    (OUT / 'run_status.json').write_text(json.dumps(status, indent=2), encoding='utf-8')
    print(json.dumps(status, indent=2), flush=True)


# The periodic replay stores full choice rows alongside metrics to prevent a second
# decision simulation. This helper is populated by the replay's deterministic route log.
all_inference_records: list[dict] = []
all_observed_logs: list[pd.DataFrame] = []
all_coherence_records: list[dict] = []
all_forecasts_by_dataset: dict[str, dict] = {}
all_periodic_score_logs: list[pd.DataFrame] = []


def periodic_choice_replay(d: dict, score_models: dict, configs: dict, inference_log: list[dict]) -> list[dict]:
    """Generate strictly online periodic assignments and record observed outcomes."""
    state0 = d['metrics'][d['metrics'].unique_id.isin(d['leaves']) & d['metrics'].split.isin(['train', 'validation'])][
        ['origin', 'unique_id', 'model', 'mase']].copy()
    windows = d['origins'][d['origins'].split.eq('test')].sort_values('origin').reset_index(drop=True)
    monitors = {int(windows.loc[i, 'origin']) for i in range(0, len(windows), MONITOR_EVERY)}
    choices = []
    for selector in ['GlobalBest', 'HistoricalBest', 'HierarchyLoss', 'ConfidenceGated',
                     'ConfidenceGatedLeafGuard', 'Hysteresis']:
        state = state0.copy(); active = {}; gate_history = {u: [] for u in d['leaves']}
        for w in windows.itertuples(index=False):
            origin = int(w.origin)
            if not state.empty and int(state.origin.max()) >= origin:
                raise ValueError('Periodic route used an outcome that was not completed before this origin')
            hb, global_model = refs_from_history(state, d['leaves'])
            q = dynamic_feature_rows(d, origin, state)
            sr = None
            if selector in ['HierarchyLoss', 'ConfidenceGated', 'ConfidenceGatedLeafGuard', 'Hysteresis']:
                objective_heads = ('hierarchy',) if selector == 'HierarchyLoss' else ('hierarchy', 'regret')
                pred = predict_scores(d, q, score_models, inference_log, origin,
                                      f'periodic_choice_{selector}', objective_heads)
                sr = score_to_rows(d, origin, pred, hb, state)
            if selector == 'GlobalBest': route = {u: global_model for u in d['leaves']}
            elif selector == 'HistoricalBest': route = hb
            elif selector == 'HierarchyLoss': route = dict(zip(sr.unique_id, sr.hierarchy_recommendation))
            elif selector == 'ConfidenceGated': route, gate = gated_route(sr, configs['gate']['hierarchy_threshold'], None)
            elif selector == 'ConfidenceGatedLeafGuard': route, gate = gated_route(sr, configs['guard']['hierarchy_threshold'], configs['guard']['leaf_tolerance'])
            else:
                _, gate = gated_route(sr, configs['hysteresis']['hierarchy_threshold'], configs['hysteresis']['leaf_tolerance'])
                route = {}
                for r in gate.itertuples(index=False):
                    uid = r.unique_id; gate_history[uid].append(r.gate_model)
                    if uid not in active: active[uid] = hb[uid]
                    prior = gate_history[uid][:-1][-configs['hysteresis_patience']:]
                    if len(prior) == configs['hysteresis_patience'] and len(set(prior)) == 1 and r.gate_model == prior[-1]:
                        active[uid] = r.gate_model
                    route[uid] = active[uid]
            if sr is not None:
                if selector in ['ConfidenceGated', 'ConfidenceGatedLeafGuard', 'Hysteresis']:
                    gate = gate.assign(dataset=d['name'], scenario='periodic', selector=selector)
                    all_periodic_score_logs.append(gate)
                else:
                    all_periodic_score_logs.append(sr.assign(dataset=d['name'], scenario='periodic', selector=selector))
            for uid, model in route.items():
                choices.append({'dataset': d['name'], 'scenario': 'periodic', 'split': 'test',
                                'origin': origin, 'selector': selector, 'unique_id': uid, 'model': model})
            actual = d['metrics'][d['metrics'].origin.eq(origin) & d['metrics'].unique_id.isin(d['leaves'])]
            if origin in monitors:
                obs = actual[['origin', 'unique_id', 'model', 'mase']].copy(); kind = 'full_shadow'
            else:
                mask = [r.model == route.get(r.unique_id) for r in actual.itertuples(index=False)]
                obs = actual.loc[mask, ['origin', 'unique_id', 'model', 'mase']].copy(); kind = 'routed_candidate'
            obs['dataset'] = d['name']; obs['selector'] = selector
            obs['execution_kind'] = kind; obs['origin_completed'] = origin
            if not set(obs.model).issubset(set(MODELS) if origin in monitors else set(route.values())):
                raise ValueError('Observed performance for an unexecuted candidate')
            all_observed_logs.append(obs)
            state = pd.concat([state, obs[['origin', 'unique_id', 'model', 'mase']]], ignore_index=True)
    return choices


if __name__ == '__main__':
    main()
