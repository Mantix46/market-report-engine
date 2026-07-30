"""
news_fetcher.py — Moduł pobierania newsów z wielu źródeł

Obsługuje:
  1. Google News RSS — polskojęzyczne newsy dla spółek GPW (darmowe, bez klucza API)
  2. yfinance — newsy anglojęzyczne dla spółek US (istniejące rozwiązanie)

Użycie:
  from news_fetcher import fetch_all_news
  news = fetch_all_news(gpw_tickers_map, gpw_news_queries, us_tickers, max_per_ticker=3)
"""

import time
import xml.etree.ElementTree as ET
from datetime import datetime
from html import unescape
from typing import Optional
from urllib.parse import quote_plus

import requests
import yfinance as yf

import logging

logger = logging.getLogger(__name__)


# ============================================================
# Cache — unikamy wielokrotnych zapytań w jednym uruchomieniu
# ============================================================

_news_cache: dict[str, list[dict]] = {}


def _cache_key(source: str, query: str) -> str:
    return f"{source}:{query}"


# ============================================================
# Google News RSS — polskojęzyczne newsy
# ============================================================

_GOOGLE_NEWS_RSS_URL = (
    "https://news.google.com/rss/search"
    "?q={query}+when:7d"
    "&hl=pl&gl=PL&ceid=PL:pl"
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )
}


def _strip_html(text: str) -> str:
    """Usuwa tagi HTML z tekstu (prosty regex-free)."""
    result = []
    in_tag = False
    for ch in text:
        if ch == "<":
            in_tag = True
        elif ch == ">":
            in_tag = False
        elif not in_tag:
            result.append(ch)
    return unescape("".join(result)).strip()


def _parse_pub_date(date_str: str) -> str:
    """Parsuje datę z RSS do czytelnego formatu."""
    try:
        # Format RSS: "Tue, 10 Jun 2026 08:30:00 GMT"
        dt = datetime.strptime(date_str, "%a, %d %b %Y %H:%M:%S %Z")
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return date_str or ""


def fetch_google_news_rss(
    query: str,
    max_results: int = 5,
) -> list[dict]:
    """
    Pobiera najnowsze newsy z Google News RSS dla podanego zapytania.
    
    Args:
        query: Fraza wyszukiwania (np. "XTB GPW", "Żabka")
        max_results: Maks. liczba wyników
        
    Returns:
        Lista słowników z kluczami: title, publisher, date, summary, url
    """
    cache_k = _cache_key("google_rss", query)
    if cache_k in _news_cache:
        return _news_cache[cache_k][:max_results]

    encoded_query = quote_plus(query)
    url = _GOOGLE_NEWS_RSS_URL.format(query=encoded_query)

    articles = []
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=15)
        resp.raise_for_status()

        root = ET.fromstring(resp.content)

        # RSS struktura: <rss><channel><item>...</item></channel></rss>
        channel = root.find("channel")
        if channel is None:
            logger.warning(f"Google News RSS: brak <channel> w odpowiedzi dla '{query}'")
            _news_cache[cache_k] = []
            return []

        items = channel.findall("item")

        for item in items[:max_results]:
            title_el = item.find("title")
            link_el = item.find("link")
            pub_date_el = item.find("pubDate")
            source_el = item.find("source")
            desc_el = item.find("description")

            title = title_el.text if title_el is not None and title_el.text else ""
            link = link_el.text if link_el is not None and link_el.text else ""
            pub_date = pub_date_el.text if pub_date_el is not None and pub_date_el.text else ""
            publisher = source_el.text if source_el is not None and source_el.text else ""
            description = desc_el.text if desc_el is not None and desc_el.text else ""

            # Oczyszczenie opisu z HTML
            summary = _strip_html(description)[:200] if description else ""

            if title:
                articles.append({
                    "title": title,
                    "publisher": publisher,
                    "date": _parse_pub_date(pub_date),
                    "summary": summary,
                    "url": link,
                    "source": "Google News",
                })

    except requests.exceptions.Timeout:
        logger.warning(f"Google News RSS: timeout dla zapytania '{query}'")
    except requests.exceptions.RequestException as e:
        logger.warning(f"Google News RSS: błąd sieci dla '{query}': {e}")
    except ET.ParseError as e:
        logger.warning(f"Google News RSS: błąd parsowania XML dla '{query}': {e}")
    except Exception as e:
        logger.warning(f"Google News RSS: nieoczekiwany błąd dla '{query}': {e}")

    _news_cache[cache_k] = articles
    return articles[:max_results]


# ============================================================
# yfinance — newsy dla spółek US
# ============================================================

def fetch_yfinance_news(ticker: str, max_per_ticker: int = 3) -> list[dict]:
    """
    Pobiera najnowsze newsy dla tickera z Yahoo Finance (yfinance).
    
    Działa dobrze dla spółek US, słabo/wcale dla spółek GPW.
    """
    cache_k = _cache_key("yfinance", ticker)
    if cache_k in _news_cache:
        return _news_cache[cache_k][:max_per_ticker]

    articles = []
    try:
        t = yf.Ticker(ticker)
        raw_news = t.news or []
        for item in raw_news[:max_per_ticker]:
            content = item.get("content", {})
            title = content.get("title") or item.get("title", "")
            publisher = (
                content.get("provider", {}).get("displayName")
                or item.get("publisher", "")
            )
            pub_date = content.get("pubDate") or item.get("providerPublishTime", "")
            summary = content.get("summary", "")

            # Link — próbuj z różnych pól (zależnie od wersji yfinance)
            link = item.get("link")
            if not link:
                link = (
                    content.get("clickThroughUrl", {}).get("url")
                    or content.get("canonicalUrl", {}).get("url")
                )

            if title:
                articles.append({
                    "title": title,
                    "publisher": publisher,
                    "date": str(pub_date),
                    "summary": summary[:200] if summary else "",
                    "url": link or "",
                    "source": "Yahoo Finance",
                })
    except Exception as e:
        logger.warning(f"yfinance news: brak newsów dla {ticker}: {e}")

    _news_cache[cache_k] = articles
    return articles[:max_per_ticker]


# ============================================================
# Główna funkcja — łączy wszystkie źródła
# ============================================================

def fetch_all_news(
    gpw_tickers_map: dict[str, str],
    gpw_news_queries: dict[str, str],
    us_tickers: list[str],
    extra_tickers: Optional[list[str]] = None,
    max_per_ticker: int = 3,
) -> dict[str, list[dict]]:
    """
    Pobiera newsy dla wszystkich spółek z portfela.
    
    - Spółki GPW → Google News RSS (polskojęzyczne newsy)
    - Spółki US → yfinance (anglojęzyczne newsy)
    - Extra tickers (np. top movers) → yfinance
    
    Args:
        gpw_tickers_map: Mapa tickerów GPW → nazwa spółki
        gpw_news_queries: Mapa tickerów GPW → fraza wyszukiwania Google News
        us_tickers: Lista tickerów US
        extra_tickers: Opcjonalne dodatkowe tickery (top movers)
        max_per_ticker: Maks. liczba newsów na ticker
        
    Returns:
        Dict: ticker → lista artykułów
    """
    all_news: dict[str, list[dict]] = {}

    # --- GPW: Google News RSS ---
    for ticker in gpw_tickers_map:
        query = gpw_news_queries.get(ticker)
        if not query:
            # Fallback: użyj nazwy spółki z mapy
            query = gpw_tickers_map[ticker]

        logger.info(f"Pobieram polskie newsy dla {ticker} (query: '{query}')...")
        articles = fetch_google_news_rss(query, max_results=max_per_ticker)

        if articles:
            all_news[ticker] = articles
        else:
            # Fallback: spróbuj yfinance
            logger.info(f"Brak wyników z Google News dla {ticker}, próbuję yfinance...")
            yf_articles = fetch_yfinance_news(ticker, max_per_ticker)
            if yf_articles:
                all_news[ticker] = yf_articles

        # Rate limiting — nie bombardujemy Google News
        time.sleep(0.5)

    # --- US: yfinance ---
    for ticker in us_tickers:
        logger.info(f"Pobieram newsy US dla {ticker}...")
        articles = fetch_yfinance_news(ticker, max_per_ticker)
        if articles:
            all_news[ticker] = articles

    # --- Extra tickers (top movers, indeksy, makro) ---
    if extra_tickers:
        for ticker in extra_tickers:
            if ticker in all_news:
                continue  # Już pobrany
            # Sprawdź czy to ticker GPW
            if ticker.endswith(".WA"):
                query = gpw_news_queries.get(ticker, ticker.replace(".WA", ""))
                articles = fetch_google_news_rss(query, max_results=max_per_ticker)
                if articles:
                    all_news[ticker] = articles
                time.sleep(0.3)
            else:
                articles = fetch_yfinance_news(ticker, max_per_ticker)
                if articles:
                    all_news[ticker] = articles

    logger.info(f"Łącznie pobrano newsy dla {len(all_news)} tickerów.")
    return all_news
