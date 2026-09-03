"""
news_fetcher.py — Moduł pobierania newsów z wielu źródeł

Obsługuje:
  1. Google News RSS — polskojęzyczne newsy dla spółek GPW (darmowe, bez klucza API)
  2. yfinance — newsy anglojęzyczne dla spółek US (istniejące rozwiązanie)

Użycie:
  from news_fetcher import fetch_all_news
  news = fetch_all_news(gpw_tickers_map, gpw_news_queries, us_tickers, max_per_ticker=3)
"""

import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
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


def parse_article_datetime(date_val) -> Optional[datetime]:
    """Normalizuje datę artykułu (RSS, ISO, unix) do datetime UTC-aware."""
    if date_val in (None, ""):
        return None
    if isinstance(date_val, (int, float)):
        try:
            ts = float(date_val)
            if ts > 1e12:
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    text = str(date_val).strip()
    if text.isdigit():
        return parse_article_datetime(int(text))
    iso = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%a, %d %b %Y %H:%M:%S %z",
    ):
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def _normalize_title(title: str) -> str:
    return re.sub(r"\W+", " ", (title or "").lower()).strip()


def filter_articles(
    articles: list[dict],
    max_age_hours: Optional[int] = 36,
    max_keep: int = 3,
    now: Optional[datetime] = None,
) -> list[dict]:
    """Odrzuca stare i zduplikowane newsy; sortuje od najnowszych.

    Brak daty nie dyskwalifikuje artykułu (trafia na koniec).
    """
    if not articles:
        return []
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now - timedelta(hours=max_age_hours) if max_age_hours else None

    dated = []
    for article in articles:
        dt = parse_article_datetime(article.get("date"))
        if cutoff is not None and dt is not None and dt < cutoff:
            continue
        dated.append((dt, article))

    dated.sort(key=lambda item: item[0] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    seen: set[str] = set()
    out = []
    for _dt, article in dated:
        key = _normalize_title(article.get("title") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(article)
        if len(out) >= max_keep:
            break
    return out


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
            item = item or {}
            content = item.get("content") or {}
            if not isinstance(content, dict):
                content = {}
            title = content.get("title") or item.get("title", "")
            provider = content.get("provider") or {}
            publisher = (
                provider.get("displayName") if isinstance(provider, dict) else ""
            ) or item.get("publisher", "")
            pub_date = content.get("pubDate") or item.get("providerPublishTime", "")
            summary = content.get("summary") or ""

            link = item.get("link")
            if not link:
                click = content.get("clickThroughUrl") or {}
                canon = content.get("canonicalUrl") or {}
                link = (
                    (click.get("url") if isinstance(click, dict) else None)
                    or (canon.get("url") if isinstance(canon, dict) else None)
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
    max_age_hours: Optional[int] = 36,
) -> dict[str, list[dict]]:
    """
    Pobiera newsy dla wszystkich spółek z portfela.

    - Spółki GPW → Google News RSS (polskojęzyczne newsy)
    - Spółki US → yfinance (anglojęzyczne newsy)
    - Extra tickers (np. top movers) → yfinance

    Pobiera zapas artykułów, potem filtruje po świeżości i de-duplikuje tytuły.
    """
    all_news: dict[str, list[dict]] = {}
    fetch_n = max(max_per_ticker * 3, 8)

    def _keep(articles: list[dict]) -> list[dict]:
        return filter_articles(articles, max_age_hours=max_age_hours, max_keep=max_per_ticker)

    # --- GPW: Google News RSS ---
    for ticker in gpw_tickers_map:
        query = gpw_news_queries.get(ticker)
        if not query:
            query = gpw_tickers_map[ticker]

        logger.info(f"Pobieram polskie newsy dla {ticker} (query: '{query}')...")
        articles = fetch_google_news_rss(query, max_results=fetch_n)

        if articles:
            all_news[ticker] = _keep(articles)
        else:
            logger.info(f"Brak wyników z Google News dla {ticker}, próbuję yfinance...")
            yf_articles = fetch_yfinance_news(ticker, fetch_n)
            if yf_articles:
                all_news[ticker] = _keep(yf_articles)

        time.sleep(0.5)

    # --- US: yfinance ---
    for ticker in us_tickers:
        logger.info(f"Pobieram newsy US dla {ticker}...")
        articles = fetch_yfinance_news(ticker, fetch_n)
        if articles:
            all_news[ticker] = _keep(articles)

    # --- Extra tickers (top movers, indeksy, makro) ---
    if extra_tickers:
        for ticker in extra_tickers:
            if ticker in all_news:
                continue
            if ticker.endswith(".WA"):
                query = gpw_news_queries.get(ticker, ticker.replace(".WA", ""))
                articles = fetch_google_news_rss(query, max_results=fetch_n)
                if articles:
                    all_news[ticker] = _keep(articles)
                time.sleep(0.3)
            else:
                articles = fetch_yfinance_news(ticker, fetch_n)
                if articles:
                    all_news[ticker] = _keep(articles)

    logger.info(f"Łącznie pobrano newsy dla {len(all_news)} tickerów.")
    return all_news
