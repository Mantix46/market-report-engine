"""Testy nowych funkcji data_fetching dla layoutu v2 — zamockowany yfinance."""

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import data_fetching  # noqa: E402


class _EmptyTicker:
    """Symulacja starego yfinance (0.2.x) / tickera GPW: brak atrybutów lub puste dane."""
    info = {}

    @property
    def recommendations_summary(self):
        raise AttributeError("recommendations_summary not available")

    @property
    def upgrades_downgrades(self):
        return pd.DataFrame()


class _RichTicker:
    """Symulacja nowego yfinance z pełnymi danymi rekomendacji."""
    info = {"targetMeanPrice": 130.0, "targetLowPrice": 90.0, "targetHighPrice": 175.0,
            "currentPrice": 100.0, "numberOfAnalystOpinions": 30}
    recommendations_summary = pd.DataFrame([
        {"period": "0m", "strongBuy": 10, "buy": 20, "hold": 5, "sell": 1, "strongSell": 0},
        {"period": "-1m", "strongBuy": 9, "buy": 21, "hold": 5, "sell": 1, "strongSell": 0},
    ])
    analyst_price_targets = {"low": 90.0, "high": 175.0, "mean": 130.0,
                             "median": 128.0, "current": 100.0}
    upgrades_downgrades = pd.DataFrame(
        {"Firm": ["MS"], "Action": ["up"], "FromGrade": ["Hold"], "ToGrade": ["Buy"]},
        index=pd.DatetimeIndex([pd.Timestamp("2099-01-01")]),  # zawsze w oknie 30 dni
    )


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(data_fetching.time, "sleep", lambda *_: None)


def test_analyst_recs_tolerates_empty_and_missing(monkeypatch):
    monkeypatch.setattr(data_fetching.yf, "Ticker", lambda t: _EmptyTicker())
    out = data_fetching.fetch_analyst_recommendations("XTB.WA")
    assert out == {"rec_summary": None, "price_targets": None, "recent_changes": []}


def test_analyst_recs_full_data(monkeypatch):
    monkeypatch.setattr(data_fetching.yf, "Ticker", lambda t: _RichTicker())
    monkeypatch.setattr(
        data_fetching, "warsaw_today",
        lambda: pd.Timestamp("2099-01-15").date(),
    )
    out = data_fetching.fetch_analyst_recommendations("MU")
    assert out["rec_summary"]["strong_buy"] == 10
    assert out["rec_summary"]["buy"] == 20
    assert out["price_targets"]["mean"] == 130.0
    assert len(out["recent_changes"]) == 1
    assert out["recent_changes"][0]["to_grade"] == "Buy"


def test_analyst_recs_ticker_constructor_failure(monkeypatch):
    def boom(t):
        raise RuntimeError("network down")
    monkeypatch.setattr(data_fetching.yf, "Ticker", boom)
    out = data_fetching.fetch_analyst_recommendations("MU")
    assert out == {"rec_summary": None, "price_targets": None, "recent_changes": []}


class _FundamentalsTicker:
    """Ticker do testu rozszerzonej wyceny: ujemna EBITDA -> debt_to_ebitda = None."""
    def __init__(self, ebitda):
        self.info = {
            "forwardPE": 12.0, "trailingPE": 15.0, "enterpriseToEbitda": 8.0,
            "priceToBook": 3.0, "returnOnEquity": 0.2, "profitMargins": 0.1,
            "operatingMargins": 0.15, "trailingPegRatio": 1.1,
            "totalDebt": 1000.0, "ebitda": ebitda,
            "freeCashflow": 50.0, "marketCap": 1000.0,
            "fiftyTwoWeekHigh": 120.0, "fiftyTwoWeekLow": 60.0,
        }
        self.fast_info = {"lastPrice": 100.0}
        self.insider_transactions = pd.DataFrame()


def test_fundamentals_negative_ebitda_gives_none(monkeypatch):
    monkeypatch.setattr(data_fetching.yf, "Ticker", lambda t: _FundamentalsTicker(ebitda=-500.0))
    out = data_fetching.fetch_fundamentals_short_insider("MU")
    assert out["debt_to_ebitda"] is None
    assert out["fcf_yield_pct"] == 5.0
    assert out["ev_to_ebitda"] == 8.0
    assert out["peg_ratio"] == 1.1


def test_fundamentals_positive_ebitda(monkeypatch):
    monkeypatch.setattr(data_fetching.yf, "Ticker", lambda t: _FundamentalsTicker(ebitda=500.0))
    out = data_fetching.fetch_fundamentals_short_insider("MU")
    assert out["debt_to_ebitda"] == 2.0


def test_fundamentals_exception_fallback_has_v2_keys(monkeypatch):
    def boom(t):
        raise RuntimeError("network down")
    monkeypatch.setattr(data_fetching.yf, "Ticker", boom)
    out = data_fetching.fetch_fundamentals_short_insider("MU")
    for key in ("trailing_pe", "peg_ratio", "ev_to_ebitda", "price_to_book",
                "return_on_equity", "profit_margin", "operating_margin",
                "debt_to_ebitda", "fcf_yield_pct"):
        assert key in out and out[key] is None


def test_get_index_tickers_v2_fallback_to_epol(monkeypatch):
    monkeypatch.setattr(
        data_fetching, "fetch_quote_cached",
        lambda t, period="5d": {"error": "fail", "price": None},
    )
    idx = data_fetching.get_index_tickers_v2()
    assert idx["Euro Stoxx 50"] == "^STOXX50E"
    assert idx["WIG20 (Proxy EPOL)"] == "EPOL"


def test_get_index_tickers_v2_real_wig20(monkeypatch):
    monkeypatch.setattr(
        data_fetching, "fetch_quote_cached",
        lambda t, period="5d": {"error": None, "price": 2600.0},
    )
    monkeypatch.setattr(data_fetching, "fetch_period_change", lambda t, period="7d": 1.5)
    idx = data_fetching.get_index_tickers_v2()
    assert idx["WIG20"] == "WIG20.WA"  # pierwszy kandydat wygrywa


def test_get_index_tickers_v2_single_row_history_falls_back(monkeypatch):
    """Cena jest, ale historia 1-wierszowa (WIG20.WA) -> proxy EPOL."""
    monkeypatch.setattr(
        data_fetching, "fetch_quote_cached",
        lambda t, period="5d": {"error": None, "price": 3666.0},
    )
    monkeypatch.setattr(data_fetching, "fetch_period_change", lambda t, period="7d": None)
    idx = data_fetching.get_index_tickers_v2()
    assert idx["WIG20 (Proxy EPOL)"] == "EPOL"
