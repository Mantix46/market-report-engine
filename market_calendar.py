"""
market_calendar.py — Wykrywanie dni wolnych od giełdy (USA + GPW).

Korzysta z biblioteki `holidays`:
  - USA: kalendarz finansowy NYSE (holidays.financial_holidays("NYSE"))
  - GPW: polskie święta państwowe (holidays.country_holidays("PL")) — GPW jest zamknięta
    w te same dni co dni ustawowo wolne w Polsce.

Uwaga: biblioteka nie rozróżnia sesji skróconych (half-days) — traktujemy je jako otwarte.
"""

from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

# Jedno źródło prawdy o czasie: raport zawsze "myśli" czasem polskim,
# niezależnie od strefy maszyny (GitHub Actions działa w UTC).
WARSAW_TZ = ZoneInfo("Europe/Warsaw")


def warsaw_now() -> datetime:
    """Aktualny czas w strefie Europe/Warsaw."""
    return datetime.now(WARSAW_TZ)


def warsaw_today() -> date:
    """Dzisiejsza data w strefie Europe/Warsaw."""
    return warsaw_now().date()

try:
    import holidays as _holidays
except ImportError:  # pragma: no cover - fallback gdy brak zależności
    _holidays = None

try:
    from dateutil.easter import easter as _easter  # zależność pandas — zawsze dostępna
except ImportError:  # pragma: no cover
    _easter = None


# Kalendarze tworzone leniwie i cache'owane (rozszerzają się o kolejne lata w locie)
_US_CAL = _holidays.financial_holidays("NYSE") if _holidays else None
_PL_CAL = _holidays.country_holidays("PL") if _holidays else None

_MARKETS = ("US", "PL")


def _calendar(market: str):
    return _US_CAL if market == "US" else _PL_CAL


def _gpw_extra_closure(d: date) -> Optional[str]:
    """Dni bez sesji GPW, które NIE są świętami państwowymi (brak ich w bibliotece holidays):
    Wielki Piątek, Wigilia (do 2024 — od 2025 to święto państwowe) oraz Sylwester."""
    if _easter is not None and d == _easter(d.year) - timedelta(days=2):
        return "Wielki Piątek (dzień bez sesji GPW)"
    if d.month == 12 and d.day == 24 and d.year < 2025:
        return "Wigilia (dzień bez sesji GPW)"
    if d.month == 12 and d.day == 31:
        return "Sylwester (dzień bez sesji GPW)"
    return None


def holiday_name(market: str, d: Optional[date] = None) -> Optional[str]:
    """Zwraca nazwę święta dla danego rynku, jeśli to dzień świąteczny — inaczej None."""
    d = d or warsaw_today()
    if market == "PL":
        extra = _gpw_extra_closure(d)
        if extra:
            return extra
    cal = _calendar(market)
    if cal is None:
        return None
    return cal.get(d)


def is_market_open(market: str, d: Optional[date] = None) -> bool:
    """True jeśli giełda danego rynku ('US'/'PL') jest dziś otwarta (nie weekend i nie święto)."""
    d = d or warsaw_today()
    if d.weekday() >= 5:  # sobota=5, niedziela=6
        return False
    return holiday_name(market, d) is None


def market_status(d: Optional[date] = None) -> dict:
    """Zwraca status wszystkich rynków, np.:
    {"US": {"open": False, "holiday": "Independence Day"},
     "PL": {"open": True,  "holiday": None}}
    """
    d = d or warsaw_today()
    status = {}
    for market in _MARKETS:
        name = holiday_name(market, d)
        is_weekend = d.weekday() >= 5
        status[market] = {
            "open": (not is_weekend) and name is None,
            "holiday": name,
            "weekend": is_weekend,
        }
    return status
