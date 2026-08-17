"""
Adaptive cost forecasting: fits every model the available history can
support, backtests each with one-step-ahead walk-forward validation, and
returns whichever generalized best on this specific series — not a fixed
formula, and not the LLM freehand-picking a model name.
"""
import json
import statistics
from dataclasses import dataclass
from typing import Callable

import numpy as np
from langchain_core.tools import tool
from statsmodels.tsa.holtwinters import Holt, SimpleExpSmoothing

_MIN_BACKTEST_TRAIN = 2


def _fit_naive(train: list[float]) -> float:
    return statistics.mean(train)


def _forecast_naive(model: float, horizon: int) -> list[float]:
    return [model] * horizon


def _fit_linear(train: list[float]) -> tuple[float, float, int]:
    x = np.arange(len(train))
    slope, intercept = np.polyfit(x, train, 1)
    return (slope, intercept, len(train))


def _forecast_linear(model: tuple[float, float, int], horizon: int) -> list[float]:
    slope, intercept, n = model
    x = np.arange(n, n + horizon)
    return (slope * x + intercept).tolist()


def _fit_ses(train: list[float]):
    return SimpleExpSmoothing(
        np.asarray(train, dtype=float), initialization_method="estimated"
    ).fit()


def _fit_holt(train: list[float]):
    return Holt(
        np.asarray(train, dtype=float), initialization_method="estimated"
    ).fit()


def _forecast_statsmodels(model, horizon: int) -> list[float]:
    return model.forecast(horizon).tolist()


@dataclass(frozen=True)
class _Candidate:
    name: str
    min_history: int
    fit: Callable[[list[float]], object]
    forecast: Callable[[object, int], list[float]]


# min_history gates whether a candidate is even considered for a given
# series length — a judgment call about how much data each model honestly
# needs, not just "try it and see if it crashes".
_CANDIDATES = (
    _Candidate("naive_average", 3, _fit_naive, _forecast_naive),
    _Candidate("linear_trend", 3, _fit_linear, _forecast_linear),
    _Candidate("simple_exponential_smoothing", 4, _fit_ses, _forecast_statsmodels),
    _Candidate("holt_linear_trend", 8, _fit_holt, _forecast_statsmodels),
)


def _backtest(candidate: _Candidate, series: list[float]) -> tuple[float, int] | None:
    """One-step-ahead walk-forward MAE. Refits on each growing window, so
    the score reflects how the model would actually have performed as data
    arrived — not an in-sample fit that flatters complex models. Returns
    None if every fold fails (e.g. a degenerate window statsmodels can't
    optimize), so that candidate is excluded rather than crashing the tool.
    """
    start = max(_MIN_BACKTEST_TRAIN, candidate.min_history - 1)
    errors = []
    for i in range(start, len(series)):
        train, actual = series[:i], series[i]
        try:
            predicted = candidate.forecast(candidate.fit(train), 1)[0]
        except Exception:
            continue
        errors.append(abs(actual - predicted))
    if not errors:
        return None
    return statistics.mean(errors), len(errors)


def _evaluate_all(series: list[float]) -> list[dict]:
    """Score every candidate against this series — including the ones that
    weren't even eligible — so the full picture is available, not just
    whichever one won. This is what makes the tool's output answer "what
    happened", not just "what's the answer".
    """
    results = []
    for c in _CANDIDATES:
        if len(series) < c.min_history:
            results.append({
                "model": c.name,
                "applicable": False,
                "reason": f"needs >= {c.min_history} months of history, have {len(series)}",
            })
            continue
        outcome = _backtest(c, series)
        if outcome is None:
            results.append({
                "model": c.name,
                "applicable": True,
                "reason": "every backtest fold failed to fit (unstable on this data)",
            })
            continue
        mae, folds = outcome
        results.append({
            "model": c.name,
            "applicable": True,
            "backtest_mae": round(mae, 2),
            "backtest_folds": folds,
        })
    return results


def _select_winner(evaluations: list[dict]) -> dict:
    scored = [e for e in evaluations if "backtest_mae" in e]
    # naive_average always scores (statistics.mean never raises on a
    # non-empty list), so `scored` is never empty when series is non-empty.
    return min(scored, key=lambda e: e["backtest_mae"])


@tool
def forecast_costs(monthly_costs: str, periods_ahead: int = 6) -> str:
    """
    Forecast future monthly costs from historical monthly totals.

    Fits every model the history can support (naive average, linear trend,
    simple exponential smoothing, Holt's linear trend — each gated by how
    much history is available), backtests each with one-step-ahead
    walk-forward validation, and returns whichever had the lowest backtest
    error. No seasonal modeling — monthly cost history rarely spans enough
    years for a model to learn a real seasonal pattern honestly; claiming
    one would be false precision.

    Args:
        monthly_costs: JSON array of historical monthly totals, oldest
            first, e.g. "[6156.09, 11899.17, 11283.01, 9439.29, 10459.25]".
            Extract these from a call_aws_api("ce", "get_cost_and_usage")
            result first — this tool takes plain numbers, not the raw
            Cost Explorer response.
        periods_ahead: how many future months to forecast (default 6)

    Returns:
        JSON string: {"model": "...", "backtest_mae": ..., "backtest_folds": ...,
        "forecast": [...], "history_months_used": ..., "candidates": [...], "note": "..."}
        `candidates` lists every model considered, including ones not applicable
        to this much history (with why) — always report this comparison when
        presenting a forecast, not just the winning model.
        On failure: {"error": "..."}
    """
    try:
        series = json.loads(monthly_costs)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in monthly_costs: {e}"})

    if not isinstance(series, list) or not all(isinstance(v, (int, float)) for v in series):
        return json.dumps({"error": "monthly_costs must be a JSON array of numbers"})

    if len(series) < 3:
        return json.dumps({
            "error": f"Need at least 3 months of history to forecast, got {len(series)}."
        })

    if periods_ahead < 1:
        return json.dumps({"error": "periods_ahead must be at least 1"})

    evaluations = _evaluate_all(series)
    winner = _select_winner(evaluations)
    candidate = next(c for c in _CANDIDATES if c.name == winner["model"])
    forecast = candidate.forecast(candidate.fit(series), periods_ahead)

    # Printed (not `logging`) to match this codebase's existing convention
    # for runtime diagnostics — see deployment.py's startup checks. Shows up
    # in the terminal locally, or `journalctl -u cost-advisor` when deployed.
    print(
        f"[forecast_costs] history={len(series)}mo periods_ahead={periods_ahead} "
        f"candidates={evaluations} selected={winner['model']}",
        flush=True,
    )

    return json.dumps({
        "model": winner["model"],
        "backtest_mae": winner["backtest_mae"],
        "backtest_folds": winner["backtest_folds"],
        "forecast": [round(v, 2) for v in forecast],
        "history_months_used": len(series),
        "candidates": evaluations,
        "note": (
            f"Selected {winner['model']} — lowest one-step-ahead backtest error "
            f"(${winner['backtest_mae']:.2f} MAE over {winner['backtest_folds']} "
            "fold(s)). See 'candidates' for every model considered, including any "
            "not applicable to this much history and why. No seasonal component — "
            "insufficient history to fit one honestly."
        ),
    })
