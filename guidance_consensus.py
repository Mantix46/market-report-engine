"""Private snapshots of FMP revenue consensus for future fiscal periods."""

import json
import logging
import math
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import monotonic

import requests

logger = logging.getLogger(__name__)

STATE_PATH = Path(__file__).resolve().parent / "state" / "earnings_consensus.json"
SOURCE = "https://site.financialmodelingprep.com/developer/docs/stable/financial-estimates"
FMP_API = "https://financialmodelingprep.com/stable/"
RETENTION_DAYS = 90


def load_guidance_estimates(path: Path = STATE_PATH) -> list[dict]:
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        logger.warning("Nie udało się odczytać historii konsensusu guidance.")
        return []
    if not isinstance(rows, list):
        logger.warning("Historia konsensusu guidance ma nieprawidłowy format.")
        return []
    return [row for row in rows if isinstance(row, dict)]


def _fmp_json(endpoint: str, key: str, *, deadline: float, **params) -> list:
    left = deadline - monotonic()
    if left <= 0:
        raise TimeoutError
    timeout = min(3.0, left / 2)
    response = requests.get(
        FMP_API + endpoint, params={**params, "apikey": key},
        timeout=(timeout, timeout), allow_redirects=False,
    )
    if response.status_code != 200:
        raise ValueError("FMP HTTP status")
    rows = response.json()
    if not isinstance(rows, list):
        raise ValueError("FMP response shape")
    return rows


def _future_revenue_rows(
    rows: list, ticker: str, scope: str, currency: str, captured_at: datetime
) -> list[dict]:
    estimates = []
    for row in rows:
        if not isinstance(row, dict) or row.get("symbol") != ticker:
            continue
        try:
            period_end = date.fromisoformat(row["date"])
            value = float(row["revenueAvg"])
            contributors = int(row["numAnalystsRevenue"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if period_end <= captured_at.date() or not math.isfinite(value) or value <= 0 or contributors <= 0:
            continue
        reported_currency = row.get("reportedCurrency") or row.get("currency") or currency
        if reported_currency != currency:
            continue
        estimates.append({
            "ticker": ticker, "period_end": period_end.isoformat(), "scope": scope,
            "metric": "revenue", "basis": "reported", "currency": currency,
            "value": value, "captured_at": captured_at.isoformat(), "source": SOURCE,
        })
    return estimates


def refresh_guidance_estimates(tickers: list[str], previous: list[dict], path: Path = STATE_PATH) -> None:
    key = os.getenv("FMP_API_KEY") or os.getenv("FINANCIAL_MODELING_PREP_API_KEY")
    if not key or key in {"your_financial_modeling_prep_api_key_here", "WKLEJ_KLUCZ_API"}:
        return

    fresh = []
    deadline = monotonic() + 25
    for ticker in tickers:
        try:
            financials = _fmp_json(
                "income-statement", key, deadline=deadline, symbol=ticker, period="annual", limit=1,
            )
            if not financials or not isinstance(financials[0], dict) or financials[0].get("symbol") != ticker:
                continue
            currency = financials[0].get("reportedCurrency")
            if not isinstance(currency, str) or len(currency) != 3 or not currency.isascii() or not currency.isupper():
                continue
            for scope in ("quarter", "annual"):
                estimates = _fmp_json(
                    "analyst-estimates", key, deadline=deadline, symbol=ticker, period=scope, limit=12,
                )
                fresh.extend(_future_revenue_rows(estimates, ticker, scope, currency, datetime.now(timezone.utc)))
        except (requests.RequestException, ValueError, TimeoutError):
            logger.warning("FMP: nie udało się pobrać części szacunków guidance.")
        if monotonic() >= deadline:
            break

    if not fresh:
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).date().isoformat()
    history = {}
    for row in [*previous, *fresh]:
        stamp = row.get("captured_at")
        fields = (row.get("ticker"), row.get("period_end"), row.get("scope"), stamp)
        if all(isinstance(field, str) for field in fields) and stamp[:10] >= cutoff:
            history[(*fields[:3], stamp[:10])] = row

    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(list(history.values()), handle, ensure_ascii=False, allow_nan=False)
        temporary.replace(path)
    except (OSError, ValueError):
        logger.warning("Nie udało się zapisać historii konsensusu guidance.")
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                logger.warning("Nie udało się usunąć tymczasowego pliku konsensusu guidance.")
