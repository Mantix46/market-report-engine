"""Testy publicznych wskaźników sentymentu bez połączenia z siecią."""

from datetime import date
from unittest.mock import Mock

import pytest
import requests

import market_sentiment as sentiment


def _cftc_row(day, open_interest="100", long="20", short="5"):
    return {
        "report_date_as_yyyy_mm_dd": f"{day}T00:00:00.000",
        "open_interest_all": open_interest,
        "lev_money_positions_long": long,
        "lev_money_positions_short": short,
    }


@pytest.fixture
def providers(monkeypatch):
    ofr = Mock(text=(
        "Date,OFR FSI\n2026-10-30,1.05\n2026-11-07,1.25\n"
        "2026-11-07,8.90\n2026-11-08,nan\n2026-11-16,2.10\n"
    ))
    cftc = Mock()
    cftc.json.return_value = [
        _cftc_row("2026-10-31", "200", "20", "40"),
        _cftc_row("2026-10-20"),
        _cftc_row("2026-11-03", "0"),
        _cftc_row("2026-11-07", short="inf"),
        _cftc_row("2026-11-10", long=None),
    ]
    get = Mock(side_effect=lambda url, **kwargs: {
        sentiment.OFR_FSI_URL: ofr,
        sentiment.CFTC_TFF_URL: cftc,
    }[url])
    monkeypatch.setattr(sentiment.requests, "get", get)
    return ofr, cftc, get


def test_sentiment_uses_two_latest_valid_distinct_dates(providers):
    _, _, get = providers
    data = sentiment.collect_market_sentiment(date(2026, 11, 14))
    fsi = data["ofr_fsi"]
    cot = data["cftc_sp500"]
    assert (fsi["date"], fsi["value"], fsi["previous_date"]) == (
        date(2026, 11, 7), 1.25, date(2026, 10, 30),
    )
    assert fsi["change"] == pytest.approx(0.20)
    assert (cot["date"], cot["value"], cot["previous_date"]) == (
        date(2026, 10, 31), -10.0, date(2026, 10, 20),
    )
    assert cot["change"] == pytest.approx(-25.0)
    assert not fsi["is_stale"]
    assert not cot["is_stale"]
    assert get.call_args.kwargs["params"]["cftc_contract_market_code"] == "13874+"
    assert all(call.kwargs["timeout"] for call in get.call_args_list)

    delayed = sentiment.render_market_sentiment(sentiment.collect_market_sentiment(date(2026, 11, 15)))
    assert delayed.count("opóźnione") == 2
    assert "1,250, stan na 07.11.2026, zmiana +0,200 pkt względem 30.10.2026" in delayed
    assert "-10,00%, stan na 31.10.2026, zmiana -25,00 p.p. względem 20.10.2026" in delayed


@pytest.mark.parametrize("failed_source", ["ofr_fsi", "cftc_sp500"])
def test_provider_failure_keeps_other_observation(providers, caplog, failed_source):
    ofr, cftc, _ = providers
    failed_response = ofr if failed_source == "ofr_fsi" else cftc
    failed_response.raise_for_status.side_effect = requests.HTTPError("503")
    data = sentiment.collect_market_sentiment(date(2026, 11, 14))
    assert data[failed_source] is None
    output = sentiment.render_market_sentiment(data)
    assert output.count("b/d") == 1
    assert output.count("stan na") == 1
    assert "nie udało się pobrać danych" in caplog.text


def test_single_observation_has_no_delta(providers, caplog):
    ofr, cftc, _ = providers
    ofr.text = "Date,OFR FSI\n2026-11-07,1.25\n2026-11-08,inf\n"
    cftc.json.return_value = [_cftc_row("2026-11-10", "0")]
    data = sentiment.collect_market_sentiment(date(2026, 11, 14))
    assert data["ofr_fsi"]["change"] is None
    assert data["ofr_fsi"]["previous_date"] is None
    assert data["cftc_sp500"] is None
    assert "brak poprawnych obserwacji" in caplog.text
    assert "zmiana b/d" in sentiment.render_market_sentiment(data)
    assert sentiment.render_market_sentiment(None).count("b/d") == 2
