"""Fetch and track official SEC EDGAR filings for the market report."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests


STATE_PATH = Path(__file__).resolve().parent / "state" / "sec_edgar.json"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions"
ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data"
_SEC_HOSTS = {"www.sec.gov", "data.sec.gov"}
_CACHE_TTL = timedelta(days=7)
_BOOTSTRAP_DAYS = 7
_OVERLAP_DAYS = 7
_REQUEST_INTERVAL = 0.15
_REQUEST_ATTEMPTS = 3
_MAX_RETRY_DELAY = 60
_POLL_BUDGET = 45
_ACCESSION = re.compile(r"[0-9]{10}-[0-9]{2}-[0-9]{6}")
_CIK = re.compile(r"[0-9]{10}")
_TICKER = re.compile(r"[A-Z0-9]+(?:[-.][A-Z0-9]+)*")
_CONTACT = re.compile(r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9-]+(?:\.[A-Z0-9-]+)+", re.I)
_REQUEST_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()
_next_request_at = 0.0
_deadline = ContextVar[float | None]("sec_deadline", default=None)
logger = logging.getLogger(__name__)


class SecError(requests.RequestException):
    """SEC request or data failure with a message safe to display."""


@contextmanager
def sec_budget(seconds: float):
    """Bound request waits and timeouts for one group of SEC operations."""
    deadline = time.monotonic() + seconds
    previous = _deadline.get()
    token = _deadline.set(min(previous, deadline) if previous is not None else deadline)
    try:
        yield
    finally:
        _deadline.reset(token)


def _request_time_left() -> float:
    deadline = _deadline.get()
    if deadline is None:
        return float("inf")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SecError("Przekroczono limit czasu odczytu SEC.")
    return remaining


def _wait(delay: float) -> None:
    if delay >= _request_time_left():
        raise SecError("Przekroczono limit czasu odczytu SEC.")
    time.sleep(delay)


def _user_agent() -> str:
    agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if agent and _CONTACT.search(agent) and agent.isascii() and agent.isprintable():
        return agent
    contact = os.environ.get("SEC_CONTACT_EMAIL", "").strip()
    if _CONTACT.fullmatch(contact):
        return f"MarketReportEngine/1.0 {contact}"
    raise SecError("Ustaw SEC_CONTACT_EMAIL z adresem e-mail kontaktowym dla SEC.")


def _pace_request() -> None:
    global _next_request_at
    while True:
        with _REQUEST_LOCK:
            now = time.monotonic()
            delay = _next_request_at - now
            if delay <= 0:
                _next_request_at = now + _REQUEST_INTERVAL
                return
            if delay > _MAX_RETRY_DELAY:
                raise SecError("SEC wstrzymała żądania na dłuższy czas. Próba zostanie ponowiona przy następnym skanowaniu.")
        _wait(delay)


def _retry_delay(response: requests.Response, fallback: float) -> float:
    value = response.headers.get("Retry-After", "")
    if value.isdecimal():
        return float(value)
    if value:
        try:
            until = parsedate_to_datetime(value)
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            return max(0.0, (until - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            pass
    return fallback


def sec_get(url: str, **kwargs) -> requests.Response:
    """Make a paced, bounded request to an official SEC HTTPS host."""
    global _next_request_at
    try:
        parsed = urlsplit(url)
    except ValueError:
        raise SecError("Nieprawidłowy adres SEC.") from None
    if parsed.scheme != "https" or parsed.netloc.lower() not in _SEC_HOSTS or "\\" in url:
        raise SecError("Adres spoza oficjalnych serwisów SEC.")

    headers = requests.structures.CaseInsensitiveDict(kwargs.pop("headers", None) or {})
    headers["User-Agent"] = _user_agent()
    headers.setdefault("Accept-Encoding", "gzip, deflate")
    timeout = kwargs.pop("timeout", 12)
    kwargs["allow_redirects"] = False
    for attempt in range(_REQUEST_ATTEMPTS):
        _pace_request()
        fallback = 0.5 * 2**attempt
        remaining = _request_time_left()
        if isinstance(timeout, tuple):
            request_timeout = tuple(min(value, remaining / 2) for value in timeout)
        else:
            request_timeout = min(timeout, remaining / 2)
        try:
            response = requests.get(url, headers=headers, timeout=request_timeout, **kwargs)
        except (requests.ConnectionError, requests.Timeout):
            if attempt + 1 == _REQUEST_ATTEMPTS:
                raise SecError("Nie udało się połączyć z SEC.") from None
            _wait(fallback)
            continue
        except requests.RequestException:
            raise SecError("Żądanie do SEC nie powiodło się.") from None

        status = response.status_code
        if 200 <= status < 300:
            return response
        retry = status == 429 or 500 <= status < 600
        delay = _retry_delay(response, fallback) if retry else 0
        response.close()
        if retry:
            with _REQUEST_LOCK:
                _next_request_at = max(_next_request_at, time.monotonic() + delay)
            if delay <= _MAX_RETRY_DELAY and attempt + 1 < _REQUEST_ATTEMPTS:
                continue
        raise SecError(f"SEC zwróciła HTTP {status}.")
    raise SecError("Żądanie do SEC nie powiodło się.")


def _sec_json(url: str) -> dict:
    with sec_get(url) as response:
        try:
            data = response.json()
        except ValueError:
            raise SecError("SEC zwróciła nieprawidłowy JSON.") from None
    if not isinstance(data, dict):
        raise SecError("SEC zwróciła nieoczekiwany format danych.")
    return data


def configured_sec_tickers(defaults: list[str]) -> list[str]:
    override = _normalize_tickers(os.environ.get("SEC_TICKERS", "").split(","))
    return override or _normalize_tickers(defaults)


def _normalize_tickers(tickers: list[str]) -> list[str]:
    return list(dict.fromkeys(ticker.strip().upper() for ticker in tickers if ticker.strip()))


def _filing_url(cik: str, accession: str) -> str:
    return f"{ARCHIVES_URL}/{int(cik)}/{accession.replace('-', '')}/{accession}-index.htm"


@dataclass(frozen=True)
class Filing:
    ticker: str
    cik: str
    accession: str
    form: str
    filing_date: str
    report_date: str
    accepted_at: str
    items: tuple[str, ...]
    primary_document: str
    url: str
    description: str = ""


def _filing_order(filing: Filing) -> tuple[str, str, str]:
    return filing.filing_date, filing.accepted_at, filing.accession


@dataclass
class SecScan:
    filings: list[Filing] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    _state_path: Path | None = field(default=None, repr=False)
    weekly_filings: list[Filing] = field(default_factory=list)

    def acknowledge(self) -> bool:
        """Mark the filings included in this scan as sent after SMTP succeeds."""
        if self._state_path is None:
            return False
        if not self.filings:
            return True
        try:
            with _STATE_LOCK:
                state = _load_state(self._state_path, required=True)
                for filing in self.filings:
                    state.pending.pop(filing.accession, None)
                    state.reported.add(filing.accession)
                _save_state(self._state_path, state)
        except (SecError, OSError):
            self.warnings.append("Nie udało się zapisać potwierdzenia wysyłki zgłoszeń SEC.")
            logger.warning("SEC EDGAR: nie udało się zapisać potwierdzenia po wysyłce.")
            return False
        return True


@dataclass
class _Coverage:
    start: date
    through: date | None


@dataclass
class _TickerCache:
    fetched_on: date
    tickers: dict[str, str]


@dataclass
class _State:
    cache: _TickerCache | None = None
    first_seen: dict[str, date] = field(default_factory=dict)
    coverage: dict[str, _Coverage] = field(default_factory=dict)
    pending: dict[str, Filing] = field(default_factory=dict)
    reported: set[str] = field(default_factory=set)


def _valid_ticker(value: str) -> bool:
    return bool(_TICKER.fullmatch(value)) and len(value) <= 20


def _state_filing(accession: str, raw: dict) -> Filing:
    filing = Filing(**{**raw, "items": tuple(raw["items"])})
    for value in (filing.form, filing.report_date, filing.accepted_at, filing.primary_document,
                  filing.description, *filing.items):
        if not isinstance(value, str):
            raise ValueError
    if (
        accession != filing.accession
        or not _ACCESSION.fullmatch(accession)
        or not _CIK.fullmatch(filing.cik)
        or not _valid_ticker(filing.ticker)
        or not isinstance(raw["items"], list)
        or filing.url != _filing_url(filing.cik, accession)
    ):
        raise ValueError
    date.fromisoformat(filing.filing_date)
    if filing.report_date:
        date.fromisoformat(filing.report_date)
    if filing.accepted_at:
        datetime.fromisoformat(filing.accepted_at)
    return filing


def _load_state(path: Path, *, required: bool = False) -> _State:
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        if required:
            raise SecError("Brak zapisanego stanu SEC.") from None
        return _State()
    except (OSError, ValueError):
        raise SecError("Stan SEC jest niedostępny lub uszkodzony.") from None

    try:
        if type(data["version"]) is not int or data["version"] != 1:
            raise ValueError
        first_seen = {ticker: date.fromisoformat(day) for ticker, day in data["first_seen"].items()}
        coverage = {
            cik: _Coverage(date.fromisoformat(window["start"]), date.fromisoformat(window["through"]) if window["through"] else None)
            for cik, window in data["coverage"].items()
        }
        pending = {acc: _state_filing(acc, raw) for acc, raw in data["pending"].items()}
        if not isinstance(data["reported"], list):
            raise ValueError
        reported = set(data["reported"])
        if (
            not all(_valid_ticker(ticker) for ticker in first_seen)
            or not all(_CIK.fullmatch(cik) for cik in coverage)
            or any(window.through and window.through < window.start for window in coverage.values())
            or not all(_ACCESSION.fullmatch(acc) for acc in reported)
            or reported.intersection(pending)
        ):
            raise ValueError
        cache = None
        raw_cache = data["ticker_cache"]
        if raw_cache is not None:
            tickers = raw_cache["tickers"]
            if not tickers or not all(_valid_ticker(ticker) and _CIK.fullmatch(cik) for ticker, cik in tickers.items()):
                raise ValueError
            cache = _TickerCache(date.fromisoformat(raw_cache["fetched_on"]), tickers)
        return _State(cache, first_seen, coverage, pending, reported)
    except (AttributeError, KeyError, TypeError, ValueError):
        raise SecError("Stan SEC jest uszkodzony lub ma nieobsługiwaną wersję. Pozostawiono go bez zmian.") from None


def _save_state(path: Path, state: _State) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cache = state.cache
    payload = {
        "version": 1,
        "ticker_cache": {"fetched_on": cache.fetched_on.isoformat(), "tickers": cache.tickers} if cache else None,
        "first_seen": {ticker: day.isoformat() for ticker, day in state.first_seen.items()},
        "coverage": {
            cik: {"start": window.start.isoformat(), "through": window.through.isoformat() if window.through else None}
            for cik, window in state.coverage.items()
        },
        "pending": {accession: asdict(filing) for accession, filing in state.pending.items()},
        "reported": sorted(state.reported),
    }
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temp_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _ticker_cache(state: _State, today: date, warnings: list[str]) -> _TickerCache | None:
    cache = state.cache
    if cache and timedelta(0) <= today - cache.fetched_on < _CACHE_TTL:
        return cache
    try:
        result = {}
        for row in _sec_json(TICKERS_URL).values():
            try:
                cik = int(row["cik_str"])
                ticker = row["ticker"].strip().upper()
                if 0 < cik <= 9_999_999_999 and _valid_ticker(ticker):
                    result.setdefault(ticker, f"{cik:010d}")
            except (AttributeError, KeyError, OverflowError, TypeError, ValueError):
                continue
        if not result:
            raise ValueError
        return _TickerCache(today, result)
    except (SecError, ValueError):
        warnings.append("Nie udało się odświeżyć mapy tickerów SEC." + (" Użyto ostatniej zapisanej mapy." if cache else ""))
        return cache


def _column_text(data: dict, column: str, index: int) -> str:
    values = data.get(column)
    if isinstance(values, list) and index < len(values) and isinstance(values[index], str):
        return values[index].strip()
    return ""


def _rows(data: dict, ticker: str, cik: str, start: date, end: date) -> tuple[list[Filing], bool]:
    dates = data.get("filingDate")
    accessions = data.get("accessionNumber")
    forms = data.get("form")
    if not all(isinstance(values, list) for values in (dates, accessions, forms)):
        return [], False
    complete = len(dates) == len(accessions) == len(forms)
    filings = []
    for i, filed_value in enumerate(dates):
        try:
            filed = date.fromisoformat(filed_value)
        except (TypeError, ValueError):
            complete = False
            continue
        if not start <= filed <= end:
            continue
        accession = _column_text(data, "accessionNumber", i)
        form = _column_text(data, "form", i)
        if not _ACCESSION.fullmatch(accession) or not form:
            complete = False
            continue

        report_date = _column_text(data, "reportDate", i)
        accepted_at = _column_text(data, "acceptanceDateTime", i)
        if report_date:
            try:
                report_date = date.fromisoformat(report_date).isoformat()
            except ValueError:
                report_date = ""
        if accepted_at:
            try:
                accepted_at = datetime.fromisoformat(accepted_at).isoformat()
            except ValueError:
                accepted_at = ""
        items = tuple(item.strip() for item in _column_text(data, "items", i).split(",") if item.strip())
        filings.append(Filing(
            ticker, cik, accession, form, filed.isoformat(), report_date, accepted_at, items,
            _column_text(data, "primaryDocument", i), _filing_url(cik, accession),
            _column_text(data, "primaryDocDescription", i)[:240],
        ))
    return filings, complete


def _submissions(ticker: str, cik: str, start: date, end: date) -> tuple[list[Filing], bool]:
    data = _sec_json(f"{SUBMISSIONS_URL}/CIK{cik}.json")
    filings = data.get("filings")
    if not isinstance(filings, dict):
        return [], False
    recent = filings.get("recent")
    rows, complete = _rows(recent, ticker, cik, start, end) if isinstance(recent, dict) else ([], False)
    history = filings.get("files")
    if not isinstance(history, list):
        return rows, False
    for page in history:
        try:
            page_start = date.fromisoformat(page["filingFrom"])
            page_end = date.fromisoformat(page["filingTo"])
            if page_start > page_end:
                raise ValueError
            if page_end < start or page_start > end:
                continue
            name = page["name"]
            if not re.fullmatch(rf"CIK{cik}-submissions-[0-9]+\.json", name):
                raise ValueError
            page_rows, valid = _rows(_sec_json(f"{SUBMISSIONS_URL}/{name}"), ticker, cik, start, end)
            rows.extend(page_rows)
            complete = complete and valid
        except (SecError, KeyError, TypeError, ValueError):
            complete = False
    return rows, complete


def _persist_scan(path: Path, state: _State) -> _State:
    """Merge successful work without resurrecting already acknowledged accessions."""
    with _STATE_LOCK:
        current = _load_state(path)
        if state.cache and (not current.cache or state.cache.fetched_on >= current.cache.fetched_on):
            current.cache = state.cache
        for ticker, day in state.first_seen.items():
            current.first_seen[ticker] = min(day, current.first_seen.get(ticker, day))
        for cik, window in state.coverage.items():
            previous = current.coverage.get(cik)
            if previous is None or previous.start > window.start:
                current.coverage[cik] = window
            elif previous.start == window.start and window.through:
                previous.through = max(previous.through or window.through, window.through)
        for accession, filing in state.pending.items():
            if accession not in current.reported:
                current.pending.setdefault(accession, filing)
        _save_state(path, current)
        return current


def poll_sec(tickers: list[str], *, state_path: Path = STATE_PATH, persist: bool = False) -> SecScan:
    """Poll EDGAR and return unacknowledged filings. Preview runs leave state untouched."""
    with sec_budget(_POLL_BUDGET):
        return _poll_sec(tickers, Path(state_path), persist)


def _poll_sec(tickers: list[str], path: Path, persist: bool) -> SecScan:
    scan = SecScan()
    try:
        state = _load_state(path)
    except SecError as exc:
        scan.warnings.append(str(exc))
        state = _State()
        persist = False

    tickers = _normalize_tickers(tickers)
    valid_tickers = [ticker for ticker in tickers if _valid_ticker(ticker)]
    ciks = {}
    weekly = {}
    if len(valid_tickers) != len(tickers):
        scan.warnings.append("Pominięto symbole SEC w nieprawidłowym formacie.")
    if valid_tickers:
        today = datetime.now(ZoneInfo("America/New_York")).date()
        bootstrap = today - timedelta(days=_BOOTSTRAP_DAYS - 1)
        for ticker in valid_tickers:
            state.first_seen.setdefault(ticker, bootstrap)
        try:
            _user_agent()
            configured = True
        except SecError as exc:
            configured = False
            scan.warnings.append(str(exc))
        if configured:
            state.cache = _ticker_cache(state, today, scan.warnings)
        if state.cache:
            for ticker in valid_tickers:
                cik = state.cache.tickers.get(ticker) or state.cache.tickers.get(ticker.replace(".", "-"))
                if cik is None:
                    if configured:
                        scan.warnings.append(f"{ticker}: brak CIK w mapie SEC.")
                    continue
                first_seen = state.first_seen[ticker]
                previous = ciks.get(cik)
                ciks[cik] = (previous[0], min(previous[1], first_seen)) if previous else (ticker, first_seen)

        if configured:
            for cik, (ticker, first_seen) in ciks.items():
                window = state.coverage.setdefault(cik, _Coverage(first_seen, None))
                start = window.start
                if window.through:
                    start = max(start, window.through - timedelta(days=_OVERLAP_DAYS - 1))
                try:
                    filings, complete = _submissions(ticker, cik, min(start, bootstrap), today)
                except SecError as exc:
                    scan.warnings.append(f"{ticker}: {exc}")
                    continue
                for filing in filings:
                    filed = date.fromisoformat(filing.filing_date)
                    if filed >= bootstrap:
                        weekly.setdefault(filing.accession, filing)
                    if filed >= start and filing.accession not in state.reported:
                        state.pending.setdefault(filing.accession, filing)
                if complete:
                    window.through = max(window.through or today, today)
                else:
                    scan.warnings.append(f"{ticker}: niepełne dane EDGAR. Zakres będzie sprawdzony ponownie.")

    if persist and (valid_tickers or state.pending):
        try:
            state = _persist_scan(path, state)
            scan._state_path = path
        except (SecError, OSError):
            scan.warnings.append("Nie udało się zapisać stanu SEC. Zgłoszenia mogą pojawić się ponownie.")
    active = []
    for filing in state.pending.values():
        if filing.ticker in valid_tickers:
            active.append(filing)
        elif filing.cik in ciks:
            active.append(replace(filing, ticker=ciks[filing.cik][0]))
    scan.filings = sorted(active, key=_filing_order, reverse=True)
    # Weekly analysis also needs filings already delivered in a daily report.
    if valid_tickers:
        for filing in active:
            if bootstrap <= date.fromisoformat(filing.filing_date) <= today:
                weekly.setdefault(filing.accession, filing)
    scan.weekly_filings = sorted(weekly.values(), key=_filing_order, reverse=True)
    if scan.warnings:
        logger.warning("SEC EDGAR: %d ostrzeżeń podczas odczytu. Szczegóły dostępne w raporcie.", len(scan.warnings))
    return scan


def _markdown(value: str) -> str:
    return re.sub(r"([\\`*_{}\[\]<>])", r"\\\1", " ".join(value.split()))


_FORM_TOPICS = {
    "3": "Pierwotny stan posiadania insiderów",
    "4": "Zgłoszone zmiany stanu posiadania insiderów",
    "5": "Roczne zgłoszenia insiderów",
    "6-K": "Bieżące informacje emitenta zagranicznego",
    "8-K": "Pozostałe raporty bieżące",
    "10-K": "Raporty roczne",
    "10-Q": "Raporty kwartalne",
    "11-K": "Raporty pracowniczego planu oszczędnościowego",
    "20-F": "Raporty roczne emitenta zagranicznego",
    "40-F": "Raporty roczne emitenta kanadyjskiego",
    "144": "Zawiadomienia o zamiarze sprzedaży papierów wartościowych",
    "DEF 14A": "Materiały dla akcjonariuszy przed głosowaniem",
    "PRE 14A": "Wstępne materiały dla akcjonariuszy przed głosowaniem",
    "S-1": "Rejestracja oferty papierów wartościowych",
    "S-3": "Uproszczona rejestracja oferty papierów wartościowych",
    "F-1": "Rejestracja oferty emitenta zagranicznego",
    "F-3": "Uproszczona rejestracja oferty emitenta zagranicznego",
    "SCHEDULE 13D": "Zgłoszenia znacznego udziału i zamiarów inwestora",
    "SCHEDULE 13G": "Uproszczone zgłoszenia znacznego udziału",
}

_PERIODIC_FORMS = {"10-K", "10-Q", "11-K", "20-F", "40-F"}
_8K_TOPICS = {
    "1.01": "Zawarcie istotnej umowy",
    "1.02": "Rozwiązanie istotnej umowy",
    "1.03": "Upadłość lub postępowanie zarządcze",
    "1.05": "Istotny incydent cyberbezpieczeństwa",
    "2.01": "Zakończone przejęcie lub zbycie aktywów",
    "2.02": "Wyniki i sytuacja finansowa",
    "2.03": "Nowe zobowiązania finansowe lub pozabilansowe",
    "2.04": "Zdarzenia zwiększające lub przyspieszające wymagalność zobowiązań",
    "2.05": "Koszty zamknięcia lub zbycia działalności",
    "2.06": "Istotne odpisy z tytułu utraty wartości",
    "3.01": "Zmiana notowań lub kwestia zgodności z wymogami giełdy",
    "3.02": "Sprzedaż akcji bez rejestracji oferty",
    "3.03": "Istotna zmiana praw posiadaczy papierów wartościowych",
    "4.01": "Zmiana audytora",
    "4.02": "Zastrzeżenia do wiarygodności wcześniejszych sprawozdań lub opinii audytora",
    "5.01": "Zmiana kontroli nad spółką",
    "5.02": "Zmiany w organach spółki lub warunkach wynagradzania kadry",
    "5.03": "Zmiana dokumentów założycielskich, statutu lub roku obrotowego",
    "5.07": "Wyniki głosowania akcjonariuszy",
}


def _base_form(filing: Filing) -> str:
    form = filing.form.upper().removesuffix("/A")
    if form in ("SC 13D", "SC 13G"):
        return form.replace("SC ", "SCHEDULE ", 1)
    return form


def _topics(filing: Filing) -> list[str]:
    form = _base_form(filing)
    if form == "8-K":
        topics = list(dict.fromkeys(_8K_TOPICS[item] for item in filing.items if item in _8K_TOPICS))
        if topics:
            return topics
        if "7.01" in filing.items:
            return ["Komunikaty w trybie Regulation FD"]
    return [_FORM_TOPICS.get(form, f"Inne zgłoszenia ({form})")]


def format_filing_event(filing: Filing, *, financial_results: bool = False) -> str:
    """Describe only events supported by filing metadata or a verified earnings release."""
    topics = (
        ["Publikacja wyników finansowych"]
        if financial_results and _base_form(filing) == "6-K"
        else _topics(filing)
    )
    description = filing.description.strip()
    if description and description.upper() not in {filing.form.upper(), f"FORM {filing.form.upper()}"}:
        topics.append(description)
    return f"{filing.filing_date} · {_representative_link(filing)} · {_markdown(', '.join(topics))}"


def _filing_count(count: int) -> str:
    if count == 1:
        return "1 zgłoszenie"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return f"{count} zgłoszenia"
    return f"{count} zgłoszeń"


def _representative_link(filing: Filing) -> str:
    return f"[EDGAR · {_markdown(filing.form)}]({filing.url})"


def _topic_summary(topic: str, filings: list[Filing], *, link: bool) -> str:
    latest = filings[0]
    earliest = filings[-1].filing_date
    dates = latest.filing_date if earliest == latest.filing_date else f"{earliest} – {latest.filing_date}"
    details = [_filing_count(len(filings)), dates]
    periods = sorted({filing.report_date for filing in filings if _base_form(filing) in _PERIODIC_FORMS and filing.report_date})
    if periods:
        details.append(f"Koniec okresu {', '.join(periods)}")
    amendments = sum(filing.form.upper().endswith("/A") for filing in filings)
    if amendments:
        details.append(f"korekty {amendments}")
    if link:
        details.append(_representative_link(latest))
    return f"- **{_markdown(topic)}** · " + " · ".join(details)


def render_sec(scan: SecScan, *, earnings_accessions: set[str] | None = None) -> str:
    """Summarize the last seven SEC filing dates by company and topic."""
    lines = ["## SEC EDGAR", ""]
    weekly = {filing.accession: filing for filing in scan.weekly_filings}
    companies = {}
    for filing in sorted(weekly.values(), key=_filing_order, reverse=True):
        companies.setdefault(filing.ticker, []).append(filing)
    if companies:
        lines.append("Ostatnie 7 dni według dat zgłoszeń w SEC.")
    elif not scan.warnings:
        lines.append("Brak zgłoszeń z ostatnich 7 dni.")
    for ticker, filings in sorted(companies.items()):
        lines.extend(["", f"### {_markdown(ticker)} · {_filing_count(len(filings))}", ""])
        grouped = {}
        for filing in filings:
            topics = (
                ["Publikacje wynikowe emitenta zagranicznego"]
                if earnings_accessions and filing.accession in earnings_accessions and _base_form(filing) == "6-K"
                else _topics(filing)
            )
            for topic in topics:
                grouped.setdefault(topic, []).append(filing)
        linked = set()
        for topic, group in grouped.items():
            accession = group[0].accession
            lines.append(_topic_summary(topic, group, link=accession not in linked))
            linked.add(accession)

    backlog = {}
    for filing in scan.filings:
        if filing.accession not in weekly:
            backlog.setdefault(filing.ticker, []).append(filing)
    if backlog:
        lines.extend(["", "### Starsze zgłoszenia oczekujące na wysyłkę", ""])
        for ticker, filings in sorted(backlog.items()):
            latest = max(filings, key=_filing_order)
            lines.append(f"- **{_markdown(ticker)}** · {_filing_count(len(filings))} · {_representative_link(latest)}")
    if scan.warnings:
        if companies or backlog:
            lines.append("")
        lines.extend(f"- {_markdown(warning)}" for warning in scan.warnings)
    return "\n".join(lines)
