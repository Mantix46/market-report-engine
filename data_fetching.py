import yfinance as yf
from datetime import datetime, timedelta
from typing import Optional
import os
import time
import concurrent.futures
import numpy as np
import pandas as pd
import io
import requests
import json
import sys

import logging

from market_calendar import warsaw_today

logger = logging.getLogger(__name__)

FMP_API_KEY = os.getenv("FMP_API_KEY") or os.getenv("FINANCIAL_MODELING_PREP_API_KEY")
if FMP_API_KEY in (None, "", "your_financial_modeling_prep_api_key_here", "WKLEJ_KLUCZ_API"):
    FMP_API_KEY = None

# ============================================================
# Publiczna konfiguracja przykładowa (nadpisywana przez prywatny watchlists.json)
# ============================================================

US_TICKERS = ["AAPL", "MSFT"]
GPW_TICKERS_MAP = {
    "PKO.WA": "PKO BP",
}
GPW_NEWS_QUERIES = {
    "PKO.WA": "PKO BP akcje",
}
INDEX_TICKERS = {
    "S&P 500": "^GSPC",
    "NASDAQ 100": "^NDX",
    "WIG20 (Proxy EPOL)": "EPOL",
}

# Benchmarki do liczenia alpha portfela — SEKTOROWE, nie szeroki rynek:
# portfel US to semi/AI-hardware, wiec alpha vs S&P mierzylaby glownie "czy semis > rynek".
US_BENCHMARK = "SMH"   # VanEck Semiconductor ETF
PL_BENCHMARK = "EPOL"  # iShares MSCI Poland

# 100 zweryfikowanych i działających tickerów GPW
GPW_100_TICKERS = [
    # WIG20 (20)
    'ALE.WA', 'ALR.WA', 'BDX.WA', 'CDR.WA', 'CPS.WA', 'DNP.WA', 'JSW.WA', 'KGH.WA', 'KRU.WA', 'LPP.WA',
    'MBK.WA', 'OPL.WA', 'PCO.WA', 'PEO.WA', 'PGE.WA', 'PKO.WA', 'PKN.WA', 'PZU.WA', 'SPL.WA', 'TPE.WA',
    # mWIG40 (40)
    '11B.WA', 'ABE.WA', 'ACP.WA', 'APR.WA', 'ASB.WA', 'BFT.WA', 'BHW.WA', 'CBF.WA', 'DOM.WA', 'DVL.WA',
    'EAT.WA', 'ENA.WA', 'ENP.WA', 'EUR.WA', 'GPP.WA', 'GPW.WA', 'GTC.WA', 'HUG.WA', 'ING.WA', 'CAR.WA',
    'LWB.WA', 'MAB.WA', 'MIL.WA', 'NEU.WA', 'PEP.WA', 'PKP.WA', 'RVU.WA', 'SLV.WA', 'TEN.WA', 'TXT.WA',
    'VRG.WA', 'WPL.WA', 'XTB.WA', 'ZEP.WA', 'DAT.WA', 'COG.WA', 'TOR.WA', 'UNT.WA', 'VOX.WA', 'WAS.WA',
    # sWIG80 (40)
    'ALG.WA', 'AMB.WA', 'APT.WA', 'AST.WA', 'ATC.WA', 'ATG.WA', 'ATR.WA', 'BBT.WA', 'BCS.WA', 'BOW.WA',
    'BRS.WA', 'CAP.WA', 'CIG.WA', 'CLN.WA', 'CPL.WA', 'DAD.WA', 'DEK.WA', 'DGA.WA', 'EAH.WA', 'EDI.WA',
    'EEX.WA', 'ELT.WA', 'ENT.WA', 'FTE.WA', 'GOP.WA', 'GRN.WA', 'HEL.WA', 'KGN.WA', 'LBW.WA', 'MCI.WA',
    'MLS.WA', 'PCR.WA', 'PNT.WA', 'PHN.WA', 'PXM.WA', 'VOT.WA', 'WLT.WA', 'ZUE.WA', 'SNK.WA', 'SGN.WA'
]

RADAR_US = ["NVDA", "AMZN", "INTC", "CSCO", "ANET", "AMD", "TSLA", "META", "GOOG", "MSFT"]
RADAR_GPW = GPW_100_TICKERS

RADAR_SMALLCAPS = ["SOFI", "PLTR", "IONQ"]

# Noty ostrzegawcze per ticker (nadpisywane przez watchlists.json -> TICKER_NOTES)
TICKER_NOTES = {}

# Wczytywanie z watchlists.json
watchlist_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlists.json")
if os.path.exists(watchlist_path):
    try:
        with open(watchlist_path, "r", encoding="utf-8") as f:
            config_data = json.load(f)
            US_TICKERS = config_data.get("US_TICKERS", US_TICKERS)
            GPW_TICKERS_MAP = config_data.get("GPW_TICKERS_MAP", GPW_TICKERS_MAP)
            GPW_NEWS_QUERIES = config_data.get("GPW_NEWS_QUERIES", GPW_NEWS_QUERIES)
            RADAR_SMALLCAPS = config_data.get("RADAR_SMALLCAPS", RADAR_SMALLCAPS)
            TICKER_NOTES = config_data.get("TICKER_NOTES", TICKER_NOTES)
    except Exception as e:
        logger.warning(f"Błąd wczytywania watchlists.json: {e}. Używam wbudowanej konfiguracji.")

TICKER_NAMES = {
    "AAPL": "Apple",
    "FPS": "Forgent Power Solutions",
    "NVDA": "NVIDIA",
    "AMZN": "Amazon",
    "MU": "Micron Technology",
    "AAOI": "Applied Optoelectronics",
    "XTB.WA": "XTB S.A.",
    "ZAB.WA": "Zabka Group",
    "CBF.WA": "cyber_Folks",
    "INTC": "Intel",
    "CSCO": "Cisco Systems",
    "ANET": "Arista Networks",
    "AMD": "AMD",
    "TSLA": "Tesla",
    "META": "Meta Platforms",
    "GOOG": "Alphabet",
    "MSFT": "Microsoft",
    "DNP.WA": "Dino Polska",
    "KGH.WA": "KGHM",
    "CDR.WA": "CD Projekt",
    "PKO.WA": "PKO BP",
    "BFT.WA": "Benefit Systems",
    "PEO.WA": "Bank Pekao",
    "NBIS": "Nebius Group",
    "AMKR": "Amkor Technology",
}

for t, name in GPW_TICKERS_MAP.items():
    if t not in TICKER_NAMES:
        TICKER_NAMES[t] = name

MACRO_TICKERS = {
    "VIX": "^VIX",
    "US 10Y Treasury": "^TNX",
    "USD/PLN": "USDPLN=X",
}

# ============================================================
# Pobieranie danych przez yfinance (z Cache i Retry)
# ============================================================

def clean_history(ticker: str, hist: pd.DataFrame, t: Optional[yf.Ticker] = None) -> pd.DataFrame:
    """
    Czyszczenie i uzupełnianie historii notowań z yfinance.
    Jeśli ostatni wiersz w DataFrame ma Close jako NaN, próbujemy uzupełnić go
    przy użyciu t.fast_info['lastPrice'] (lub pobieramy nowy Ticker).
    Dzięki temu wiersz z wczorajszą/dzisiejszą sesją nie zostanie utracony.
    """
    if hist.empty:
        return hist
    
    # Jeśli ostatni wiersz ma NaN w Close
    if pd.isna(hist["Close"].iloc[-1]):
        try:
            if t is None:
                t = yf.Ticker(ticker)
            last_price = t.fast_info.get("lastPrice")
            if last_price is not None and not pd.isna(last_price) and last_price > 0:
                idx = hist.index[-1]
                hist.loc[idx, "Close"] = last_price
                # Uzupełnij Open, High, Low jeśli są NaN
                for col in ["Open", "High", "Low"]:
                    if col in hist.columns and pd.isna(hist[col].iloc[-1]):
                        hist.loc[idx, col] = last_price
                # Jeśli Volume jest NaN lub 0, spróbujmy uzupełnić z fast_info['lastVolume']
                if "Volume" in hist.columns and (pd.isna(hist["Volume"].iloc[-1]) or hist["Volume"].iloc[-1] == 0):
                    last_vol = t.fast_info.get("lastVolume")
                    if last_vol is not None and not pd.isna(last_vol):
                        hist.loc[idx, "Volume"] = last_vol
        except Exception as e:
            logger.warning(f"Nie udało się uzupełnić ostatniego wiersza dla {ticker}: {e}")
            
    # Po ewentualnym uzupełnieniu, jeśli nadal są jakieś NaN w Close, usuwamy je
    hist = hist.dropna(subset=["Close"])
    return hist


def fetch_quote(ticker: str, period: str = "5d") -> dict:
    """Pobiera surowe dane notowania jednej spółki z yfinance."""
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period=period)
        
        hist = clean_history(ticker, hist, t)
        
        if hist.empty:
            return {"symbol": ticker, "error": True}
        
        latest_close = hist["Close"].iloc[-1]
        
        if len(hist) >= 2:
            prev_close = hist["Close"].iloc[-2]
            change_pct = ((latest_close - prev_close) / prev_close) * 100
        else:
            change_pct = 0.0
            prev_close = None
        
        volume = hist["Volume"].iloc[-1] if "Volume" in hist.columns else 0
        
        latest_date = hist.index[-1].date()
        today = warsaw_today()
        # Liczymy dni ROBOCZE, nie kalendarzowe — piątkowe dane w poniedziałek nie są "stale"
        is_stale = int(np.busday_count(latest_date, today)) > 2
        
        return {
            "symbol": ticker,
            "price": round(latest_close, 2),
            "change_pct": round(change_pct, 2),
            "prev_close": round(prev_close, 2) if prev_close is not None else None,
            "volume": int(volume),
            "error": False,
            "data_date": str(latest_date),
            "is_stale": is_stale,
        }
    except Exception as e:
        logger.warning(f"Błąd pobierania {ticker}: {e}")
        return {"symbol": ticker, "error": True}


class TickerCache:
    """Cache + retry dla danych tickerów z rate limiting."""
    def __init__(self, max_retries: int = 3, backoff: float = 2.0):
        self._cache = {}
        self.max_retries = max_retries
        self.backoff = backoff
    
    def fetch(self, ticker: str, period: str = "5d") -> dict:
        key = f"{ticker}_{period}"
        if key in self._cache:
            return self._cache[key]
        
        for attempt in range(self.max_retries):
            result = fetch_quote(ticker, period)
            if not result.get("error"):
                self._cache[key] = result
                return result
            if attempt < self.max_retries - 1:
                wait = self.backoff * (attempt + 1)
                logger.info(f"Re-trying {ticker} (attempt {attempt+1}/{self.max_retries}) in {wait}s...")
                time.sleep(wait)
        
        self._cache[key] = result  # cache even if error
        return result
    
    def fetch_batch(self, tickers: list[str], period: str = "5d") -> dict:
        """Pobiera dane dla listy tickerów (wykorzystuje ThreadPoolExecutor oraz cache)."""
        results = {}
        uncached = []
        for t in tickers:
            key = f"{t}_{period}"
            if key in self._cache:
                results[t] = self._cache[key]
            else:
                uncached.append(t)
        
        if uncached:
            fetched = {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                future_to_ticker = {executor.submit(self.fetch, t, period): t for t in uncached}
                for future in concurrent.futures.as_completed(future_to_ticker):
                    t = future_to_ticker[future]
                    try:
                        fetched[t] = future.result()
                    except Exception:
                        fetched[t] = {"symbol": t, "error": True}
            
            for t, data in fetched.items():
                self._cache[f"{t}_{period}"] = data
                results[t] = data
                
        return results


GLOBAL_CACHE = TickerCache()


def fetch_quote_cached(ticker: str, period: str = "5d") -> dict:
    """Pobiera dane notowania z użyciem cache."""
    return GLOBAL_CACHE.fetch(ticker, period)


def fetch_quotes_batch(tickers: list[str], period: str = "5d") -> dict:
    """Pobiera notowania dla listy tickerów z użyciem cache (asynchronicznie)."""
    return GLOBAL_CACHE.fetch_batch(tickers, period)


# Kandydaci na realny indeks WIG20 w yfinance (kolejność prób); fallback: proxy EPOL.
WIG20_CANDIDATES = ["WIG20.WA", "^WIG20"]


def resolve_wig20_ticker() -> tuple[str, str]:
    """Zwraca (etykieta, ticker) dla WIG20: pierwszy kandydat z poprawnym notowaniem
    I wielodniową historią, w razie niepowodzenia proxy EPOL.

    Sama cena nie wystarcza: WIG20.WA potrafi zwracać 1 wiersz historii (zmiana
    dzienna 0.00%, brak 1T/YTD) — wtedy wolimy pełne dane z proxy EPOL."""
    for cand in WIG20_CANDIDATES:
        q = fetch_quote_cached(cand, period="5d")
        if not q.get("error") and q.get("price") and fetch_period_change(cand, "5d") is not None:
            return "WIG20", cand
    return "WIG20 (Proxy EPOL)", "EPOL"


def get_index_tickers_v2() -> dict:
    """Zestaw indeksów dla layoutu v2 (SPX, NDX, SX5E, WIG20).

    Celowo FUNKCJA, nie stała: rozstrzygnięcie tickera WIG20 wymaga sieci,
    więc nie może dziać się przy imporcie. NIE modyfikuje INDEX_TICKERS (v1)."""
    label, wig = resolve_wig20_ticker()
    return {
        "S&P 500": "^GSPC",
        "NASDAQ 100": "^NDX",
        "Euro Stoxx 50": "^STOXX50E",
        label: wig,
    }


def fetch_period_change(ticker: str, period: str = "7d") -> Optional[float]:
    """Zmiana procentowa za CAŁY okres (np. tydzień): ostatnie zamknięcie vs pierwsze.
    W odróżnieniu od change_pct z fetch_quote (który zawsze jest 1-sesyjny)."""
    try:
        t = yf.Ticker(ticker)
        hist = clean_history(ticker, t.history(period=period), t)
        if len(hist) >= 2:
            first = hist["Close"].iloc[0]
            last = hist["Close"].iloc[-1]
            if first and first > 0:
                return round((last / first - 1) * 100, 2)
    except Exception as e:
        logger.warning(f"Blad liczenia zmiany {period} dla {ticker}: {e}")
    return None


def get_top_movers(tickers: list[str], top_n: int = 3, period: str = "5d") -> tuple[list, list]:
    """Zwraca top N zyskujących i tracących."""
    quotes = fetch_quotes_batch(tickers, period)
    valid = [(k, v) for k, v in quotes.items() if not v.get("error")]
    
    sorted_by_change = sorted(valid, key=lambda x: x[1].get("change_pct", 0))
    
    losers = sorted_by_change[:top_n]
    winners = sorted_by_change[-top_n:][::-1]
    
    return winners, losers


def fetch_nasdaq100_tickers() -> list[str]:
    """Pobiera skład NASDAQ-100 dynamicznie z Wikipedii."""
    url = "https://en.wikipedia.org/wiki/Nasdaq-100"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        tables = pd.read_html(io.StringIO(r.text), attrs={"id": "constituents"})
        if tables:
            df = tables[0]
            tickers = df["Ticker"].tolist()
            return sorted(list(set(tickers)))
    except Exception as e:
        logger.warning(f"Błąd pobierania składu NASDAQ-100 z Wiki: {e}. Używam fallbacku.")
    
    # Fallback w razie problemów z siecią
    return ["AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "META", "TSLA", "AVGO", "COST", "PEP",
            "AZN", "CSCO", "TMUS", "ADBE", "QCOM", "TXN", "AMGN", "INTC", "ISRG", "HON"]


def fetch_earnings_dates(tickers: list[str]) -> dict:
    """Pobiera daty najbliższych earnings dla spółek portfelowych."""
    results = {}
    for ticker in tickers:
        try:
            t = yf.Ticker(ticker)
            cal = t.calendar
            if cal and isinstance(cal, dict):
                earnings_dates = cal.get("Earnings Date", [])
                if earnings_dates:
                    next_date = earnings_dates[0]
                    days_until = (next_date - warsaw_today()).days
                    results[ticker] = {
                        "date": str(next_date),
                        "days_until": days_until,
                        "is_range": len(earnings_dates) > 1
                    }
            time.sleep(0.3)  # rate limiting
        except Exception as e:
            logger.warning(f"Błąd pobierania kalendarza dla {ticker}: {e}")
    return results


def fetch_analyst_recommendations(ticker: str) -> dict:
    """Rekomendacje analityków (layout v2): rozkład ocen, ceny docelowe, zmiany 30 dni.

    Każdy blok w osobnym try/except — CI używa yfinance 0.2.x, lokalnie nowsze wersje
    mają inne atrybuty/kształty DataFrame, a tickery GPW zwykle zwracają puste dane."""
    out = {"rec_summary": None, "price_targets": None, "recent_changes": []}
    try:
        t = yf.Ticker(ticker)
    except Exception as e:
        logger.warning(f"Blad tworzenia Ticker dla rekomendacji {ticker}: {e}")
        return out

    # 1. Rozkład ocen (strongBuy/buy/hold/sell/strongSell) z bieżącego miesiąca
    try:
        rec_df = getattr(t, "recommendations_summary", None)
        if rec_df is not None and not rec_df.empty:
            row = rec_df[rec_df["period"] == "0m"] if "period" in rec_df.columns else rec_df.head(1)
            if not row.empty:
                r = row.iloc[0]
                out["rec_summary"] = {
                    k: int(r[col]) for k, col in [
                        ("strong_buy", "strongBuy"), ("buy", "buy"), ("hold", "hold"),
                        ("sell", "sell"), ("strong_sell", "strongSell"),
                    ] if col in row.columns and not pd.isna(r[col])
                }
    except Exception as e:
        logger.debug(f"Brak recommendations_summary dla {ticker}: {e}")

    # 2. Ceny docelowe analityków (fallback: pola z t.info)
    try:
        targets = getattr(t, "analyst_price_targets", None)
        if isinstance(targets, dict) and targets.get("mean"):
            out["price_targets"] = {
                "low": targets.get("low"), "high": targets.get("high"),
                "mean": targets.get("mean"), "median": targets.get("median"),
                "current": targets.get("current"),
            }
    except Exception as e:
        logger.debug(f"Brak analyst_price_targets dla {ticker}: {e}")
    if out["price_targets"] is None:
        try:
            info = t.info
            if info.get("targetMeanPrice"):
                out["price_targets"] = {
                    "low": info.get("targetLowPrice"), "high": info.get("targetHighPrice"),
                    "mean": info.get("targetMeanPrice"), "median": info.get("targetMedianPrice"),
                    "current": info.get("currentPrice"),
                    "analysts": info.get("numberOfAnalystOpinions"),
                }
        except Exception as e:
            logger.debug(f"Brak target price w info dla {ticker}: {e}")

    # 3. Ostatnie zmiany rekomendacji (30 dni, max 5)
    try:
        ud = getattr(t, "upgrades_downgrades", None)
        if ud is not None and not ud.empty:
            cutoff = pd.Timestamp(warsaw_today() - timedelta(days=30))
            if isinstance(ud.index, pd.DatetimeIndex):
                idx = ud.index.tz_localize(None) if ud.index.tz is not None else ud.index
                recent = ud[idx >= cutoff]
            else:
                recent = ud.head(5)
            for idx, row in recent.head(5).iterrows():
                out["recent_changes"].append({
                    "date": str(getattr(idx, "date", lambda: idx)()),
                    "firm": row.get("Firm", ""),
                    "action": row.get("Action", ""),
                    "from_grade": row.get("FromGrade", ""),
                    "to_grade": row.get("ToGrade", ""),
                })
    except Exception as e:
        logger.debug(f"Brak upgrades_downgrades dla {ticker}: {e}")

    time.sleep(0.3)  # rate limiting jak w fetch_earnings_dates
    return out


def fetch_fundamentals_short_insider(ticker: str) -> dict:
    """Fetch fundamentals, short interest and a richer insider/smart-money summary."""
    try:
        t = yf.Ticker(ticker)
        info = t.info

        try:
            price = t.fast_info.get("lastPrice")
        except Exception:
            price = None

        high = info.get("fiftyTwoWeekHigh")
        low = info.get("fiftyTwoWeekLow")
        pct_from_high = round(((price / high - 1) * 100), 2) if price and high else None
        pct_from_low = round(((price / low - 1) * 100), 2) if price and low else None

        insiders = []
        buy_shares_90d = 0
        sell_shares_90d = 0
        buy_value_90d = 0.0
        sell_value_90d = 0.0
        recent_buys_90d = 0
        recent_sales_90d = 0
        cutoff = warsaw_today() - timedelta(days=90)

        try:
            insider_df = t.insider_transactions
            if insider_df is not None and not insider_df.empty:
                for _, row in insider_df.head(12).iterrows():
                    transaction = str(row.get("Transaction", "") or "")
                    shares = row.get("Shares", 0) or 0
                    try:
                        shares = int(shares)
                    except Exception:
                        shares = 0

                    # Wartość transakcji — sygnał ważymy pieniędzmi, nie sztukami
                    # (10 000 akcji po $2 to nie to samo co 10 000 po $500)
                    value = row.get("Value")
                    try:
                        value = float(value) if value is not None and not pd.isna(value) else None
                    except Exception:
                        value = None
                    if not value and shares and price:
                        value = shares * price

                    raw_date = row.get("Start Date", "")
                    parsed_date = pd.to_datetime(raw_date, errors="coerce")
                    is_recent = bool(not pd.isna(parsed_date) and parsed_date.date() >= cutoff)
                    transaction_l = transaction.lower()

                    if "sale" in transaction_l or "sell" in transaction_l:
                        if is_recent:
                            sell_shares_90d += shares
                            sell_value_90d += value or 0.0
                            recent_sales_90d += 1
                    elif "purchase" in transaction_l or "buy" in transaction_l:
                        if is_recent:
                            buy_shares_90d += shares
                            buy_value_90d += value or 0.0
                            recent_buys_90d += 1

                    insiders.append({
                        "insider": row.get("Insider", ""),
                        "position": row.get("Position", ""),
                        "transaction": transaction,
                        "shares": shares,
                        "value": round(value, 0) if value else None,
                        "date": str(raw_date),
                    })
        except Exception:
            pass

        net_shares_90d = buy_shares_90d - sell_shares_90d
        # Sygnał na podstawie WARTOŚCI transakcji (nie liczby akcji)
        insider_signal = "neutral"
        if recent_sales_90d >= 2 and sell_value_90d > max(buy_value_90d, 0.0):
            insider_signal = "recent_selling_pressure"
        elif recent_buys_90d >= 1 and buy_value_90d > sell_value_90d:
            insider_signal = "recent_buying_support"

        # Rozszerzona wycena (layout v2) — te same dane z już pobranego t.info,
        # zero dodatkowych zapytań. Debt/EBITDA tylko przy dodatniej EBITDA.
        total_debt = info.get("totalDebt")
        ebitda = info.get("ebitda")
        debt_to_ebitda = (
            round(total_debt / ebitda, 2)
            if total_debt is not None and ebitda and ebitda > 0 else None
        )
        fcf = info.get("freeCashflow")
        mcap = info.get("marketCap")
        fcf_yield_pct = round(fcf / mcap * 100, 2) if fcf is not None and mcap else None

        return {
            "symbol": ticker,
            "forward_pe": info.get("forwardPE"),
            "price_to_sales": info.get("priceToSalesTrailing12Months"),
            "market_cap": info.get("marketCap"),
            "trailing_pe": info.get("trailingPE"),
            "peg_ratio": info.get("trailingPegRatio") or info.get("pegRatio"),
            "ev_to_ebitda": info.get("enterpriseToEbitda"),
            "price_to_book": info.get("priceToBook"),
            "return_on_equity": info.get("returnOnEquity"),
            "profit_margin": info.get("profitMargins"),
            "operating_margin": info.get("operatingMargins"),
            "debt_to_ebitda": debt_to_ebitda,
            "fcf_yield_pct": fcf_yield_pct,
            "pct_from_52w_high": pct_from_high,
            "pct_from_52w_low": pct_from_low,
            "short_pct_float": info.get("shortPercentOfFloat"),
            "short_ratio": info.get("shortRatio"),
            "insider_transactions": insiders[:5],
            "insider_summary": {
                "buy_shares_90d": buy_shares_90d,
                "sell_shares_90d": sell_shares_90d,
                "buy_value_90d": round(buy_value_90d, 0),
                "sell_value_90d": round(sell_value_90d, 0),
                "net_shares_90d": net_shares_90d,
                "recent_buys_90d": recent_buys_90d,
                "recent_sales_90d": recent_sales_90d,
            },
            "insider_signal": insider_signal,
        }
    except Exception as e:
        logger.warning(f"Blad fundamentals dla {ticker}: {e}")
        return {
            "symbol": ticker,
            "forward_pe": None,
            "price_to_sales": None,
            "market_cap": None,
            "trailing_pe": None,
            "peg_ratio": None,
            "ev_to_ebitda": None,
            "price_to_book": None,
            "return_on_equity": None,
            "profit_margin": None,
            "operating_margin": None,
            "debt_to_ebitda": None,
            "fcf_yield_pct": None,
            "pct_from_52w_high": None,
            "pct_from_52w_low": None,
            "short_pct_float": None,
            "short_ratio": None,
            "insider_transactions": [],
            "insider_summary": {},
            "insider_signal": "neutral",
        }


# Publikacje makro US śledzone w FRED (release_id → etykieta w raporcie).
# Kalendarz makro FMP wymaga płatnego planu, więc korzystamy z darmowego API FRED.
FRED_RELEASES = {
    10: "CPI (inflacja US)",
    46: "PPI (ceny producentow US)",
    50: "Payrolls (raport z rynku pracy US)",
    53: "GDP (PKB US)",
    54: "PCE (inflacja PCE US)",
    9: "Sprzedaz detaliczna US",
}

# Daty DECYZJI (ostatni dzień posiedzenia) wg oficjalnych harmonogramów:
# FOMC: federalreserve.gov/monetarypolicy/fomccalendars.htm; RPP: nbp.pl (harmonogram 2026).
# Do uzupełnienia o kolejny rok, gdy banki centralne opublikują harmonogramy.
MACRO_MEETINGS = {
    "US": ("Decyzja FOMC ws. stop procentowych (Fed)", [
        "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
        "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
    ]),
    "PL": ("Decyzja RPP ws. stop procentowych (NBP)", [
        "2026-01-14", "2026-02-04", "2026-03-04", "2026-04-09",
        "2026-05-06", "2026-06-10", "2026-07-08", "2026-09-02",
        "2026-10-07", "2026-11-04", "2026-12-02",
    ]),
}


def fetch_macro_calendar(days_ahead: int = 10, countries: Optional[list[str]] = None) -> list[dict]:
    """Nadchodzące wydarzenia makro: publikacje US z FRED + stały harmonogram FOMC/RPP."""
    countries = countries or ["US", "PL"]
    date_from = warsaw_today()
    date_to = date_from + timedelta(days=days_ahead)
    events = []

    fred_key = os.getenv("FRED_API_KEY")
    if fred_key and "US" in countries:
        url = "https://api.stlouisfed.org/fred/releases/dates"
        params = {
            "api_key": fred_key,
            "file_type": "json",
            "realtime_start": str(date_from),
            "realtime_end": str(date_to),
            "include_release_dates_with_no_data": "true",
            "sort_order": "asc",
            "limit": 1000,
        }
        try:
            r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            seen = set()
            for item in r.json().get("release_dates", []):
                label = FRED_RELEASES.get(item.get("release_id"))
                date_val = item.get("date")
                if not label or not date_val or (date_val, label) in seen:
                    continue
                seen.add((date_val, label))
                events.append({
                    "date": date_val,
                    "country": "US",
                    "event": label,
                    "impact": "high",
                    "source": "fred",
                })
        except Exception as e:
            logger.warning(f"Blad pobierania kalendarza makro z FRED: {e}")
    elif not fred_key:
        logger.info("Brak FRED_API_KEY — kalendarz makro bez publikacji US z FRED.")

    # Posiedzenia banków centralnych ze stałego harmonogramu (FRED ich nie kalendarzuje sensownie)
    for country, (name, dates) in MACRO_MEETINGS.items():
        if country not in countries:
            continue
        for d in dates:
            if str(date_from) <= d <= str(date_to):
                events.append({
                    "date": d,
                    "country": country,
                    "event": name,
                    "impact": "high",
                    "source": "schedule",
                })

    events.sort(key=lambda e: e.get("date") or "")

    if not events:
        events = [{
            "date_range": f"{date_from} - {date_to}",
            "country": "US/PL",
            "event": "Brak dopasowanych wydarzen (lub brak FRED_API_KEY). Sprawdz recznie CPI, FOMC/FED, NBP/RPP, payrolls, PCE i GDP.",
            "impact": "watchlist",
            "source": "fallback_watchlist",
        }]

    return events[:12]


def fetch_earnings_call_context(tickers: list[str], max_items: int = 2) -> dict:
    """Fetch earnings-call transcript context when possible, otherwise use guidance-related news."""
    global FMP_API_KEY
    results = {}
    for ticker in tickers:
        items = []

        if FMP_API_KEY:
            try:
                symbol = ticker.replace(".WA", "")
                transcript_url = f"https://financialmodelingprep.com/api/v3/earning_call_transcript/{symbol}"
                params = {"limit": max_items, "apikey": FMP_API_KEY}
                r = requests.get(transcript_url, params=params, timeout=12)
                if r.status_code in (401, 402, 403):
                    logger.info(f"Wyłączam FMP API dla transkryptów (status {r.status_code}: {r.text[:120]})")
                    FMP_API_KEY = None
                elif r.ok:
                    for item in (r.json() or [])[:max_items]:
                        content = item.get("content") or ""
                        items.append({
                            "date": item.get("date"),
                            "quarter": item.get("quarter"),
                            "year": item.get("year"),
                            "summary": content[:700],
                            "source": "financialmodelingprep_transcript",
                        })
            except Exception as e:
                if FMP_API_KEY:
                    logger.warning(f"Blad pobierania transkryptu earnings dla {ticker}: {e}")

        if not items:
            try:
                t = yf.Ticker(ticker)
                raw_news = t.news or []
                keywords = ("earnings call", "transcript", "guidance", "outlook", "results", "earnings")
                for item in raw_news:
                    content = item.get("content", {})
                    title = content.get("title") or item.get("title", "")
                    summary = content.get("summary", "")
                    haystack = f"{title} {summary}".lower()
                    if not any(keyword in haystack for keyword in keywords):
                        continue
                    link = item.get("link") or content.get("clickThroughUrl", {}).get("url") or content.get("canonicalUrl", {}).get("url")
                    items.append({
                        "title": title,
                        "summary": summary[:500] if summary else "",
                        "url": link or "",
                        "source": "yahoo_finance_news",
                    })
                    if len(items) >= max_items:
                        break
            except Exception as e:
                logger.warning(f"Brak kontekstu earnings call dla {ticker}: {e}")

        if items:
            results[ticker] = items

    return results
