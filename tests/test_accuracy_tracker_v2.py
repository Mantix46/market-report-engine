"""Regresja pętli feedbacku i kompatybilności prediction_tracker.csv."""

import csv
import os
import sys
from datetime import date, datetime

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import accuracy_tracker  # noqa: E402


def _prediction(date_value="2026-07-11", source="gemini_report", ticker="AMKR"):
    return {
        "date": date_value,
        "ticker": ticker,
        "direction": "up",
        "horizon_days": 5,
        "base_price": 100.0,
        "thesis": "katalizator i trend",
        "source": source,
        "atr_pct": 4.0,
        "confidence": "średnia",
        "forecast_context": "{\"earnings\":{\"date\":\"2026-07-30\"}}",
    }


def test_extract_prediction_reviews_accepts_machine_format():
    report = (
        "### Analiza trafionych i nietrafionych prognoz\n"
        "- 2026-07-11 AMKR: wynik=nietrafiona; wyjaśnienie=kurs spadł mimo tezy; "
        "lekcja=nie nadawać dużej wagi pojedynczej rekomendacji.\n"
    )
    reviews = accuracy_tracker.extract_prediction_reviews(report)
    assert reviews == [{
        "date": "2026-07-11",
        "ticker": "AMKR",
        "verdict": "nietrafiona",
        "outcome_explanation": "kurs spadł mimo tezy",
        "week_driver": "",
        "lesson": "nie nadawać dużej wagi pojedynczej rekomendacji.",
    }]


def test_save_predictions_keeps_ai_and_rule_baseline_separately(monkeypatch, tmp_path):
    tracker_path = tmp_path / "prediction_tracker.csv"
    monkeypatch.setattr(accuracy_tracker, "get_predictions_file_path", lambda: str(tracker_path))
    monkeypatch.setattr(accuracy_tracker, "warsaw_now", lambda: datetime(2026, 7, 11, 9, 0))

    accuracy_tracker.save_predictions([
        _prediction(source="gemini_report"),
        _prediction(source="rule_based_pre_gemini"),
    ])
    rows = list(csv.DictReader(tracker_path.open(encoding="utf-8", newline="")))
    assert {row["source"] for row in rows} == {"gemini_report", "rule_based_pre_gemini"}

    replacement = _prediction(source="gemini_report")
    replacement["thesis"] = "zaktualizowana teza"
    accuracy_tracker.save_predictions([replacement])
    rows = list(csv.DictReader(tracker_path.open(encoding="utf-8", newline="")))
    assert len(rows) == 2
    assert next(row for row in rows if row["source"] == "rule_based_pre_gemini")["thesis"] == "katalizator i trend"
    assert next(row for row in rows if row["source"] == "gemini_report")["thesis"] == "zaktualizowana teza"


def test_evaluation_retains_removed_ticker(monkeypatch, tmp_path):
    tracker_path = tmp_path / "prediction_tracker.csv"
    monkeypatch.setattr(accuracy_tracker, "get_predictions_file_path", lambda: str(tracker_path))
    monkeypatch.setattr(accuracy_tracker, "warsaw_today", lambda: date(2026, 7, 20))

    accuracy_tracker.save_predictions([_prediction()])
    history = pd.DataFrame(
        {"Close": [100, 101, 102, 103, 104, 105]},
        index=pd.to_datetime([
            "2026-07-11", "2026-07-13", "2026-07-14", "2026-07-15",
            "2026-07-16", "2026-07-17",
        ]),
    )

    class _Ticker:
        def history(self, period="6mo"):
            return history

    monkeypatch.setattr(accuracy_tracker.yf, "Ticker", lambda _ticker: _Ticker())
    monkeypatch.setattr(accuracy_tracker, "clean_history", lambda _ticker, hist, _t: hist)
    result = accuracy_tracker.evaluate_previous_predictions(dry_run=True)

    assert result["total_evaluated"] == 1
    assert result["new"][0]["ticker"] == "AMKR"
    rows = list(csv.DictReader(tracker_path.open(encoding="utf-8", newline="")))
    assert rows[0]["ticker"] == "AMKR"


def test_record_prediction_reviews_updates_only_matching_gemini_row(monkeypatch, tmp_path):
    tracker_path = tmp_path / "prediction_tracker.csv"
    monkeypatch.setattr(accuracy_tracker, "get_predictions_file_path", lambda: str(tracker_path))
    monkeypatch.setattr(accuracy_tracker, "warsaw_now", lambda: datetime(2026, 7, 20, 9, 0))
    rows = [_prediction(source="gemini_report"), _prediction(source="rule_based_pre_gemini")]
    for row in rows:
        row.update({"evaluated_date": "2026-07-20", "realized_pct": "-5.00", "hit": "0"})
    accuracy_tracker.save_predictions(rows)

    changed = accuracy_tracker.record_prediction_reviews([{
        "date": "2026-07-11",
        "ticker": "AMKR",
        "verdict": "nietrafiona",
        "outcome_explanation": "ruch przeciwny do tezy",
        "lesson": "weryfikować katalizator w newsach",
    }])
    assert changed == 1
    saved = list(csv.DictReader(tracker_path.open(encoding="utf-8", newline="")))
    assert next(row for row in saved if row["source"] == "gemini_report")["lesson"] == "weryfikować katalizator w newsach"
    assert next(row for row in saved if row["source"] == "rule_based_pre_gemini")["lesson"] == ""


def test_extract_prediction_reviews_accepts_week_driver_and_colon():
    report = (
        "- **2026-07-11** **AMKR**: wynik: nietrafiona; wyjaśnienie=teza up, a kurs spadł; "
        "przeważyło=guidance poniżej oczekiwań; lekcja=czekać na wyniki\n"
    )
    reviews = accuracy_tracker.extract_prediction_reviews(report)
    assert reviews[0]["week_driver"] == "guidance poniżej oczekiwań"
    assert reviews[0]["verdict"] == "nietrafiona"


def test_format_accuracy_section_includes_reason():
    md = accuracy_tracker.format_accuracy_section({
        "new": [{
            "prediction_date": "2026-07-11",
            "ticker": "AMKR",
            "direction": "up",
            "realized_pct": -5.0,
            "threshold_pct": 2.0,
            "hit": False,
            "outcome_explanation": "teza wzrostowa, a kurs spadł",
            "week_driver": "słabe wyniki",
        }],
        "total_evaluated": 1,
        "total_hits": 0,
        "pending": 0,
        "stats": {},
    })
    assert "nietrafiona" in md
    assert "teza wzrostowa, a kurs spadł" in md
    assert "słabe wyniki" in md
    assert "Trafność prognoz tygodniowych" in md


def test_format_accuracy_section_splits_atr_and_legacy_cohorts():
    md = accuracy_tracker.format_accuracy_section({
        "new": [],
        "total_evaluated": 10,
        "total_hits": 4,
        "pending": 0,
        "stats": {"source": {"gemini_report": {"hits": 3, "total": 6}}},
        "atr_evaluated": 6,
        "atr_hits": 3,
        "legacy_evaluated": 4,
        "legacy_hits": 1,
    })
    assert "Skuteczność łącznie" in md
    assert "próg 0,5×ATR: 3/6" in md
    assert "stary próg stały" in md
    assert "gemini_report: 3/6" in md
