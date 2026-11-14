from datetime import date, datetime, timezone
from unittest.mock import Mock

import pytest
import requests

import guidance_consensus
import layout_v2
import main
import market_sentiment
import report_builder
from sec_edgar import ARCHIVES_URL, Filing, SecScan


@pytest.mark.parametrize("success", [False, True])
def test_pending_sec_filings_acknowledged_only_after_successful_smtp(monkeypatch, tmp_path, success):
    clock = datetime(2026, 11, 14, 7, 30)
    scan = Mock(spec=SecScan)
    scan_watchlist = Mock(return_value=scan)
    build = Mock(return_value="## SEC EDGAR\n\n- A filing")
    smtp = Mock(return_value=success)
    calls = Mock()
    calls.attach_mock(smtp, "smtp")
    calls.attach_mock(scan.acknowledge, "acknowledge")
    monkeypatch.setattr(main, "warsaw_now", lambda: clock)
    monkeypatch.setattr(main, "market_status", lambda: {
        "US": {"open": False, "weekend": True}, "PL": {"open": False},
    })
    monkeypatch.setattr(main, "scan_watchlist", scan_watchlist)
    monkeypatch.setattr(main, "build_report", build)
    monkeypatch.setattr(main, "send_report_email", smtp)
    monkeypatch.setattr(main, "__file__", str(tmp_path / "main.py"))
    config = dict(smtp_server="example.com", smtp_port=587, smtp_user="user",
                  smtp_password="test", recipient="recipient@example.com")

    main.generate_and_send(config)

    scan_watchlist.assert_called_once_with(persist=True)
    assert build.call_args.kwargs["sec_scan"] is scan
    assert smtp.call_args.kwargs["md_report"] == build.return_value
    assert [entry[0] for entry in calls.mock_calls] == (["smtp", "acknowledge"] if success else ["smtp"])


def test_preview_bypasses_smtp_and_disables_state_persistence(monkeypatch, tmp_path, capsys):
    build = Mock(return_value="Preview")
    smtp = Mock()
    load_config = Mock()
    monkeypatch.setattr(main.sys, "argv", ["main.py", "--preview"])
    monkeypatch.setattr(main, "__file__", str(tmp_path / "main.py"))
    monkeypatch.setattr(main, "build_report", build)
    monkeypatch.setattr(main, "send_report_email", smtp)
    monkeypatch.setattr(main, "load_config", load_config)

    main.main()

    build.assert_called_once_with(persist_state=False)
    smtp.assert_not_called()
    load_config.assert_not_called()
    assert capsys.readouterr().out == "Preview\n"
    assert list(tmp_path.iterdir()) == []


def test_poll_only_persists_sec_without_loading_smtp_or_building_report(monkeypatch, tmp_path):
    scan = Mock(return_value=SecScan())
    build = Mock()
    smtp = Mock()
    load_config = Mock()
    monkeypatch.setattr(main.sys, "argv", ["main.py", "--sec-poll-only"])
    monkeypatch.setattr(main, "__file__", str(tmp_path / "main.py"))
    monkeypatch.setattr(main, "scan_watchlist", scan)
    monkeypatch.setattr(main, "build_report", build)
    monkeypatch.setattr(main, "send_report_email", smtp)
    monkeypatch.setattr(main, "load_config", load_config)

    main.main()

    scan.assert_called_once_with(persist=True)
    build.assert_not_called()
    smtp.assert_not_called()
    load_config.assert_not_called()


def test_preview_collects_sec_and_sentiment_without_refreshing_guidance(monkeypatch):
    data = {"now": datetime(2026, 11, 14, 7, 30)}
    scan = SecScan(warnings=["SEC temporarily unavailable"])
    snapshot = [{"ticker": "MU"}]
    earnings = Mock(return_value=[])
    refresh = Mock()
    sentiment = Mock(return_value={"ofr_fsi": None, "cftc_sp500": {"value": -13.2}})
    scan_watchlist = Mock(return_value=scan)
    monkeypatch.setattr(report_builder, "scan_watchlist", scan_watchlist)
    monkeypatch.setattr(report_builder, "load_guidance_estimates", lambda: snapshot)
    monkeypatch.setattr(report_builder, "collect_earnings", earnings)
    monkeypatch.setattr(report_builder, "collect_market_sentiment", sentiment)
    monkeypatch.setattr(report_builder, "refresh_guidance_estimates", refresh)

    report_builder._collect_enrichments(data, None, persist_state=False)

    scan_watchlist.assert_called_once_with(persist=False)
    refresh.assert_not_called()
    earnings.assert_called_once_with([], guidance_estimates=snapshot)
    sentiment.assert_called_once_with(date(2026, 11, 14))
    assert data["sec_scan"] is scan
    assert data["market_sentiment"] == sentiment.return_value


def test_guidance_snapshot_keeps_future_estimates_with_explicit_capture_time():
    captured = datetime(2026, 8, 15, 9, 30, tzinfo=timezone.utc)
    row = {"symbol": "AVGO", "date": "2026-11-01", "revenueAvg": 34000000000,
           "numAnalystsRevenue": 26, "reportedCurrency": "USD"}
    rows = [row, {**row, "date": "2026-08-02"}, {**row, "reportedCurrency": "EUR"},
            {**row, "numAnalystsRevenue": 0}]

    assert guidance_consensus._future_revenue_rows(rows, "AVGO", "quarter", "USD", captured) == [{
        "ticker": "AVGO", "period_end": "2026-11-01", "scope": "quarter", "metric": "revenue",
        "basis": "reported", "currency": "USD", "value": 34000000000,
        "captured_at": "2026-08-15T09:30:00+00:00", "source": guidance_consensus.SOURCE,
    }]


@pytest.mark.parametrize("earnings_failure", [False, True])
def test_optional_provider_failures_reach_smtp_and_keep_other_sentiment(monkeypatch, tmp_path, earnings_failure):
    clock = datetime(2026, 11, 13, 7, 30)
    status = {"US": {"open": True}, "PL": {"open": True}}
    filing_url = f"{ARCHIVES_URL}/1513845/000110465926126008/0001104659-26-126008-index.htm"
    filing = Filing("NBIS", "0001513845", "0001104659-26-126008", "6-K", "2026-11-12",
                    "2026-11-12", "", (), "tm2629571d1_6k.htm", filing_url)
    scan = SecScan(warnings=["SEC HTTP 503"], filings=[filing] if earnings_failure else [])
    if earnings_failure:
        monkeypatch.setattr(report_builder, "collect_earnings", Mock(side_effect=ValueError("invalid exhibit")))
    acknowledge = Mock()
    monkeypatch.setattr(scan, "acknowledge", acknowledge)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(main, "warsaw_now", lambda: clock)
    monkeypatch.setattr(main, "market_status", lambda: status)
    monkeypatch.setattr(main, "scan_watchlist", lambda **_: scan)
    monkeypatch.setattr(main, "__file__", str(tmp_path / "main.py"))
    monkeypatch.setattr(report_builder, "load_guidance_estimates", list)
    monkeypatch.setattr(report_builder, "refresh_guidance_estimates", Mock())
    monkeypatch.setattr(report_builder, "collect_report_data", lambda persist: {
        "now": clock, "skip": set(), "status": status, "active_tickers": [],
        "persist": persist, "v2": {"index_quotes": {}}, "macro_quotes": {}, "macro_calendar": [],
    })
    monkeypatch.setattr(layout_v2, "collect_v2_extras", Mock())
    monkeypatch.setattr(layout_v2, "render_basic_report_v2", lambda data: layout_v2._assemble_report(
        data, {heading: heading for heading in layout_v2.V2_AI_HEADERS},
    ))
    monkeypatch.setattr(layout_v2, "build_movers_md", lambda _: "## Radar")
    monkeypatch.setattr(layout_v2, "build_valuation_md", lambda _: "## Wycena")
    monkeypatch.setattr(market_sentiment, "_fetch_ofr_fsi", Mock(side_effect=requests.HTTPError("503")))
    monkeypatch.setattr(market_sentiment, "_fetch_cftc_sp500", lambda: {
        date(2026, 11, 10): -11.25, date(2026, 11, 3): -10.75,
    })
    smtp = Mock(return_value=True)
    monkeypatch.setattr(main, "send_report_email", smtp)
    config = dict(smtp_server="example.com", smtp_port=587, smtp_user="user",
                  smtp_password="test", recipient="recipient@example.com")

    main.generate_and_send(config)

    smtp.assert_called_once()
    md = smtp.call_args.kwargs["md_report"]
    assert "SEC HTTP 503" in md
    if earnings_failure:
        assert filing_url in md
        assert "Odczyt danych wynikowych SEC nie powiódł się" in md
    assert "OFR Financial Stress Index** · b/d" in md
    assert "-11,25%" in md
    acknowledge.assert_called_once_with()
