import os
from datetime import datetime, timedelta
from typing import Optional

try:
    from google import genai
except ImportError:
    genai = None

from data_fetching import (
    US_TICKERS, GPW_TICKERS_MAP, INDEX_TICKERS, GPW_100_TICKERS, RADAR_SMALLCAPS,
    TICKER_NAMES, MACRO_TICKERS, GPW_NEWS_QUERIES,
    US_BENCHMARK, PL_BENCHMARK,
    fetch_nasdaq100_tickers, fetch_quote_cached, fetch_quotes_batch, get_top_movers,
    fetch_fundamentals_short_insider, fetch_earnings_dates,
    fetch_macro_calendar, fetch_earnings_call_context
)

from news_fetcher import fetch_all_news

from technicals import calc_relative_strength, fetch_technicals

from snapshot_store import load_last_snapshots, save_today_snapshots

from market_calendar import market_status, warsaw_now

import logging

logger = logging.getLogger(__name__)


_MARKET_LABELS = {"US": "USA", "PL": "GPW"}

# Ponizej tego udzialu poprawnych notowan w aktywnym portfelu raport NIE jest generowany
MIN_DATA_QUALITY = 0.5


class DataQualityError(Exception):
    """Zbyt malo poprawnych danych rynkowych, by wygenerowac wiarygodny raport."""


def markets_to_skip(status: dict) -> set:
    """Zwraca zbiór rynków ('US'/'PL') zamkniętych dziś z powodu ŚWIĘTA (w dzień roboczy).
    Weekend pomijamy — raport sobotni celowo podsumowuje piątkową sesję."""
    return {m for m, info in status.items() if info.get("holiday") and not info.get("weekend")}


def market_banner_lines(skip: set, status: dict) -> list[str]:
    """Tworzy linie banera o zamkniętych rynkach (święta)."""
    lines = []
    for market in skip:
        holiday = status.get(market, {}).get("holiday") or "dzień wolny"
        label = _MARKET_LABELS.get(market, market)
        lines.append(
            f"> 🟡 Giełda {label} zamknięta dziś — {holiday}. "
            f"Spółki {label} pominięte w tym raporcie."
        )
    return lines


def active_portfolio_tickers(skip: set) -> list:
    """Lista tickerów portfela z pominięciem rynków zamkniętych dziś (święto)."""
    tickers = []
    if "US" not in skip:
        tickers += list(US_TICKERS)
    if "PL" not in skip:
        tickers += list(GPW_TICKERS_MAP.keys())
    return tickers


def format_change(pct: float) -> str:
    """Formatuje zmianę procentową dla podstawowych tekstów."""
    if pct > 0:
        return f"+{pct:.2f}%"
    elif pct < 0:
        return f"{pct:.2f}%"
    return "0.00%"


def format_pct(val: Optional[float]) -> str:
    """Formatowanie zmiany procentowej w tabeli (usunięto małe wykresiki 📈/📉)."""
    if val is None:
        return "b/d"
    if val > 0:
        return f"**+{val:.2f}%**"
    elif val < 0:
        return f"**{val:.2f}%**"
    return "0.00%"


PLAN_LABELS = {
    "planned": "zaplanowane 10b5-1",
    "discretionary": "nagłe",
    "unknown": "plan: b/d",
    "other": "",
}


def format_money_amount(value, ccy: str) -> str:
    """Kwota insiderów: nigdy '0.00 mln' dla braku obrotu lub drobnicy."""
    if value is None:
        return "b/d"
    try:
        amount = float(value)
    except Exception:
        return "b/d"
    if abs(amount) < 0.5:
        return "brak"
    if abs(amount) >= 1e6:
        return f"{amount / 1e6:.2f} mln {ccy}"
    if abs(amount) >= 1e3:
        return f"{amount / 1e3:.1f} tys. {ccy}"
    return f"{amount:.0f} {ccy}"


def _plan_split_text(buy_or_sell: str, ins: dict, ccy: str) -> str:
    planned = ins.get(f"planned_{buy_or_sell}_value_90d") or 0
    discretionary = ins.get(f"discretionary_{buy_or_sell}_value_90d") or 0
    unknown = ins.get(f"unknown_{buy_or_sell}_value_90d") or 0
    total = ins.get(f"{buy_or_sell}_value_90d") or 0
    if not total:
        return ""
    if planned or discretionary:
        parts = [
            f"{format_money_amount(planned, ccy)} zaplanowane",
            f"{format_money_amount(discretionary, ccy)} nagłe",
        ]
        if unknown:
            parts.append(f"{format_money_amount(unknown, ccy)} b/d")
        return " (z tego " + " / ".join(parts) + ")"
    return " (plan: b/d)"


def format_insider_lines(ticker: str, fund: dict) -> list[str]:
    """Linie insiderów do prompta Gemini — bez fałszywego '0 USD'."""
    ins = fund.get("insider_summary") or {}
    status = fund.get("insider_data_status", "unavailable")
    source = fund.get("insider_data_source", "b/d")
    if status not in ("available", "no_open_market_trades") or not ins:
        return [
            f"      insiderzy 90d: b/d — źródło: {source}; "
            f"status: {status} (to nie jest zero transakcji)"
        ]

    ccy = "PLN" if ticker.endswith(".WA") else "USD"
    if status == "no_open_market_trades":
        return [
            f"      insiderzy 90d: brak zakupów/sprzedaży rynkowych; "
            f"inne operacje: {ins.get('recent_other_90d', 0)}; źródło: {source}"
        ]

    buy_val = ins.get("buy_value_90d") or 0
    sell_val = ins.get("sell_value_90d") or 0
    buy_txt = (
        f"brak zakupów"
        if not buy_val else
        f"kupno {format_money_amount(buy_val, ccy)}"
        f" ({ins.get('recent_buys_90d', 0)} trans.){_plan_split_text('buy', ins, ccy)}"
    )
    sell_txt = (
        f"brak sprzedaży"
        if not sell_val else
        f"sprzedaż {format_money_amount(sell_val, ccy)}"
        f" ({ins.get('recent_sales_90d', 0)} trans.){_plan_split_text('sell', ins, ccy)}"
    )
    lines = [
        f"      insiderzy 90d: {buy_txt}; {sell_txt}; "
        f"sygnał: {_v(fund.get('insider_signal'))}; źródło: {source}"
    ]
    samples = []
    for item in (fund.get("insider_transactions") or [])[:3]:
        plan_label = PLAN_LABELS.get(item.get("plan") or "", "")
        date_txt = item.get("date") or "b/d"
        extra = f", {plan_label}" if plan_label else ""
        samples.append(
            f"{item.get('insider') or 'b/d'}, {item.get('transaction') or 'b/d'}, "
            f"{item.get('shares') or 0:,} akcji, "
            f"{format_money_amount(item.get('value'), ccy)} ({date_txt}{extra})"
        )
    if samples:
        lines.append("      przykłady: " + "; ".join(samples))
    return lines


def collect_portfolio_data(tickers: Optional[list] = None) -> dict:
    """Pobiera dane techniczne, fundamentalne, alpha i notowania dla wskazanych spółek.

    Bez listy — cały skonfigurowany portfel. Przy święcie przekazuj tylko aktywny rynek.
    """
    if tickers is None:
        tickers = list(US_TICKERS) + list(GPW_TICKERS_MAP.keys())
    portfolio_details = {}

    us = [t for t in tickers if not t.endswith(".WA")]
    gpw = [t for t in tickers if t.endswith(".WA")]
    quotes = {}
    if us:
        quotes.update(fetch_quotes_batch(us, period="5d"))
    if gpw:
        quotes.update(fetch_quotes_batch(gpw, period="5d"))

    for t in tickers:
        benchmark = US_BENCHMARK if not t.endswith(".WA") else PL_BENCHMARK
        quote = quotes.get(t, {})

        fundamentals = fetch_fundamentals_short_insider(t)
        technicals = fetch_technicals(t)
        alpha = calc_relative_strength(t, benchmark, "1mo")

        portfolio_details[t] = {
            "quote": quote,
            "fundamentals": fundamentals,
            "technicals": technicals,
            "alpha_1m_vs_benchmark": alpha,
            "benchmark": benchmark,
        }
    return portfolio_details


# ============================================================
# FAZA 1: Zbieranie danych (wspólna dla obu rendererów)
# ============================================================

def collect_report_data(persist_state: bool = True) -> dict:
    """Jedna faza zbierania WSZYSTKICH danych do raportu.

    Zwraca słownik ("ReportData") używany przez oba renderery (AI i podstawowy),
    dzięki czemu fallback po awarii Gemini nie pobiera niczego ponownie.
    Tu też jedyne miejsce zapisu dziennych snapshotów.

    persist_state=False (np. --preview): NIE zapisuje snapshotów ani prognoz —
    lokalne podglądy nie mogą nadpisywać danych CI wartościami śróddziennymi.
    Rzuca DataQualityError, gdy większość aktywnego portfela nie ma notowań.
    """
    now = warsaw_now()
    is_saturday = now.weekday() == 5

    # 0. Status rynków (święta) — pomijamy spółki rynków zamkniętych dziś
    status = market_status()
    skip = markets_to_skip(status)
    us_active = "US" not in skip
    pl_active = "PL" not in skip
    active_tickers = active_portfolio_tickers(skip)

    # 1. Notowania: portfel (techniczne, fundamenty, alpha), indeksy, makro
    logger.info("Pobieram dane rynkowe (portfel, indeksy, makro)...")
    portfolio_details = collect_portfolio_data(active_tickers)
    index_quotes = {name: fetch_quote_cached(ticker, period="5d") for name, ticker in INDEX_TICKERS.items()}
    macro_quotes = {name: fetch_quote_cached(ticker, period="5d") for name, ticker in MACRO_TICKERS.items()}
    macro_calendar = fetch_macro_calendar(days_ahead=7)

    # 2. Próg jakości danych — gdy większość aktywnych spółek portfela nie ma notowań,
    #    nie generujemy raportu (lepszy jasny błąd niż mail pełen "b/d").
    if active_tickers:
        valid = [
            t for t in active_tickers
            if (portfolio_details.get(t, {}).get("quote") or {}).get("price") is not None
        ]
        if len(valid) / len(active_tickers) < MIN_DATA_QUALITY:
            raise DataQualityError(
                f"Tylko {len(valid)}/{len(active_tickers)} aktywnych spółek portfela ma notowania "
                f"(próg: {MIN_DATA_QUALITY:.0%}). Prawdopodobna awaria źródła danych (yfinance)."
            )

    # 3. Top movers (tylko otwarte rynki)
    logger.info("Obliczam top movers (NASDAQ-100 i GPW-100)...")
    radar_us = fetch_nasdaq100_tickers() if us_active else []
    us_winners, us_losers = (
        get_top_movers(radar_us, top_n=3, period="5d", exclude=active_tickers)
        if us_active else ([], [])
    )
    gpw_winners, gpw_losers = (
        get_top_movers(GPW_100_TICKERS, top_n=3, period="5d", exclude=active_tickers)
        if pl_active else ([], [])
    )
    sc_winners, sc_losers = (
        get_top_movers(RADAR_SMALLCAPS, top_n=5, period="5d", exclude=active_tickers)
        if us_active else ([], [])
    )
    today_movers = {
        "us_winners": us_winners,
        "us_losers": us_losers,
        "gpw_winners": gpw_winners,
        "gpw_losers": gpw_losers,
        "sc_winners": sc_winners,
        "sc_losers": sc_losers,
    }

    # 4. Kalendarz wyników i kontekst earnings call — JEDNO pobranie na cały raport
    logger.info("Pobieram kalendarz publikacji wyników...")
    earnings_dates = fetch_earnings_dates(active_tickers)
    earnings_call_context = fetch_earnings_call_context(active_tickers)

    # 5. Newsy (superset: portfel + indeksy + makro + top movers)
    logger.info("Pobieranie newsów (Google News RSS + Yahoo Finance)...")
    top_movers_tickers = [t for t, _ in (us_winners + us_losers + gpw_winners + gpw_losers + sc_winners + sc_losers)]
    news_data = fetch_all_news(
        gpw_tickers_map=GPW_TICKERS_MAP if pl_active else {},
        gpw_news_queries=GPW_NEWS_QUERIES,
        us_tickers=US_TICKERS if us_active else [],
        extra_tickers=list(set(
            list(INDEX_TICKERS.values()) +
            list(MACRO_TICKERS.values()) +
            top_movers_tickers
        )),
        max_per_ticker=3,
        max_age_hours=168 if is_saturday else 36,
    )
    logger.info(f"Pobrano newsy dla {len(news_data)} tickerów.")

    # 6. Snapshoty: baza porównania + zapis dzisiejszych (jedyne miejsce zapisu).
    # W sobotę porównujemy ze stanem sprzed tygodnia (raport tygodniowy) —
    # sekcja zmian łapie wtedy przekroczenia z całego tygodnia, nie tylko pt vs czw.
    snapshot_before = None
    if is_saturday:
        snapshot_before = (now.date() - timedelta(days=5)).strftime("%Y-%m-%d")
    last_snapshots = load_last_snapshots(before=snapshot_before)
    current_metrics = {}
    for t, data in portfolio_details.items():
        quote = data.get("quote") or {}
        technicals = data.get("technicals") or {}
        current_metrics[t] = {
            "price": quote.get("price"),
            "change_pct": quote.get("change_pct"),
            "rsi_14": technicals.get("rsi_14"),
            "price_vs_sma20": technicals.get("price_vs_sma20"),
            "price_vs_sma50": technicals.get("price_vs_sma50"),
            "macd_histogram": technicals.get("macd_histogram"),
            "bollinger_position": technicals.get("bollinger_position"),
        }
    for _cat, movers in today_movers.items():
        for ticker, q in movers:
            if ticker not in current_metrics:
                current_metrics[ticker] = {
                    "price": q.get("price"),
                    "change_pct": q.get("change_pct"),
                    "rsi_14": None,
                    "price_vs_sma20": None,
                    "price_vs_sma50": None,
                    "macd_histogram": None,
                    "bollinger_position": None,
                }
    if persist_state:
        save_today_snapshots(current_metrics)
    else:
        logger.info("Tryb podglądu — pomijam zapis snapshotów.")

    current_prices = {
        ticker: (data.get("quote") or {}).get("price")
        for ticker, data in portfolio_details.items()
    }

    return {
        "now": now,
        "is_saturday": is_saturday,
        "persist": persist_state,
        "status": status,
        "skip": skip,
        "us_active": us_active,
        "pl_active": pl_active,
        "active_tickers": active_tickers,
        "portfolio_details": portfolio_details,
        "index_quotes": index_quotes,
        "macro_quotes": macro_quotes,
        "macro_calendar": macro_calendar,
        "today_movers": today_movers,
        "earnings_dates": earnings_dates,
        "earnings_call_context": earnings_call_context,
        "news_data": news_data,
        "last_snapshots": last_snapshots,
        "current_prices": current_prices,
    }


def _report_header(now: datetime, skip: set, status: dict) -> str:
    """Nagłówek raportu + baner o zamkniętych rynkach (wspólny dla obu rendererów)."""
    date_str = now.strftime("%d.%m.%Y")
    day_names = {
        0: "poniedziałek", 1: "wtorek", 2: "środa",
        3: "czwartek", 4: "piątek", 5: "sobota", 6: "niedziela"
    }
    day_name = day_names[now.weekday()]
    header = (
        f"# RAPORT RYNKOWY -- {date_str} ({day_name})\n"
        f"*Senior Capital Markets Analyst | Sektor: Tech/Semiconductors (NASDAQ) + GPW*\n\n---\n\n"
    )
    banner = market_banner_lines(skip, status)
    if banner:
        header += "\n".join(banner) + "\n\n"
    return header


# ============================================================
# FAZA 2a: Renderer podstawowy (bez AI)
# ============================================================

# ============================================================
# FAZA 2b: Renderer AI (Gemini)
# ============================================================

def _v(x, suffix: str = "") -> str:
    """Wartość do prompta: None -> 'b/d', inaczej wartość + sufiks."""
    return "b/d" if x is None else f"{x}{suffix}"


def _fmt_quote_line(label: str, q: dict) -> str:
    """Jedna linia notowania do prompta (zamiast repr() słownika)."""
    q = q or {}
    if q.get("error") or q.get("price") is None:
        return f"{label}: brak danych"
    vol = q.get("volume")
    vol_txt = f", wolumen {vol:,}" if vol else ""
    stale = " [STALE]" if q.get("is_stale") else ""
    return (
        f"{label}: {q['price']:,.2f} ({q.get('change_pct', 0):+.2f}%)"
        f"{vol_txt}, sesja {q.get('data_date', 'b/d')}{stale}"
    )


def _fmt_quotes_block(quotes: dict) -> str:
    """Blok notowań {nazwa/ticker: quote} jako lista linii."""
    lines = [f"    - {_fmt_quote_line(k, q)}" for k, q in quotes.items()]
    return "\n".join(lines) if lines else "    - brak danych"


def _fmt_movers(movers: list) -> str:
    """Lista top movers [(ticker, quote), ...] jako linie tekstu."""
    lines = [f"    - {_fmt_quote_line(f'{TICKER_NAMES.get(t, t)} ({t})', q)}" for t, q in movers]
    return "\n".join(lines) if lines else "    - brak"


def _fmt_earnings(earnings_dates: dict) -> str:
    lines = [
        f"    - {t}: {info.get('date', 'b/d')} (za {info.get('days_until', '?')} dni)"
        for t, info in earnings_dates.items()
    ]
    return "\n".join(lines) if lines else "    - brak danych o nadchodzących wynikach"


def _fmt_portfolio_block(portfolio_details: dict) -> str:
    """Zwięzły, czytelny opis portfela do prompta (zamiast repr() zagnieżdżonych słowników)."""
    out = []
    for t, d in portfolio_details.items():
        d = d or {}
        q = d.get("quote") or {}
        tech = d.get("technicals") or {}
        fund = d.get("fundamentals") or {}

        out.append(f"    {TICKER_NAMES.get(t, t)} ({t}):")
        out.append(f"      notowanie: {_fmt_quote_line(t, q)}")
        out.append(
            f"      technika: RSI {_v(tech.get('rsi_14'))}, vs SMA20 {_v(tech.get('price_vs_sma20'), '%')}, "
            f"vs SMA50 {_v(tech.get('price_vs_sma50'), '%')}, MACD hist {_v(tech.get('macd_histogram'))} "
            f"({_v(tech.get('macd_trend'))}), Bollinger {_v(tech.get('bollinger_position'))} "
            f"({_v(tech.get('bollinger_signal'))}), zmiana 5 sesji {_v(tech.get('change_5d'), '%')}, "
            f"wolumen {_v(tech.get('volume_ratio_10d'), 'x śr. 10d')}"
        )
        out.append(
            f"      trend: układ średnich {_v(tech.get('ema_stack'))}, ADX {_v(tech.get('adx'))} "
            f"(+DI {_v(tech.get('plus_di'))} / -DI {_v(tech.get('minus_di'))}), "
            f"Supertrend {_v(tech.get('supertrend_dir'))}, Ichimoku {_v(tech.get('ichimoku_cloud'))}, "
            f"Donchian {_v(tech.get('donchian_signal'))}, ATR {_v(tech.get('atr_pct'), '%')}, "
            f"nachylenie regresji {_v(tech.get('reg_slope_pct'), '%/sesję')}"
        )
        mcap = fund.get("market_cap")
        # yfinance podaje kapitalizację w walucie notowania: GPW (.WA) -> PLN
        mcap_ccy = "PLN" if t.endswith(".WA") else "USD"
        mcap_txt = f"{mcap / 1e9:.1f} mld {mcap_ccy}" if mcap else "b/d"
        short_pct = fund.get("short_pct_float")
        if short_pct is not None and short_pct <= 1.0:
            short_pct = round(short_pct * 100.0, 1)
        out.append(
            f"      fundamenty: fwd P/E {_v(fund.get('forward_pe'))}, P/S {_v(fund.get('price_to_sales'))}, "
            f"kapitalizacja {mcap_txt}, {_v(fund.get('pct_from_52w_high'), '%')} od szczytu 52w, "
            f"{_v(fund.get('pct_from_52w_low'), '%')} od dołka 52w, short float {_v(short_pct, '%')}"
        )
        out.extend(format_insider_lines(t, fund))
        bench = US_BENCHMARK if not t.endswith(".WA") else PL_BENCHMARK
        out.append(f"      alpha 1M vs benchmark sektorowy ({bench}): {_v(d.get('alpha_1m_vs_benchmark'), ' pp')}")
        out.append("")
    return "\n".join(out) if out else "    brak danych portfela"


# ============================================================
# FAZA 3: Orkiestracja
# ============================================================

def build_report(persist_state: bool = True) -> str:
    """Generuje raport (layout v2 — jedyny aktywny układ, patrz layout_v2.py):
    zbiera dane RAZ, próbuje Gemini, przy awarii renderuje raport regułowy
    z TYCH SAMYCH danych (bez ponownego pobierania).

    persist_state=False (--preview): bez zapisu snapshotów i prognoz.
    Stary układ v1 zarchiwizowany w archive/layout_v1.py (nieużywany)."""
    api_key = os.getenv("GEMINI_API_KEY")
    has_ai = bool(
        api_key and genai
        and api_key not in ("WKLEJ_KLUCZ_API", "your_gemini_api_key_here")
    )

    data = collect_report_data(persist_state)  # DataQualityError propaguje do main.py

    # Import leniwy — layout_v2 importuje helpery z tego modułu (unikamy cyklu)
    from layout_v2 import collect_v2_extras, render_ai_report_v2, render_basic_report_v2
    collect_v2_extras(data)
    if not has_ai:
        logger.info("Brak poprawnego klucza GEMINI_API_KEY. Uzywam generatora regułowego.")
        return render_basic_report_v2(data)
    logger.info("Klucz Gemini znaleziony. Generowanie analizy AI...")
    try:
        return render_ai_report_v2(data, api_key)
    except Exception as e:
        logger.error(f"Blad generowania przez Gemini: {e}")
        logger.info("Fallback do generatora regułowego (bez ponownego pobierania danych).")
        return render_basic_report_v2(data)


def build_basic_report(persist_state: bool = True) -> str:
    """Buduje raport regułowy (bez AI) — zachowane dla kompatybilności z report_fetcher."""
    from layout_v2 import collect_v2_extras, render_basic_report_v2
    data = collect_report_data(persist_state)
    collect_v2_extras(data)
    return render_basic_report_v2(data)


# ============================================================
# Komponenty raportu
# ============================================================

def generate_trend_analysis_section(portfolio_details: dict, active_tickers: Optional[list] = None) -> str:
    """Sobotnia, pogłębiona analiza trendu per spółka:
    układ średnich (EMA20/50/200 vs SMA200), ADX/+DI/−DI, Supertrend, Ichimoku, Donchian, ATR%, nachylenie regresji.
    """
    tickers = active_tickers if active_tickers is not None else US_TICKERS + list(GPW_TICKERS_MAP.keys())
    stack_label = {"strong_up": "↑ wzrostowy", "strong_down": "↓ spadkowy", "mixed": "→ mieszany"}
    ichi_label = {"above": "nad chmurą", "below": "pod chmurą", "inside": "w chmurze"}
    don_label = {"breakout_up": "wybicie ↑", "breakout_down": "wybicie ↓", "inside": "w kanale"}

    lines = []
    lines.append("## Analiza trendu (tygodniowa)")
    lines.append("")
    lines.append("| Spółka | Układ średnich | ADX (+DI/-DI) | Supertrend | Ichimoku | Donchian | ATR % | Nachylenie |")
    lines.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
    for ticker in tickers:
        t = (portfolio_details.get(ticker, {}) or {}).get("technicals") or {}
        ema_stack = stack_label.get(t.get("ema_stack"), "b/d")
        adx, plus_di, minus_di = t.get("adx"), t.get("plus_di"), t.get("minus_di")
        adx_str = (
            f"{adx:.0f} ({plus_di:.0f}/{minus_di:.0f})"
            if adx is not None and plus_di is not None and minus_di is not None else "b/d"
        )
        st = t.get("supertrend_dir") or "b/d"
        ichi = ichi_label.get(t.get("ichimoku_cloud"), "b/d")
        don = don_label.get(t.get("donchian_signal"), "b/d")
        atr_pct = t.get("atr_pct")
        atr_str = f"{atr_pct:.1f}%" if atr_pct is not None else "b/d"
        slope = t.get("reg_slope_pct")
        slope_str = f"{slope:+.2f}%/d" if slope is not None else "b/d"
        lines.append(
            f"| **{ticker}** | {ema_stack} | {adx_str} | {st} | {ichi} | {don} | {atr_str} | {slope_str} |"
        )
    lines.append("")
    lines.append("> Trend wzrostowy: układ EMA20>EMA50>SMA200, ADX≥25 z +DI>−DI, cena nad chmurą Ichimoku i Supertrend `long`.")
    lines.append("")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


