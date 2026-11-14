from datetime import date
from decimal import Decimal
from unittest.mock import Mock

from lxml import html
import pandas as pd
import pytest

import earnings_report as earnings
import guidance_consensus
from sec_edgar import ARCHIVES_URL, Filing


def filing(ticker, cik, accession, form, filed, report, items=()):
    url = f"{ARCHIVES_URL}/{int(cik)}/{accession.replace('-', '')}/{accession}-index.htm"
    return Filing(ticker, cik, accession, form, filed, report, "", items, "filing.htm", url)


MU_10K = filing("MU", "0000723125", "0000723125-26-000023", "10-K", "2026-10-09", "2026-09-03")
AVGO_10Q = filing("AVGO", "0001730168", "0001730168-26-000080", "10-Q", "2026-09-10", "2026-08-02")
NBIS_RELEASE = filing("NBIS", "0001513845", "0001104659-26-126008", "6-K", "2026-11-12", "2026-11-12")
NBIS_STATEMENTS = filing("NBIS", "0001513845", "0001104659-26-126238", "6-K", "2026-11-12", "2026-11-12")
NBIS_BRIDGE = filing("NBIS", "0001513845", "0001104659-26-117978", "6-K", "2026-10-22", "2026-10-22")
FOREIGN_20F = filing("FOREIGN", "0001513845", "0001104659-26-000001", "20-F", "2026-04-15", "2025-12-31")


@pytest.mark.parametrize(("report", "scope", "start", "revenue", "eps"), [
    (MU_10K, "annual", "2025-08-29", 133188000000, "74.33"),
    (AVGO_10Q, "quarter", "2026-05-04", 29591000000, "2.68"),
])
def test_reads_exact_sec_accession_and_duration(monkeypatch, report, scope, start, revenue, eps):
    """Financial statement values and filing metadata come from the linked 10-K / 10-Q."""
    def tag(unit, value):
        return {"units": {unit: [
            {"accn": report.accession, "start": start, "end": report.report_date, "val": value},
            {"accn": report.accession, "start": "2026-07-01", "end": report.report_date, "val": 999},
            {"accn": "0000000000-26-000001", "start": start, "end": report.report_date, "val": 888},
        ]}}

    company_facts = {"us-gaap": {
        "RevenueFromContractWithCustomerExcludingAssessedTax": tag("USD", revenue),
        "EarningsPerShareDiluted": tag("USD/shares", eps),
    }}
    monkeypatch.setattr(earnings, "_company_facts", lambda _: company_facts)

    [result] = earnings.collect_earnings([report], fmp_key="", yahoo_fallback=False)

    assert result.period == earnings.Period(scope, date.fromisoformat(report.report_date))
    assert result.measures[("revenue", "GAAP")].value == Decimal(revenue)
    assert result.measures[("eps", "GAAP")].value == Decimal(eps)
    assert result.measures[("eps", "GAAP")].source == report.url
    assert result.result_consensus == {}


def test_foreign_release_without_xbrl_merges_same_period_and_ignores_financing(monkeypatch):
    results_html = """
    <h1>Nebius Group announces third quarter 2026 financial results</h1>
    <p>November 12, 2026. Nebius today announced its financial results for the
    third quarter ended September 30, 2026. U.S. GAAP (in USD millions).</p>
    <table><tr><th></th><th>Three months ended</th></tr>
    <tr><th></th><th>September 30, 2026</th></tr>
    <tr><td>Revenue</td><td>980.8</td></tr>
    <tr><td>Diluted earnings per share</td><td>(0.13)</td></tr></table>
    """
    bridge_html = """
    <h1>Nebius secures $3.5 billion financing facility with Citi</h1>
    <p>October 22, 2026. The facility provides access to capital for its global build-out.</p>
    <h2>Forward-looking statements</h2>
    <p>All statements about anticipated financing transactions and expected financial results
    are forward-looking statements.</p>
    """
    docs = {NBIS_RELEASE: results_html, NBIS_STATEMENTS: results_html, NBIS_BRIDGE: bridge_html}
    monkeypatch.setattr(earnings, "_filing_documents", lambda row: [
        (row.url, html.document_fromstring(docs[row])),
    ])
    facts = Mock(return_value={})
    monkeypatch.setattr(earnings, "_company_facts", facts)

    [result] = earnings.collect_earnings([NBIS_BRIDGE, NBIS_STATEMENTS, NBIS_RELEASE],
                                         fmp_key="", yahoo_fallback=False)

    assert result.period == earnings.Period("quarter", date(2026, 9, 30))
    assert {row.accession for row in result.filings} == {NBIS_RELEASE.accession, NBIS_STATEMENTS.accession}
    assert result.measures[("revenue", "GAAP")].value == Decimal("980800000")
    assert result.measures[("eps", "GAAP")].value == Decimal("-0.13")
    assert "**Ryzyka**" in earnings.render_earnings([result])
    assert "pozostał ujemny" in earnings.render_earnings([result])
    facts.assert_called_once_with(NBIS_RELEASE.cik)


def test_consensus_requires_matching_report_period_currency_and_eps_basis():
    entry = earnings.Earnings("AVGO", earnings.Period("quarter", date(2026, 8, 2)), release_date=date(2026, 9, 2))
    entry.measures = {
        ("revenue", "GAAP"): earnings.Measure("revenue", "GAAP", "USD", Decimal("29591000000"), AVGO_10Q.url),
        ("eps", "GAAP"): earnings.Measure("eps", "GAAP", "USD", Decimal("2.68"), AVGO_10Q.url, precision=Decimal("0.01")),
        ("eps", "adjusted"): earnings.Measure("eps", "adjusted", "USD", Decimal("3.32"), AVGO_10Q.url, precision=Decimal("0.01")),
    }
    # MarketBeat's historical estimates are cross-checks. Here they stand in for a FMP row.
    row = {"symbol": "AVGO", "date": "2026-09-02", "fiscalDateEnding": "2026-08-02", "period": "Q3",
           "currency": "USD", "revenueActual": 29591000000, "revenueEstimated": 29240000000,
           "epsActual": 3.32, "epsEstimated": 3.22}

    assert {key: result.estimate for key, result in earnings._result_comparisons(entry, [row]).items()} == {
        ("revenue", "GAAP"): Decimal("29240000000"), ("eps", "adjusted"): Decimal("3.22"),
    }
    assert earnings._result_comparisons(entry, [{**row, "fiscalDateEnding": "2026-08-03"}]) == {}
    assert earnings._result_comparisons(entry, [{**row, "currency": "EUR"}]) == {}
    assert ("eps", "adjusted") not in earnings._result_comparisons(entry, [{**row, "epsActual": 3.42}])
    entry.period = earnings.Period("annual", date(2026, 8, 2))
    assert earnings._result_comparisons(entry, [row]) == {}


def test_guidance_uses_latest_pre_release_snapshot_for_same_period():
    guidance = earnings.Guidance("revenue", "reported", "USD", "quarter", "Q4 2026", date(2026, 11, 1),
                                 Decimal("34800000000"), Decimal("34800000000"), AVGO_10Q.url)
    entry = earnings.Earnings("AVGO", earnings.Period("quarter", date(2026, 8, 2)),
                              guidance=[guidance], release_date=date(2026, 9, 2))
    row = {"ticker": "AVGO", "period_end": "2026-11-01", "scope": "quarter", "metric": "revenue",
           "basis": "reported", "currency": "USD", "captured_at": "2026-08-30T07:30:00+00:00",
           "value": 34000000000, "source": guidance_consensus.SOURCE}
    observations = [row, {**row, "captured_at": "2026-08-29T07:30:00+00:00", "value": 33000000000},
                    {**row, "captured_at": "2026-09-02T07:30:00+00:00", "value": 36000000000},
                    {**row, "captured_at": "2026-09-01T23:30:00-04:00", "value": 36000000000},
                    {**row, "captured_at": "2026-09-01T07:30:00+00:00", "period_end": "2026-11-02"},
                    {**row, "captured_at": "2026-09-01T07:30:00+00:00", "as_of": "2026-09-03"}]

    assert earnings._guidance_comparisons(entry, observations) == {
        guidance: earnings.Comparison(Decimal("34000000000"), guidance_consensus.SOURCE, date(2026, 8, 30)),
    }


def test_yahoo_eps_fallback_reads_actual_announcement_day_and_known_values(monkeypatch):
    announcement = pd.DatetimeIndex(["2026-09-02 16:00", "2026-12-09 16:00"], tz="America/New_York")
    ticker = Mock()
    ticker.get_earnings_dates.return_value = pd.DataFrame(
        {"EPS Estimate": [3.24, 3.84], "Reported EPS": [3.32, float("nan")]}, index=announcement,
    )
    monkeypatch.setattr(earnings.yf, "Ticker", Mock(return_value=ticker))

    entries = earnings._yahoo_eps_history("AVGO")
    first = next(row for row in entries if row["date"] == date(2026, 9, 2))
    assert first["epsActual"] == 3.32
    assert first["epsEstimated"] == 3.24
    assert first["period"] == "QUARTER"
    ticker.get_earnings_history.assert_not_called()


@pytest.mark.parametrize("payload", ['{"good":["999 USD"],"risk":[]}', '{"good":[999],"risk":[]}', 'not JSON'])
def test_untrusted_model_selection_falls_back_to_sec_facts(payload):
    entry = earnings.Earnings("AVGO", earnings.Period("quarter", date(2026, 8, 2)), filings=[AVGO_10Q])
    entry.measures[("revenue", "GAAP")] = earnings.Measure(
        "revenue", "GAAP", "USD", Decimal("29591000000"), AVGO_10Q.url,
        previous=Decimal("15952000000"), previous_end=date(2025, 8, 3),
    )
    text, selection = earnings.extract_earnings_selection(
        f"## Raport\nTreść\n<earnings_selection>{payload}</earnings_selection>"
    )

    assert text == "## Raport\nTreść"
    assert earnings.render_earnings([entry], selection) == earnings.render_earnings([entry])
    assert "999 USD" not in earnings.render_earnings([entry], selection)
    assert AVGO_10Q.url in earnings.render_earnings([entry], selection)


def test_truncated_model_selection_is_removed_from_report():
    heading = "## 10. Watchlist\nTreść"
    clean, selection = earnings.extract_earnings_selection(f'{heading}\n<earnings_selection>{{"good":["999 USD"')

    assert clean == heading
    assert selection is None


def test_periodic_20f_uses_ifrs_and_preserves_currency(monkeypatch):
    def facts(unit, value):
        return {"units": {unit: [{"accn": FOREIGN_20F.accession, "start": "2025-01-01",
                                  "end": "2025-12-31", "val": value}]}}

    monkeypatch.setattr(earnings, "_company_facts", lambda _: {"ifrs-full": {
        "Revenue": facts("EUR", 4000000000), "DilutedEarningsLossPerShare": facts("EUR/shares", 1.23),
    }})
    [result] = earnings.collect_earnings([FOREIGN_20F], fmp_key="", yahoo_fallback=False)

    assert result.period == earnings.Period("annual", date(2025, 12, 31))
    assert result.measures[("revenue", "IFRS")].currency == "EUR"
    assert result.measures[("eps", "IFRS")].value == Decimal("1.23")
    assert "[20-F z 2026-04-15]" in earnings.render_earnings([result])
