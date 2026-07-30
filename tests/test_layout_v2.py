"""Testy deterministycznych sekcji layoutu v2 — fixture bez sieci."""

import os
import sys
import threading
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import layout_v2  # noqa: E402


@pytest.fixture
def sample_data():
    """Minimalny ReportData + klucz 'v2', wystarczający dla sekcji deterministycznych."""
    quote = {"price": 100.0, "change_pct": 1.5, "volume": 1000, "error": None,
             "data_date": "2026-07-07", "is_stale": False}
    return {
        "now": datetime(2026, 7, 8, 7, 30),
        "skip": set(),
        "status": {"US": {"open": True}, "PL": {"open": True}},
        "us_active": True,
        "pl_active": True,
        "active_tickers": ["MU", "XTB.WA"],
        "portfolio_details": {
            "MU": {
                "quote": dict(quote),
                "technicals": {"rsi_14": 55.0, "price_vs_sma20": 1.0, "price_vs_sma50": 2.0,
                               "macd_trend": "bullish", "bollinger_signal": "neutral",
                               "volume_ratio_10d": 1.8, "macd_histogram": 0.5,
                               "bollinger_position": 0.6},
                "fundamentals": {"forward_pe": 12.5, "trailing_pe": None, "ev_to_ebitda": 8.1,
                                 "peg_ratio": 1.2, "price_to_book": 3.3, "fcf_yield_pct": 4.56,
                                 "return_on_equity": 0.23, "debt_to_ebitda": 0.8,
                                 "operating_margin": 0.31, "short_pct_float": 0.15,
                                 "insider_signal": "neutral", "insider_summary": {}},
            },
            "XTB.WA": {
                "quote": {**quote, "price": 70.0, "change_pct": -2.5},
                "technicals": {},
                "fundamentals": {},
            },
        },
        "macro_quotes": {
            "VIX": {"price": 14.2, "change_pct": -0.5, "error": None},
            "US 10Y Treasury": {"price": 4.1, "change_pct": 0.2, "error": None},
        },
        "macro_calendar": [
            {"date": "2026-07-08", "country": "PL", "event": "Decyzja RPP",
             "impact": "high", "source": "schedule"},
            {"date": "2026-07-10", "country": "US", "event": "CPI (inflacja US)",
             "impact": "high", "source": "fred"},
        ],
        "earnings_dates": {"MU": {"date": "2026-07-12", "days_until": 4, "is_range": False}},
        "today_movers": {
            "us_winners": [("NVDA", {"price": 200.0, "change_pct": 5.0})],
            "us_losers": [("INTC", {"price": 30.0, "change_pct": -4.0})],
            "gpw_winners": [("KGH.WA", {"price": 150.0, "change_pct": 3.0})],
            "gpw_losers": [],
            "sc_winners": [("AIXA.DE", {"price": 25.0, "change_pct": 6.0})],
            "sc_losers": [],
        },
        "news_data": {"MU": [{"publisher": "Reuters", "title": "Micron beats estimates",
                              "url": "https://example.com/mu"}]},
        "last_snapshots": {},
        "v2": {
            "index_tickers": {"S&P 500": "^GSPC", "WIG20": "WIG20.WA"},
            "index_quotes": {
                "S&P 500": {"price": 6100.0, "change_pct": 0.8, "error": None},
                "WIG20": {"price": 2600.0, "change_pct": None, "error": "fail"},
            },
            "index_periods": {
                "S&P 500": {"weekly": 1.2, "ytd": 15.3},
                "WIG20": {"weekly": None, "ytd": None},
            },
            "analyst_recs": {
                "MU": {"rec_summary": {"strong_buy": 10, "buy": 20, "hold": 5},
                       "price_targets": {"mean": 130.0, "low": 90.0, "high": 175.0},
                       "recent_changes": [{"date": "2026-07-01", "firm": "MS",
                                           "action": "up", "from_grade": "Hold",
                                           "to_grade": "Buy"}]},
                "XTB.WA": {"rec_summary": None, "price_targets": None, "recent_changes": []},
            },
        },
    }


def test_benchmarks_table(sample_data):
    md = layout_v2.build_benchmarks_md(sample_data)
    assert md.startswith("## 2. Benchmarki rynkowe")
    assert "| **S&P 500** | 6,100.00 |" in md
    assert "**+1.20%**" in md and "**+15.30%**" in md
    # indeks z błędem -> cały wiersz b/d
    assert "| **WIG20** | b/d | b/d | b/d | b/d |" in md


def test_macro_section_emoji_and_today_prefix(sample_data):
    md = layout_v2.build_macro_md(sample_data)
    assert md.startswith("## 3. Makroekonomia")
    assert "🔴" in md
    assert "**DZIŚ** **2026-07-08**" in md
    assert "VIX | 14.20" in md


def test_basic_macro_top5(sample_data):
    md = layout_v2._basic_macro_top5(sample_data)
    assert md.startswith("## 5. Top 5 wydarzeń makro")
    assert "🔴" in md
    assert "**DZIŚ** **2026-07-08**" in md  # prefiks dla dzisiejszej daty
    # max 5 pozycji
    assert sum(1 for line in md.splitlines() if line.startswith("- ")) <= 5


def test_basic_macro_top5_high_impact_first(sample_data):
    sample_data["macro_calendar"] = [
        {"date": "2026-07-09", "country": "US", "event": "Niska waga",
         "impact": "low", "source": "fred"},
        {"date": "2026-07-10", "country": "US", "event": "CPI",
         "impact": "high", "source": "fred"},
    ]
    md = layout_v2._basic_macro_top5(sample_data)
    bullets = [line for line in md.splitlines() if line.startswith("- ")]
    assert "CPI" in bullets[0]  # high-impact przed low mimo późniejszej daty


def test_macro_news_block_excludes_portfolio(sample_data):
    sample_data["news_data"]["^GSPC"] = [
        {"publisher": "Bloomberg", "title": "Fed signals pause", "url": "https://example.com/fed"}
    ]
    block = layout_v2._macro_news_block(sample_data)
    assert "Fed signals pause" in block
    assert "Micron beats estimates" not in block  # MU jest w portfelu


def test_movers_section(sample_data):
    md = layout_v2.build_movers_md(sample_data)
    assert md.startswith("## 6. Radar rynkowy -- Największe ruchy dnia")
    assert "### USA -- Top movers" in md
    assert "### GPW -- Top movers" in md
    assert "### Sektor AI Bottlenecks (Small/Mid-Caps) -- Top movers" in md
    assert "| Spółka | Kurs | Zmiana |" in md
    assert "$200.00" in md and "+5.00%" in md
    assert "150.00 PLN" in md
    assert "| 25.00 |" in md  # ticker EU bez znaku $


def test_valuation_table(sample_data):
    md = layout_v2.build_valuation_md(sample_data)
    assert "## 8. Wycena i technika" in md
    assert "| **MU** | 12.50 | b/d | 8.10 | 1.20 | 3.30 | 4.6% | 23.0% | 0.80 | 31.0% |" in md
    # spółka bez fundamentów -> b/d wszędzie, bez wyjątku
    assert "| **XTB.WA** | b/d |" in md


def test_parse_ai_sections_roundtrip():
    text = "\n".join(f"{h}\ntreść {i}" for i, h in enumerate(layout_v2.V2_AI_HEADERS))
    sections = layout_v2._parse_ai_sections(text)
    assert set(sections) == set(layout_v2.V2_AI_HEADERS)
    assert sections["## 7. Ryzyka"].startswith("## 7. Ryzyka")


def test_parse_ai_sections_missing_header_raises():
    text = "## 1. Executive Summary\ncoś\n## 7. Ryzyka\ncoś"
    with pytest.raises(ValueError, match="brak wymaganej sekcji"):
        layout_v2._parse_ai_sections(text)


def test_basic_report_has_all_ten_sections(sample_data):
    md = layout_v2.render_basic_report_v2(sample_data)
    for i in range(1, 11):
        assert f"## {i}." in md, f"Brak sekcji {i} w raporcie regułowym"
    assert md.startswith("# RAPORT RYNKOWY -- ")
    assert "Disclaimer" in md
    # ocena wpływu w monitoringu spółek
    assert "**Wpływ: " in md
    assert "Top 5 wydarzeń makro" in md
    assert "Radar rynkowy" in md


def test_parse_ai_sections_extended_headers():
    headers = layout_v2.V2_AI_HEADERS + [layout_v2.PREDICTIONS_HEADER]
    text = "\n".join(f"{h}\ntreść" for h in headers)
    sections = layout_v2._parse_ai_sections(text, headers)
    assert layout_v2.PREDICTIONS_HEADER in sections
    with pytest.raises(ValueError):  # bez sekcji prognoz przy rozszerzonej liście
        layout_v2._parse_ai_sections("\n".join(f"{h}\nx" for h in layout_v2.V2_AI_HEADERS), headers)


def test_saturday_basic_report(sample_data, monkeypatch):
    """Sobota: raport regułowy zawiera analizę trendu, trafność i parsowalne prognozy."""
    sample_data["is_saturday"] = True
    sample_data["persist"] = False
    monkeypatch.setattr(layout_v2, "evaluate_previous_predictions", lambda dry_run: {"dry": dry_run})
    monkeypatch.setattr(
        layout_v2, "format_accuracy_section",
        lambda result: "### Trafnosc prognoz tygodniowych\n\n*mock*\n",
    )
    monkeypatch.setattr(
        layout_v2, "build_rule_based_predictions",
        lambda details: [{"ticker": "MU", "direction": "up", "horizon_days": 5,
                          "thesis": "RSI 55 przy trendzie wzrostowym"}],
    )
    saved = []
    monkeypatch.setattr(layout_v2, "save_predictions", lambda preds: saved.append(preds))

    md = layout_v2.render_basic_report_v2(sample_data)
    assert "## Analiza trendu (tygodniowa)" in md
    assert "Trafnosc prognoz tygodniowych" in md
    assert "## Prognozy do weryfikacji" in md
    assert not saved  # persist=False -> brak zapisu prognoz

    # format bulletów musi łapać prawdziwy parser accuracy_trackera
    from accuracy_tracker import extract_predictions_from_report
    parsed = extract_predictions_from_report(md, ["MU", "XTB.WA"], {"MU": 100.0})
    assert len(parsed) == 1 and parsed[0]["ticker"] == "MU" and parsed[0]["direction"] == "up"


def test_saturday_basic_report_persist_saves(sample_data, monkeypatch):
    sample_data["is_saturday"] = True
    sample_data["persist"] = True
    monkeypatch.setattr(layout_v2, "evaluate_previous_predictions", lambda dry_run: {})
    monkeypatch.setattr(layout_v2, "format_accuracy_section", lambda result: "*mock*")
    preds = [{"ticker": "MU", "direction": "up", "horizon_days": 5, "thesis": "t"}]
    monkeypatch.setattr(layout_v2, "build_rule_based_predictions", lambda details: preds)
    saved = []
    monkeypatch.setattr(layout_v2, "save_predictions", lambda p: saved.append(p))
    layout_v2.render_basic_report_v2(sample_data)
    assert saved == [preds]


def test_non_saturday_has_no_saturday_sections(sample_data):
    md = layout_v2.render_basic_report_v2(sample_data)
    assert "Analiza trendu" not in md
    assert "Prognozy do weryfikacji" not in md


def test_basic_company_impact_rules():
    assert layout_v2._basic_company_impact(
        {"change_pct": 3.0}, {"volume_ratio_10d": 1.5}) == "pozytywny"
    assert layout_v2._basic_company_impact(
        {"change_pct": -3.0}, {}) == "negatywny"
    assert layout_v2._basic_company_impact(
        {"change_pct": 0.5}, {"volume_ratio_10d": 1.0}) == "neutralny"


# ---- Łańcuch modeli Gemini (_call_gemini_v2) ----

_DAILY_QUOTA_MSG = (
    "429 RESOURCE_EXHAUSTED. Quota exceeded for metric "
    "GenerateRequestsPerDayPerProjectPerModel-FreeTier, limit: 20"
)
_OVERLOAD_MSG = "503 UNAVAILABLE. This model is currently experiencing high demand."


class _FakeModels:
    def __init__(self, script, calls):
        self.script = script  # {model_name: [str|Exception, ...]} kolejno per wywołanie
        self.calls = calls

    def generate_content(self, model, contents):
        self.calls.append(model)
        outcomes = self.script.get(model, [])
        idx = sum(1 for c in self.calls if c == model) - 1
        outcome = outcomes[idx] if idx < len(outcomes) else Exception(_OVERLOAD_MSG)
        if isinstance(outcome, Exception):
            raise outcome
        return type("R", (), {"text": outcome})()


class _FakeGenai:
    def __init__(self, script, calls):
        self._script, self._calls = script, calls
        self.http_options = None

    def Client(self, api_key, http_options=None):
        self.http_options = http_options
        models = _FakeModels(self._script, self._calls)
        return type("C", (), {"models": models})()


@pytest.fixture
def gemini_env(monkeypatch):
    """Fabryka fake-genai + zerowy sleep; zwraca (install_script, calls, sleeps)."""
    calls, sleeps = [], []
    monkeypatch.setattr(layout_v2.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.delenv("GEMINI_MODEL", raising=False)

    def install(script):
        monkeypatch.setattr(layout_v2, "genai", _FakeGenai(script, calls))
        return calls, sleeps
    return install


def test_gemini_chain_falls_through_on_503(gemini_env):
    calls, sleeps = gemini_env({
        "gemini-3.5-flash": [Exception(_OVERLOAD_MSG), Exception(_OVERLOAD_MSG)],
        "gemini-3-flash-preview": ["RAPORT Z 3-FLASH"],
    })
    result = layout_v2._call_gemini_v2("prompt", "key")
    assert result == "RAPORT Z 3-FLASH"
    # dwie próby modelu podstawowego, potem sukces zapasowego
    assert calls == ["gemini-3.5-flash", "gemini-3.5-flash", "gemini-3-flash-preview"]
    assert 10 in sleeps  # backoff między próbami modelu podstawowego


def test_gemini_total_timeout_is_three_minutes(gemini_env):
    gemini_env({"gemini-3.5-flash": ["RAPORT"]})

    layout_v2._call_gemini_v2("prompt", "key")

    assert 179_000 <= layout_v2.genai.http_options.timeout <= 180_000


def test_gemini_hard_timeout_returns_control(monkeypatch):
    release_request = threading.Event()

    class BlockingModels:
        def generate_content(self, model, contents):
            release_request.wait()
            return type("R", (), {"text": "ZA PÓŹNO"})()

    class BlockingGenai:
        def Client(self, api_key, http_options=None):
            return type("C", (), {"models": BlockingModels()})()

    monkeypatch.setattr(layout_v2, "genai", BlockingGenai())
    try:
        with pytest.raises(TimeoutError, match="Przekroczono .*limit"):
            layout_v2._generate_content_with_timeout(
                "prompt",
                "key",
                "model",
                timeout_seconds=0.01,
            )
    finally:
        release_request.set()


def test_gemini_chain_skips_daily_quota_fast(gemini_env):
    calls, sleeps = gemini_env({
        "gemini-3.5-flash": [Exception(_DAILY_QUOTA_MSG)],
        "gemini-3-flash-preview": [Exception(_DAILY_QUOTA_MSG)],
        "gemini-2.5-flash": ["RAPORT Z 2.5"],
    })
    result = layout_v2._call_gemini_v2("prompt", "key")
    assert result == "RAPORT Z 2.5"
    # każdy wyczerpany model wołany raz (break bez drugiej próby), zero długiego sleepu
    assert calls == ["gemini-3.5-flash", "gemini-3-flash-preview", "gemini-2.5-flash"]
    assert sleeps == []


def test_gemini_chain_all_fail_raises(gemini_env):
    gemini_env({
        "gemini-3.5-flash": [Exception(_OVERLOAD_MSG), Exception(_OVERLOAD_MSG)],
        "gemini-3-flash-preview": [Exception(_OVERLOAD_MSG), Exception(_OVERLOAD_MSG)],
        "gemini-2.5-flash": [Exception(_OVERLOAD_MSG), Exception(_OVERLOAD_MSG)],
    })
    with pytest.raises(Exception):
        layout_v2._call_gemini_v2("prompt", "key")


def test_build_model_chain_dedup_with_env(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3-flash-preview")
    assert layout_v2._build_model_chain() == ["gemini-3-flash-preview", "gemini-2.5-flash"]


def test_build_model_chain_default(monkeypatch):
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    assert layout_v2._build_model_chain() == [
        "gemini-3.5-flash", "gemini-3-flash-preview", "gemini-2.5-flash",
    ]


def test_is_daily_quota_error():
    assert layout_v2._is_daily_quota_error(_DAILY_QUOTA_MSG)
    assert layout_v2._is_daily_quota_error("429 ... limit: 0, model: x")
    assert not layout_v2._is_daily_quota_error(_OVERLOAD_MSG)
    # per-minute 429 (bez PerDay) NIE jest dobową quotą — zasługuje na retry
    assert not layout_v2._is_daily_quota_error(
        "429 RESOURCE_EXHAUSTED limit: 20 PerMinute")
