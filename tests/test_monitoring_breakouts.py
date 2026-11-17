import os
import sys
from datetime import datetime
from unittest.mock import Mock

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import layout_v2
import technicals


def _history_prices(length=60):
    values = [99.5, 100.5] * (length // 2)
    return pd.Series(values, index=pd.bdate_range("2026-07-01", periods=length))


@pytest.mark.parametrize(
    ("prior_price", "outside", "signal"),
    [
        (99.5, 104, "Wybicie ponad górną wstęgę Bollingera"),
        (100.5, 96, "Spadek pod dolną wstęgę Bollingera"),
    ],
)
def test_bollinger_requires_fresh_breakout(prior_price, outside, signal):
    prices = _history_prices()
    prices.iloc[-3:] = [prior_price, outside, outside]
    dates = [item["date"] for item in technicals._price_breakouts(prices) if item["signal"] == signal]
    assert dates == [prices.index[-2].date().isoformat()]


def test_sma50_requires_closes_on_opposite_sides_of_average():
    prices = pd.Series([100.0] * 60, index=pd.bdate_range("2026-07-01", periods=60))
    prices.iloc[-2:] = [98, (48 * 100 + 98) / 49]  # Ostatnie zamknięcie jest równe SMA50.
    assert all("SMA50" not in item["signal"] for item in technicals._price_breakouts(prices))
    prices.iloc[-1] = 99.961
    assert technicals._price_breakouts(prices)[0] == {
        "date": prices.index[-1].date().isoformat(), "signal": "Wybicie ponad SMA50",
    }


def test_synthetic_last_price_cannot_confirm_breakout(monkeypatch):
    prices = _history_prices()
    prices.iloc[-2:] = [99.5, float("nan")]
    history = pd.DataFrame({"Close": prices, "Open": prices, "High": prices, "Low": prices, "Volume": 100})
    ticker = Mock(fast_info={"lastPrice": 104, "tenDayAverageVolume": 100})
    ticker.history.return_value = history
    monkeypatch.setattr(technicals.yf, "Ticker", lambda _: ticker)

    tech = technicals.fetch_technicals("SAMPLE")

    assert tech["bollinger_signal"] == "above_upper_band"
    assert not any(item["date"] == tech["data_date"] for item in tech["price_breakouts"])


def test_monitoring_uses_verified_breakouts_for_reporting_period():
    data = {"now": datetime(2026, 7, 11, 7, 30)}
    quote = {"data_date": "2026-07-10"}
    technicals = {
        **quote,
        "price_breakouts": [
            {"date": "2026-07-10", "signal": "Wybicie ponad SMA50"},
            {"date": "2026-07-08", "signal": "Spadek pod dolną wstęgę Bollingera"},
        ],
    }
    signals = layout_v2._monitoring_breakouts
    assert signals(data, quote, technicals) == ["2026-07-10 Wybicie ponad SMA50"]
    data["is_saturday"] = True
    assert len(signals(data, quote, technicals)) == 2
    assert signals(data, {**quote, "is_stale": True}, technicals) == []
    assert signals(data, {**quote, "data_date": "2026-07-09"}, technicals) == []
    data["now"] = datetime(2026, 7, 10, 15)
    assert signals(data, quote, technicals) == ["2026-07-08 Spadek pod dolną wstęgę Bollingera"]


def test_monitoring_fallback_surfaces_company_facts_and_estimates():
    data = {
        "now": datetime(2026, 7, 11, 7, 30),
        "is_saturday": True,
        "active_tickers": ["MU"],
        "portfolio_details": {
            "MU": {
                "quote": {"price": 100, "change_pct": -3.2, "data_date": "2026-07-10"},
                "technicals": {"rsi_14": 27, "change_5d": 2.3, "price_vs_sma50": -2.1},
                "fundamentals": {"operating_margin": 0.21, "debt_to_ebitda": 0.75},
            },
        },
        "earnings_dates": {"MU": {"date": "2026-07-18", "is_range": True}},
        "last_snapshots": {"MU": {"price_vs_sma50": 1.1}},
        "v2": {"analyst_recs": {"MU": {"price_targets": {"mean": 120}}}},
    }

    report = layout_v2._basic_monitoring(data)
    assert "5 sesji: +2.30%" in report
    assert "marża operacyjna" not in report and "EBITDA" not in report
    assert "szacunki analityków" in report
    assert "Szacowany termin wyników wg Yahoo Finance · od 2026-07-18" in report
    assert "SMA50" not in report and "RSI" not in report
    assert "SMA50" in layout_v2._company_data_block(data)
    assert "potwierdzone wybicie:" not in layout_v2._company_data_block(data)
