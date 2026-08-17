import json

import pytest

from tools.forecast_tools import forecast_costs


def run(monthly_costs: list, periods_ahead: int = 6) -> dict:
    return json.loads(
        forecast_costs.invoke({
            "monthly_costs": json.dumps(monthly_costs),
            "periods_ahead": periods_ahead,
        })
    )


def should_error_when_history_is_too_short():
    result = run([100, 200])

    assert "error" in result
    assert "at least 3 months" in result["error"]


def should_error_when_monthly_costs_is_not_valid_json():
    result = json.loads(
        forecast_costs.invoke({"monthly_costs": "{not json", "periods_ahead": 3})
    )

    assert "Invalid JSON" in result["error"]


def should_error_when_monthly_costs_is_not_a_list():
    result = json.loads(
        forecast_costs.invoke({"monthly_costs": "42", "periods_ahead": 3})
    )

    assert "error" in result


def should_error_when_monthly_costs_contains_non_numbers():
    result = json.loads(
        forecast_costs.invoke({"monthly_costs": '[100, "oops", 200]', "periods_ahead": 3})
    )

    assert "error" in result


def should_error_when_periods_ahead_is_less_than_one():
    result = run([100, 200, 300], periods_ahead=0)

    assert "error" in result


def should_return_requested_number_of_periods():
    result = run([100, 110, 120, 130, 140])

    assert len(result["forecast"]) == 6


def should_return_fewer_periods_when_asked():
    result = run([100, 110, 120], periods_ahead=2)

    assert len(result["forecast"]) == 2


def should_report_which_model_was_selected():
    result = run([100, 110, 120, 130])

    assert result["model"] in {
        "naive_average", "linear_trend", "simple_exponential_smoothing", "holt_linear_trend"
    }


def should_report_backtest_error_and_fold_count():
    result = run([100, 110, 120, 130])

    assert isinstance(result["backtest_mae"], float)
    assert result["backtest_folds"] >= 1


def should_report_history_months_used():
    result = run([100, 110, 120, 130, 140])

    assert result["history_months_used"] == 5


def should_not_claim_seasonality_in_the_note():
    result = run([100, 110, 120, 130])

    assert "insufficient history" in result["note"].lower()


def should_pick_trend_aware_model_when_data_has_a_clear_linear_trend():
    trending = [1000, 1200, 1400, 1600, 1800, 2000, 2200, 2400]

    result = run(trending, periods_ahead=3)

    assert result["model"] in {"linear_trend", "holt_linear_trend"}
    # Forecast should keep climbing, not flatten out like a plain average would.
    assert result["forecast"][0] > trending[-1]
    assert result["forecast"][-1] > result["forecast"][0]


def should_only_consider_naive_and_linear_when_history_has_exactly_three_months():
    # simple_exponential_smoothing needs >=4 months, holt needs >=8 — with
    # exactly 3, only naive_average and linear_trend are even applicable.
    result = run([100, 200, 300])

    assert result["model"] in {"naive_average", "linear_trend"}


def should_forecast_a_roughly_flat_value_when_history_has_no_trend():
    noisy_flat = [6156.09, 11899.17, 11283.01, 9439.29, 10459.25]

    result = run(noisy_flat, periods_ahead=3)

    forecast = result["forecast"]
    # No claim of a strong trend either way — every forecasted month should
    # land within the historical range, not extrapolate a spike or crash.
    assert min(noisy_flat) - 1000 <= min(forecast)
    assert max(forecast) <= max(noisy_flat) + 1000


def should_not_raise_when_history_is_a_constant_series():
    result = run([500.0, 500.0, 500.0, 500.0])

    assert "error" not in result
    assert all(v == pytest.approx(500.0, abs=1.0) for v in result["forecast"])


def should_list_every_candidate_model_in_the_output():
    result = run([100, 110, 120, 130, 140])

    names = {c["model"] for c in result["candidates"]}
    assert names == {
        "naive_average", "linear_trend", "simple_exponential_smoothing", "holt_linear_trend"
    }


def should_mark_holt_as_not_applicable_when_history_is_under_eight_months():
    result = run([100, 110, 120, 130, 140])  # 5 months, holt needs >= 8

    holt = next(c for c in result["candidates"] if c["model"] == "holt_linear_trend")
    assert holt["applicable"] is False
    assert "8" in holt["reason"]


def should_mark_holt_as_applicable_when_history_has_eight_or_more_months():
    result = run([100, 110, 120, 130, 140, 150, 160, 170])  # exactly 8 months

    holt = next(c for c in result["candidates"] if c["model"] == "holt_linear_trend")
    assert holt["applicable"] is True
    assert "backtest_mae" in holt


def should_give_applicable_candidates_a_backtest_score():
    result = run([100, 110, 120, 130, 140])

    applicable = [c for c in result["candidates"] if c["applicable"]]
    assert len(applicable) >= 2  # naive_average and linear_trend at minimum
    assert all("backtest_mae" in c and "backtest_folds" in c for c in applicable)
