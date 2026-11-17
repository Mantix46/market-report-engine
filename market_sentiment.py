"""Publiczne wskaźniki stresu finansowego i pozycjonowania na S&P 500."""

import csv
import io
import logging
from datetime import date, datetime
from math import isfinite
from typing import TypedDict

import requests

from market_calendar import warsaw_today

logger = logging.getLogger(__name__)

OFR_FSI_URL = "https://www.financialresearch.gov/financial-stress-index/data/fsi.csv"
CFTC_TFF_URL = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"
OFR_SOURCE = "https://www.financialresearch.gov/financial-stress-index/"
CFTC_SOURCE = "https://publicreporting.cftc.gov/Commitments-of-Traders/TFF-Futures-Only/gpe5-46if"
CFTC_REPORT_DATE = "report_date_as_yyyy_mm_dd"
CFTC_FIELDS = (
    CFTC_REPORT_DATE,
    "open_interest_all",
    "lev_money_positions_long",
    "lev_money_positions_short",
)
REQUEST_TIMEOUT = (5, 10)


class Observation(TypedDict):
    date: date
    value: float
    previous_date: date | None
    change: float | None
    age_days: int
    is_stale: bool


def _finite_number(raw: object) -> float | None:
    if isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if isfinite(value) else None


def _fetch_ofr_fsi() -> dict[date, float]:
    response = requests.get(OFR_FSI_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    rows = csv.DictReader(io.StringIO(response.text.removeprefix("\ufeff")))
    observations = {}
    for row in rows:
        try:
            observed_on = date.fromisoformat(row["Date"])
        except (KeyError, TypeError, ValueError):
            continue
        value = _finite_number(row.get("OFR FSI"))
        if value is not None:
            observations.setdefault(observed_on, value)
    return observations


def _fetch_cftc_sp500() -> dict[date, float]:
    response = requests.get(
        CFTC_TFF_URL,
        params={
            "cftc_contract_market_code": "13874+",  # S&P 500 Consolidated
            "$select": ",".join(CFTC_FIELDS),
            "$order": f"{CFTC_REPORT_DATE} DESC",
            "$limit": 8,
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list):
        raise ValueError("Oczekiwano listy obserwacji CFTC")
    observations = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            observed_on = datetime.fromisoformat(row[CFTC_REPORT_DATE]).date()
        except (KeyError, TypeError, ValueError):
            continue
        open_interest = _finite_number(row.get("open_interest_all"))
        long_positions = _finite_number(row.get("lev_money_positions_long"))
        short_positions = _finite_number(row.get("lev_money_positions_short"))
        if open_interest is None or open_interest <= 0 or long_positions is None or short_positions is None:
            continue
        value = (long_positions - short_positions) / open_interest * 100
        if isfinite(value):
            observations.setdefault(observed_on, value)
    return observations


def collect_market_sentiment(as_of: date | None = None) -> dict[str, Observation | None]:
    """Pobiera niezależnie obie serie i porównuje dwie ostatnie ważne daty każdej z nich."""
    today = as_of or warsaw_today()
    result = {}
    for key, source, fetch, stale_after in (
        ("ofr_fsi", "OFR", _fetch_ofr_fsi, 7),
        ("cftc_sp500", "CFTC", _fetch_cftc_sp500, 14),
    ):
        result[key] = None
        try:
            observations = fetch()
        except (requests.RequestException, ValueError, csv.Error) as exc:
            logger.warning("Sentyment %s: nie udało się pobrać danych (%s)", source, exc)
            continue
        dates = sorted((day for day in observations if day <= today), reverse=True)
        if not dates:
            logger.warning("Sentyment %s: brak poprawnych obserwacji", source)
            continue
        latest = dates[0]
        previous = dates[1] if len(dates) > 1 else None
        value = observations[latest]
        change = _finite_number(value - observations[previous]) if previous is not None else None
        age_days = (today - latest).days
        if age_days > stale_after:
            logger.warning("Sentyment %s: opóźniony odczyt z %s", source, latest)
        result[key] = Observation(
            date=latest,
            value=value,
            previous_date=previous,
            change=change,
            age_days=age_days,
            is_stale=age_days > stale_after,
        )
    return result


def render_market_sentiment(data: dict[str, Observation | None] | None) -> str:
    """Zwraca wiersze Markdown z wynikami, datami obserwacji i statusem aktualności."""
    lines = []
    for key, label, source, source_url, value_format, change_format, unit in (
        ("ofr_fsi", "OFR Financial Stress Index", "OFR", OFR_SOURCE, ".3f", "+.3f", " pkt"),
        ("cftc_sp500", "CFTC COT S&P 500 (Leveraged Funds netto, % otwartych pozycji)",
         "CFTC TFF Futures Only", CFTC_SOURCE, ".2f", "+.2f", " p.p."),
    ):
        source_link = f"[{source}]({source_url})"
        observation = (data or {}).get(key)
        if observation is None:
            lines.append(f"- **{label}** · b/d (źródło: {source_link})")
            continue
        value = format(observation["value"], value_format).replace(".", ",")
        if key == "cftc_sp500":
            value += "%"
        change = observation["change"]
        previous_date = observation["previous_date"]
        delta = "b/d"
        if change is not None and previous_date is not None:
            delta = f"{format(change, change_format).replace('.', ',')}{unit} względem {previous_date:%d.%m.%Y}"
        status = "opóźnione" if observation["is_stale"] else "aktualne"
        lines.append(
            f"- **{label}** · {value}, stan na {observation['date']:%d.%m.%Y}, "
            f"zmiana {delta} · {status} (źródło: {source_link})"
        )
    return "\n".join(lines)
