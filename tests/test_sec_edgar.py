import json
from datetime import date, datetime, timezone
from unittest.mock import Mock

import pytest
import requests

import sec_edgar


TICKERS = {"0": {"ticker": "NBIS", "cik_str": 1513845}}
SUBMISSIONS = f"{sec_edgar.SUBMISSIONS_URL}/CIK0001513845.json"
ACCESSION = "0001104659-26-126008"
DATE = "2026-11-12"


def submission(accessions):
    recent = {
        "accessionNumber": accessions,
        "filingDate": [DATE for _ in accessions],
        "reportDate": ["2026-09-30" for _ in accessions],
        "acceptanceDateTime": [f"{DATE}T14:00:00Z" for _ in accessions],
        "form": ["6-K" for _ in accessions],
        "primaryDocument": ["nbis.htm" for _ in accessions],
    }
    return {"filings": {"recent": recent, "files": []}}


@pytest.fixture
def sec_feed(monkeypatch):
    class TestClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 14, 12, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setenv("SEC_USER_AGENT", "Market Report QA qa@example.com")
    monkeypatch.setattr(sec_edgar, "datetime", TestClock)
    feed = {sec_edgar.TICKERS_URL: TICKERS, SUBMISSIONS: submission([ACCESSION])}
    monkeypatch.setattr(sec_edgar, "_sec_json", feed.__getitem__)
    return feed


def test_no_filings_is_distinct_from_failure(sec_feed, tmp_path):
    sec_feed[SUBMISSIONS] = submission([])
    path = tmp_path / "sec.json"
    scan = sec_edgar.poll_sec(["NBIS"], state_path=path, persist=True)

    assert scan.filings == []
    assert scan.warnings == []
    assert "Brak nowych zgłoszeń" in sec_edgar.render_sec(scan)
    state = json.loads(path.read_text(encoding="utf-8"))
    assert date.fromisoformat(state["coverage"]["0001513845"]["through"]) == date(2026, 11, 14)


def test_preview_leaves_state_unchanged_and_deduplicates_accession(sec_feed, tmp_path):
    sec_feed[SUBMISSIONS] = submission([ACCESSION, ACCESSION])
    path = tmp_path / "sec.json"
    preview = sec_edgar.poll_sec(["NBIS", " nbis "], state_path=path)
    assert [filing.accession for filing in preview.filings] == [ACCESSION]
    assert not preview.acknowledge()
    assert not path.exists()

    poll = sec_edgar.poll_sec(["NBIS"], state_path=path, persist=True)
    assert len(poll.filings) == 1
    before = path.read_bytes()
    assert len(sec_edgar.poll_sec(["NBIS"], state_path=path).filings) == 1
    assert path.read_bytes() == before

    assert poll.acknowledge()
    next_scan = sec_edgar.poll_sec(["NBIS"], state_path=path, persist=True)
    assert next_scan.filings == []
    assert [filing.accession for filing in next_scan.weekly_filings] == [ACCESSION]
    state = json.loads(path.read_text(encoding="utf-8"))
    assert state["pending"] == {}
    assert state["reported"] == [ACCESSION]


@pytest.mark.parametrize("status", [429, 503])
def test_sec_get_retries_transient_status_with_retry_after(status, monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "Market Report QA qa@example.com")
    monkeypatch.setattr(sec_edgar, "_next_request_at", 0)
    monkeypatch.setattr(sec_edgar, "_pace_request", Mock())
    retry = Mock(status_code=status, headers={"Retry-After": "2"})
    ok = Mock(status_code=200)
    request = Mock(side_effect=[retry, ok])
    monkeypatch.setattr(sec_edgar.requests, "get", request)
    clock = Mock(return_value=10)
    monkeypatch.setattr(sec_edgar.time, "monotonic", clock)

    assert sec_edgar.sec_get(SUBMISSIONS) is ok
    assert request.call_count == 2
    assert sec_edgar._next_request_at == 12
    assert request.call_args.kwargs["allow_redirects"] is False
    assert request.call_args.kwargs["headers"]["User-Agent"] == "Market Report QA qa@example.com"
    retry.close.assert_called_once_with()


def test_failed_poll_keeps_pending_and_does_not_advance_coverage(sec_feed, monkeypatch, tmp_path):
    path = tmp_path / "sec.json"
    first = sec_edgar.poll_sec(["NBIS"], state_path=path, persist=True)
    assert len(first.filings) == 1

    def unavailable(url):
        raise sec_edgar.SecError("SEC HTTP 503")

    monkeypatch.setattr(sec_edgar, "_sec_json", unavailable)
    before = path.read_bytes()
    scan = sec_edgar.poll_sec(["NBIS"], state_path=path, persist=True)

    assert scan.filings == first.filings
    assert scan.warnings == ["NBIS: SEC HTTP 503"]
    assert "Brak nowych zgłoszeń" not in sec_edgar.render_sec(scan)
    assert path.read_bytes() == before


def test_invalid_contact_stays_local_and_returns_warning(monkeypatch, tmp_path):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    monkeypatch.delenv("SEC_CONTACT_EMAIL", raising=False)
    request = Mock(side_effect=requests.RequestException)
    monkeypatch.setattr(sec_edgar.requests, "get", request)
    path = tmp_path / "sec.json"
    scan = sec_edgar.poll_sec(["NBIS"], state_path=path, persist=True)

    request.assert_not_called()
    assert scan.filings == []
    assert "SEC_CONTACT_EMAIL" in scan.warnings[0]
    state = json.loads(path.read_text(encoding="utf-8"))
    assert "NBIS" in state["first_seen"]
    assert state["coverage"] == {}
