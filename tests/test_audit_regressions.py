from unittest.mock import patch

import pytest

import csv_state
import data_fetching


def test_top_movers_keeps_only_correct_direction(monkeypatch):
    monkeypatch.setattr(data_fetching, "fetch_quotes_batch", lambda *_: {
        "UP": {"change_pct": 2, "volume_ratio": 1.5},
        "THIN": {"change_pct": 8, "volume_ratio": 0.4},
        "MISSING": {"change_pct": None, "volume_ratio": 1},
    })
    winners, losers = data_fetching.get_top_movers(["UP", "THIN", "MISSING"])
    assert [ticker for ticker, _ in winners] == ["UP"]
    assert losers == []


def test_insider_value_requires_transaction_price():
    assert data_fetching._parse_row_value({}, 20, "Stock Purchase") is None


def test_failed_csv_replace_preserves_previous_state(tmp_path):
    path = tmp_path / "state.csv"
    path.write_text("original", encoding="utf-8")
    with patch.object(csv_state.os, "replace", side_effect=OSError), pytest.raises(OSError):
        csv_state.write_csv(path, ["ticker"], [{"ticker": "MU"}])
    assert path.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.iterdir()) == [path]
