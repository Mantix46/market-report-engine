"""Layout v2 raportu rynkowego — 10-sekcyjny układ.

Konsumuje ten sam słownik ReportData z report_builder.collect_report_data();
sekcje deterministyczne buduje w Pythonie, narracyjne Gemini z fallbackiem regułowym.

Klient HTTP Gemini: gemini_client.py.
"""

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from data_fetching import (
    US_TICKERS, GPW_TICKERS_MAP, TICKER_NAMES, TICKER_NOTES, RADAR_NOTES,
    US_BENCHMARK, PL_BENCHMARK,
    get_index_tickers_v2, fetch_period_change, fetch_analyst_recommendations,
    fetch_quote_cached,
)
from gemini_client import (
    GEMINI_ATTEMPT_TIMEOUT_SECONDS,
    GEMINI_FALLBACK_CHAIN,
    GEMINI_TIMEOUT_SECONDS,
    call_gemini,
)
from report_builder import (
    format_change, format_pct, format_insider_lines,
    market_banner_lines,
    _v, _fmt_quote_line, _fmt_quotes_block, _fmt_movers,
    _fmt_earnings, generate_trend_analysis_section,
)
from technicals import generate_technical_signal, trend_label
from snapshot_store import calculate_deltas
from accuracy_tracker import (
    build_rule_based_predictions, evaluate_previous_predictions,
    extract_predictions_from_report, extract_prediction_reviews,
    format_accuracy_section, load_accuracy_feedback, record_prediction_reviews,
    save_predictions,
)

logger = logging.getLogger(__name__)

IMPACT_EMOJI = {"high": "🔴", "medium": "🟠", "low": "🟢"}

# Nagłówki sekcji AI — odpowiedź Gemini jest parsowana przez split na DOKŁADNIE te stringi
V2_AI_HEADERS = [
    "## 1. Executive Summary",
    "## 4. Monitoring spółek",
    "## 5. Top 5 wydarzeń makro",
    "## 7. Ryzyka",
    "## 9. Sentyment",
    "## 10. Watchlist",
]

# Sobotnia sekcja AI — format bulletów MUSI pasować do regexa
# accuracy_tracker.extract_predictions_from_report (TICKER + kierunek=up/down/neutral)
PREDICTIONS_HEADER = "## Prognozy do weryfikacji"
PREDICTION_REVIEW_HEADER = "### Analiza trafionych i nietrafionych prognoz"


# ============================================================
# Dane dodatkowe (tylko v2 — nie spowalnia v1)
# ============================================================

def collect_v2_extras(data: dict) -> dict:
    """Dopisuje do ReportData klucz 'v2': indeksy v2 (z SX5E i WIG20),
    zmiany tygodniowe/YTD indeksów oraz rekomendacje analityków dla portfela."""
    logger.info("Layout v2: pobieram indeksy (SX5E/WIG20) i rekomendacje analityków...")
    idx = get_index_tickers_v2()
    data["v2"] = {
        "index_tickers": idx,
        "index_quotes": {name: fetch_quote_cached(t, period="5d") for name, t in idx.items()},
        "index_periods": {
            name: {
                "weekly": fetch_period_change(t, period="7d"),
                "ytd": fetch_period_change(t, period="ytd"),
            }
            for name, t in idx.items()
        },
        "analyst_recs": {t: fetch_analyst_recommendations(t) for t in data["active_tickers"]},
    }
    if data.get("is_saturday"):
        # Ocena musi trafić do prompta, zanim Gemini napisze nowe prognozy.
        data["prediction_evaluation"] = evaluate_previous_predictions(
            dry_run=not data.get("persist", True)
        )
        data["prediction_feedback"] = load_accuracy_feedback(limit=20)
    return data


# ============================================================
# Sekcje deterministyczne (Python)
# ============================================================

def _header_v2(now: datetime, skip: set, status: dict) -> str:
    """Nagłówek raportu v2 + baner o zamkniętych rynkach."""
    day_names = {
        0: "poniedziałek", 1: "wtorek", 2: "środa",
        3: "czwartek", 4: "piątek", 5: "sobota", 6: "niedziela",
    }
    header = (
        f"# RAPORT RYNKOWY -- {now.strftime('%d.%m.%Y')} ({day_names[now.weekday()]})\n"
        f"*Senior Capital Markets Analyst | Sektor: Tech/Semiconductors (NASDAQ) + GPW*\n\n---\n\n"
    )
    banner = market_banner_lines(skip, status)
    if banner:
        header += "\n".join(banner) + "\n\n"
    return header


def _insider_flag(fund: dict) -> str:
    status = fund.get("insider_data_status", "unavailable")
    if status not in ("available", "no_open_market_trades"):
        return "insider: b/d"
    ins = fund.get("insider_summary") or {}
    planned_s = ins.get("planned_sell_value_90d") or 0
    disc_s = ins.get("discretionary_sell_value_90d") or 0
    planned_b = ins.get("planned_buy_value_90d") or 0
    disc_b = ins.get("discretionary_buy_value_90d") or 0
    sell = ins.get("sell_value_90d") or 0
    buy = ins.get("buy_value_90d") or 0
    if disc_s and disc_s >= planned_s and disc_s >= max(buy, disc_b):
        return "insider: sprzedaż nagła"
    if planned_s and planned_s >= disc_s and sell:
        return "insider: sprzedaż zaplanowana"
    if disc_b or planned_b or buy:
        return "insider: kupno"
    if status == "no_open_market_trades":
        return "insider: brak obrotu"
    return "insider: plan b/d" if sell or buy else "insider: brak"


def _short_pct_display(fund: dict) -> Optional[float]:
    short_pct = fund.get("short_pct_float")
    if short_pct is None:
        return None
    if short_pct <= 1.0:
        short_pct = round(short_pct * 100.0, 1)
    return short_pct


def build_positions_md(data: dict) -> str:
    """Tabela portfela — skan w 20 sekund przed Executive Summary."""
    lines = ["## Moje pozycje", ""]
    lines.append(
        "| Spółka | Kurs | 1D | 5 sesji | vs bench. | Wol. | Tech | Wyniki | Flagi |"
    )
    lines.append("| :--- | ---: | ---: | ---: | ---: | ---: | :--- | :--- | :--- |")
    for ticker in data.get("active_tickers") or []:
        details = data["portfolio_details"].get(ticker, {}) or {}
        q = details.get("quote") or {}
        tech = details.get("technicals") or {}
        fund = details.get("fundamentals") or {}
        name = TICKER_NAMES.get(ticker, ticker)
        ccy = "PLN" if ticker.endswith(".WA") else "USD"
        if q.get("error") or q.get("price") is None:
            lines.append(
                f"| **{name}** ({ticker}) | b/d | b/d | b/d | b/d | b/d | b/d | b/d | b/d |"
            )
            continue
        price = f"{q['price']:.2f} {ccy}"
        change_1d = format_change(q.get("change_pct")) if q.get("change_pct") is not None else "b/d"
        change_5d = tech.get("change_5d")
        change_5d_txt = format_change(change_5d) if change_5d is not None else "b/d"
        bench = details.get("benchmark") or (PL_BENCHMARK if ticker.endswith(".WA") else US_BENCHMARK)
        alpha = details.get("alpha_1m_vs_benchmark")
        alpha_txt = f"{alpha:+.1f} pp vs {bench}" if alpha is not None else f"b/d vs {bench}"
        vol = tech.get("volume_ratio_10d")
        vol_txt = f"{vol:.2f}x" if vol is not None else "b/d"
        signal = generate_technical_signal(
            tech.get("rsi_14"),
            tech.get("price_vs_sma20"),
            tech.get("price_vs_sma50"),
            tech.get("macd_trend"),
            tech.get("bollinger_signal"),
        )
        tech_txt = f"{signal} ({trend_label(tech)})"
        earnings = (data.get("earnings_dates") or {}).get(ticker) or {}
        days = earnings.get("days_until")
        if days is not None:
            earn_txt = f"{earnings.get('date', 'b/d')} ({days}d)"
        else:
            earn_txt = "b/d"
        flags = []
        high = fund.get("pct_from_52w_high")
        if high is not None:
            flags.append(f"52w {high:+.1f}%")
        short_pct = _short_pct_display(fund)
        if short_pct is not None and short_pct > 10:
            flags.append(f"short {short_pct:.1f}%")
        flags.append(_insider_flag(fund))
        lines.append(
            f"| **{name}** ({ticker}) | {price} | {change_1d} | {change_5d_txt} "
            f"| {alpha_txt} | {vol_txt} | {tech_txt} | {earn_txt} | {'; '.join(flags)} |"
        )
    lines.append("")
    return "\n".join(lines)


def build_benchmarks_md(data: dict) -> str:
    """## 2. Benchmarki rynkowe — SPX, NDX, SX5E, WIG20: poziom, 1D, 1T, YTD."""
    v2 = data["v2"]
    lines = ["## 2. Benchmarki rynkowe", ""]
    lines.append("| Indeks | Poziom | Zmiana 1D | Zmiana 1T | Zmiana YTD |")
    lines.append("|--------|--------|-----------|-----------|------------|")
    for name, q in v2["index_quotes"].items():
        periods = v2["index_periods"].get(name, {})
        if q.get("error") or q.get("price") is None:
            lines.append(f"| **{name}** | b/d | b/d | b/d | b/d |")
        else:
            lines.append(
                f"| **{name}** | {q['price']:,.2f} | {format_pct(q.get('change_pct'))} "
                f"| {format_pct(periods.get('weekly'))} | {format_pct(periods.get('ytd'))} |"
            )
    lines.append("")
    return "\n".join(lines)


def _calendar_line(event: dict, today) -> str:
    """Jedna linia kalendarza makro z emoji ważności i prefiksem DZIŚ/JUTRO."""
    emoji = IMPACT_EMOJI.get(event.get("impact"), "🟠")
    date_val = event.get("date") or event.get("date_range") or "b/d"
    prefix = ""
    if event.get("date") == str(today):
        prefix = "**DZIŚ** "
    elif event.get("date") == str(today + timedelta(days=1)):
        prefix = "**JUTRO** "
    estimate = event.get("estimate")
    estimate_txt = f", konsensus: {estimate}" if estimate not in (None, "") else ""
    return (
        f"- {emoji} {prefix}**{date_val}** ({event.get('country', 'b/d')}): "
        f"{event.get('event', 'b/d')}{estimate_txt}"
    )


def build_macro_md(data: dict) -> str:
    """## 3. Makroekonomia — wskaźniki (VIX, 10Y, USD/PLN) + kalendarz z emoji ważności."""
    lines = ["## 3. Makroekonomia", ""]
    lines.append("| Wskaźnik | Poziom | Zmiana |")
    lines.append("|----------|--------|--------|")
    for name, q in data["macro_quotes"].items():
        if q.get("error") or q.get("price") is None:
            lines.append(f"| {name} | b/d | b/d |")
        else:
            lines.append(f"| {name} | {q['price']:.2f} | {format_change(q['change_pct'])} |")
    lines.append("")
    lines.append("**Kalendarz makro (najbliższe dni):**")
    today = data["now"].date()
    for event in data["macro_calendar"][:8]:
        lines.append(_calendar_line(event, today))
    lines.append("")
    lines.append("Legenda ważności: 🔴 wysoki wpływ, 🟠 średni wpływ, 🟢 niski wpływ.")
    lines.append("")
    upcoming = []
    for ticker, info in (data.get("earnings_dates") or {}).items():
        days = info.get("days_until")
        if days is None:
            continue
        upcoming.append((days, ticker, info))
    if upcoming:
        upcoming.sort()
        lines.append("**Wyniki spółek portfela:**")
        for days, ticker, info in upcoming[:8]:
            lines.append(
                f"- **{TICKER_NAMES.get(ticker, ticker)}** ({ticker}): "
                f"{info.get('date', 'b/d')} (za {days} dni)"
            )
        lines.append("")
    return "\n".join(lines)


def _movers_table(movers: list, is_us: bool = True, with_notes: bool = False) -> list[str]:
    """Tabela top movers: wzrosty albo spadki, z wolumenem."""
    header = "| Spółka | Kurs | Zmiana | Wolumen |"
    sep = "|--------|------|--------|---------|"
    if with_notes:
        header += " Notatka |"
        sep += "---------|"
    lines = [header, sep]
    for ticker, q in movers:
        name_disp = TICKER_NAMES.get(ticker, ticker)
        if is_us:
            is_eu = any(suf in ticker for suf in (".DE", ".PA", ".L"))
            price_disp = f"{q['price']:.2f}" if is_eu else f"${q['price']:.2f}"
        else:
            price_disp = f"{q['price']:.2f} PLN"
        ratio = q.get("volume_ratio")
        vol_txt = f"{ratio:.2f}x" if ratio is not None else "b/d"
        row = (
            f"| **{name_disp}** ({ticker}) | {price_disp} | "
            f"{format_change(q.get('change_pct', 0))} | {vol_txt} |"
        )
        if with_notes:
            note = RADAR_NOTES.get(ticker, "")
            row = row[:-1] + f" {note} |"
        lines.append(row)
    return lines


def _append_mover_group(lines: list[str], title: str, winners: list, losers: list, is_us: bool, with_notes: bool = False):
    if not winners and not losers:
        return
    lines.append(f"### {title}")
    lines.append("")
    if winners:
        lines.append("**Wzrosty**")
        lines.append("")
        lines.extend(_movers_table(winners, is_us=is_us, with_notes=with_notes))
        lines.append("")
    if losers:
        lines.append("**Spadki**")
        lines.append("")
        lines.extend(_movers_table(losers, is_us=is_us, with_notes=with_notes))
        lines.append("")


def build_movers_md(data: dict) -> str:
    """## 6. Radar rynkowy — wzrosty/spadki + wolumen, bez spółek portfela."""
    movers = data["today_movers"]
    lines = ["## 6. Radar rynkowy -- Największe ruchy dnia", ""]
    sc_notes = bool(
        RADAR_NOTES
        and any(t in RADAR_NOTES for t, _ in (movers.get("sc_winners") or []) + (movers.get("sc_losers") or []))
    )

    if data["us_active"]:
        _append_mover_group(lines, "USA -- Top movers", movers["us_winners"], movers["us_losers"], True)
    if data["pl_active"]:
        _append_mover_group(lines, "GPW -- Top movers", movers["gpw_winners"], movers["gpw_losers"], False)
    if data["us_active"]:
        _append_mover_group(
            lines,
            "Sektor AI Bottlenecks (Small/Mid-Caps) -- Top movers",
            movers["sc_winners"],
            movers["sc_losers"],
            True,
            with_notes=sc_notes,
        )
    return "\n".join(lines)


def _fmt_ratio(val: Optional[float], as_pct: bool = False) -> str:
    """Wartość wskaźnika wyceny do tabeli: None -> b/d; ułamki (ROE, marże) jako %."""
    if val is None:
        return "b/d"
    if as_pct:
        return f"{val * 100:.1f}%"
    return f"{val:.2f}" if isinstance(val, float) else str(val)


def build_valuation_md(data: dict) -> str:
    """## 8. Wycena i technika — wąska tabela dzienna; sobota: druga warstwa szczegółów."""
    lines = ["## 8. Wycena i technika", ""]
    lines.append("| Spółka | Fwd P/E | EV/EBITDA | FCF Yield | vs 52w | Sygnał tech. |")
    lines.append("| :--- | :---: | :---: | :---: | :---: | :--- |")
    for ticker in data["active_tickers"]:
        details = data["portfolio_details"].get(ticker, {}) or {}
        fund = details.get("fundamentals") or {}
        tech = details.get("technicals") or {}
        signal = generate_technical_signal(
            tech.get("rsi_14"),
            tech.get("price_vs_sma20"),
            tech.get("price_vs_sma50"),
            tech.get("macd_trend"),
            tech.get("bollinger_signal"),
        )
        fcf_yield = fund.get("fcf_yield_pct")
        fcf_str = f"{fcf_yield:.1f}%" if fcf_yield is not None else "b/d"
        high = fund.get("pct_from_52w_high")
        high_str = f"{high:+.1f}%" if high is not None else "b/d"
        lines.append(
            f"| **{ticker}** | {_fmt_ratio(fund.get('forward_pe'))} "
            f"| {_fmt_ratio(fund.get('ev_to_ebitda'))} "
            f"| {fcf_str} | {high_str} "
            f"| {signal} ({trend_label(tech)}) |"
        )
    lines.append("")
    if data.get("is_saturday"):
        lines.append("### Wycena — szczegóły")
        lines.append("")
        lines.append("| Spółka | Trailing P/E | PEG | P/B | ROE | Debt/EBITDA | Marża oper. |")
        lines.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: |")
        for ticker in data["active_tickers"]:
            fund = (data["portfolio_details"].get(ticker, {}) or {}).get("fundamentals") or {}
            lines.append(
                f"| **{ticker}** | {_fmt_ratio(fund.get('trailing_pe'))} "
                f"| {_fmt_ratio(fund.get('peg_ratio'))} "
                f"| {_fmt_ratio(fund.get('price_to_book'))} "
                f"| {_fmt_ratio(fund.get('return_on_equity'), as_pct=True)} "
                f"| {_fmt_ratio(fund.get('debt_to_ebitda'))} "
                f"| {_fmt_ratio(fund.get('operating_margin'), as_pct=True)} |"
            )
        lines.append("")
    lines.append(
        "> Interpretacja: wartości porównuj ze średnią sektorową (semis: EV/EBITDA ~15-25, "
        "GPW: P/E ~8-15). \"b/d\" = wskaźnik niedostępny w yfinance (częste dla GPW i spółek bez zysków)."
    )
    lines.append("")
    return "\n".join(lines)


# ============================================================
# Bloki danych dla prompta AI i fallbacku regułowego
# ============================================================

def _fmt_analyst_recs(ticker: str, recs: dict, price: Optional[float]) -> list[str]:
    """Linie opisu rekomendacji analityków dla jednej spółki."""
    out = []
    summary = recs.get("rec_summary")
    if summary:
        dist = ", ".join(f"{k}: {v}" for k, v in summary.items() if v)
        out.append(f"      rekomendacje: {dist or 'brak ocen'}")
    targets = recs.get("price_targets")
    if targets and targets.get("mean"):
        mean = targets["mean"]
        upside = f" (potencjał {((mean / price) - 1) * 100:+.1f}%)" if price else ""
        out.append(
            f"      cena docelowa: średnia {mean:.2f}{upside}, "
            f"zakres {_v(targets.get('low'))}-{_v(targets.get('high'))}"
        )
    for ch in recs.get("recent_changes", []):
        out.append(
            f"      zmiana rekomendacji {ch.get('date')}: {ch.get('firm')} "
            f"{ch.get('action')} ({ch.get('from_grade') or '?'} -> {ch.get('to_grade') or '?'})"
        )
    if not out:
        out.append("      rekomendacje: brak danych analitycznych")
    return out


def _company_data_block(data: dict) -> str:
    """Jedyny kanoniczny blok faktów per spółka — wsad dla prompta Gemini."""
    v2 = data.get("v2", {})
    out = []
    for t in data["active_tickers"]:
        details = data["portfolio_details"].get(t, {}) or {}
        q = details.get("quote") or {}
        tech = details.get("technicals") or {}
        fund = details.get("fundamentals") or {}

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
        mcap_ccy = "PLN" if t.endswith(".WA") else "USD"
        mcap_txt = f"{mcap / 1e9:.1f} mld {mcap_ccy}" if mcap else "b/d"
        short_pct = _short_pct_display(fund)
        out.append(
            f"      fundamenty: fwd P/E {_v(fund.get('forward_pe'))}, P/S {_v(fund.get('price_to_sales'))}, "
            f"kapitalizacja {mcap_txt}, {_v(fund.get('pct_from_52w_high'), '%')} od szczytu 52w, "
            f"{_v(fund.get('pct_from_52w_low'), '%')} od dołka 52w, short float {_v(short_pct, '%')}"
        )
        bench = details.get("benchmark") or (PL_BENCHMARK if t.endswith(".WA") else US_BENCHMARK)
        out.append(
            f"      alpha 1M vs benchmark sektorowy ({bench}): "
            f"{_v(details.get('alpha_1m_vs_benchmark'), ' pp')}"
        )

        earnings = (data.get("earnings_dates") or {}).get(t)
        if earnings:
            out.append(
                f"      wyniki: {earnings.get('date')} (za {earnings.get('days_until', '?')} dni)"
            )
        for item in ((data.get("earnings_call_context") or {}).get(t) or [])[:2]:
            title = item.get("title") or item.get("quarter") or "earnings call"
            summary = (item.get("summary") or "")[:280]
            url = item.get("url") or ""
            out.append(f"      earnings call: {title} — {summary} {url}".strip())

        out.extend(_fmt_analyst_recs(t, (v2.get("analyst_recs") or {}).get(t, {}), q.get("price")))
        out.extend(format_insider_lines(t, fund))

        deltas = calculate_deltas(
            t,
            {
                "rsi_14": tech.get("rsi_14"),
                "price_vs_sma20": tech.get("price_vs_sma20"),
                "price_vs_sma50": tech.get("price_vs_sma50"),
                "macd_histogram": tech.get("macd_histogram"),
                "bollinger_position": tech.get("bollinger_position"),
            },
            (data.get("last_snapshots") or {}).get(t),
        )
        if deltas:
            out.append(f"      co się zmieniło od poprzedniego raportu: {', '.join(deltas)}")

        for a in (data.get("news_data") or {}).get(t, [])[:3]:
            date_txt = a.get("date") or "b/d"
            out.append(
                f"      news ({date_txt}): [{a.get('publisher')}] {a.get('title')} | {a.get('url', '')}"
            )

        note = TICKER_NOTES.get(t)
        if note:
            out.append(f"      OSTRZEŻENIE: {note}")
        out.append("")
    return "\n".join(out) if out else "    brak danych portfela"


def _macro_news_block(data: dict, max_lines: int = 15) -> str:
    """Newsy makro/indeksowe/top movers (tickery SPOZA portfela) — wsad dla sekcji 5.
    Newsy spółek portfela idą osobno w _company_data_block."""
    active = set(data.get("active_tickers") or [])
    out = []
    for ticker, articles in (data.get("news_data") or {}).items():
        if ticker in active:
            continue
        label = TICKER_NAMES.get(ticker, ticker)
        for a in articles:
            date_txt = a.get("date") or "b/d"
            out.append(
                f"    - ({date_txt}) [{a.get('publisher')}] ({label}) {a.get('title')} | {a.get('url', '')}"
            )
            if len(out) >= max_lines:
                return "\n".join(out)
    return "\n".join(out) if out else "    - brak newsów makro w danych"


def _sentiment_data_block(data: dict) -> str:
    """Deterministyczne wejścia do sekcji 9 (Sentyment) — proxy zamiast płatnych źródeł."""
    v2 = data.get("v2", {})
    out = []

    vix = (data.get("macro_quotes") or {}).get("VIX") or {}
    if vix.get("price") is not None:
        out.append(
            f"    VIX: {vix['price']:.2f} ({format_change(vix.get('change_pct', 0))}) — "
            f"{'risk-on (<15)' if vix['price'] < 15 else 'risk-off (>25)' if vix['price'] > 25 else 'neutralny (15-25)'}"
        )

    for t in data["active_tickers"]:
        details = data["portfolio_details"].get(t, {}) or {}
        fund = details.get("fundamentals") or {}
        tech = details.get("technicals") or {}
        short_pct = fund.get("short_pct_float")
        if short_pct is not None and short_pct <= 1.0:
            short_pct = round(short_pct * 100.0, 1)
        vol_ratio = tech.get("volume_ratio_10d")
        flags = []
        if short_pct is not None and short_pct > 10:
            flags.append(f"wysoki short float {short_pct}%")
        if vol_ratio is not None and vol_ratio > 1.5:
            flags.append(f"nietypowy wolumen {vol_ratio}x średniej")
        if fund.get("insider_signal") not in (None, "neutral"):
            flags.append(f"insiderzy: {fund['insider_signal']}")
        recs = (v2.get("analyst_recs") or {}).get(t, {})
        n_changes = len(recs.get("recent_changes", []))
        if n_changes:
            flags.append(f"{n_changes} zmian(y) rekomendacji w 30 dni")
        if flags:
            out.append(f"    {t}: {'; '.join(flags)}")

    return "\n".join(out) if out else "    brak sygnałów sentymentu w danych"


# ============================================================
# Renderer AI — HTTP w gemini_client.py
# ============================================================

# Re-eksport stałych (testy / diagnostyka).
GEMINI_DEFAULT_MODEL = "gemini-3.8-flash"


def _call_gemini_v2(prompt: str, api_key: str) -> tuple[str, str]:
    """Zwraca (tekst, nazwa_modelu). Fallback regułowy łapie wyjątek wyżej."""
    logger.info(
        f"Layout v2 — timeout Gemini: {GEMINI_TIMEOUT_SECONDS}s "
        f"(max {GEMINI_ATTEMPT_TIMEOUT_SECONDS}s na próbę), "
        f"łańcuch zapasowy: {' -> '.join(GEMINI_FALLBACK_CHAIN)}"
    )
    return call_gemini(prompt, api_key)


def _parse_ai_sections(text: str, headers: Optional[list] = None) -> dict:
    """Dzieli odpowiedź Gemini na sekcje po dokładnych nagłówkach (domyślnie V2_AI_HEADERS;
    w sobotę lista rozszerzona o analizę feedbacku i PREDICTIONS_HEADER).
    Brak któregokolwiek nagłówka -> ValueError (uruchamia fallback regułowy)."""
    headers = headers or V2_AI_HEADERS
    positions = []
    for header in headers:
        pos = text.find(header)
        if pos == -1:
            raise ValueError(f"Gemini v2: brak wymaganej sekcji '{header}' w odpowiedzi")
        positions.append((pos, header))
    positions.sort()
    sections = {}
    for i, (pos, header) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        sections[header] = text[pos:end].strip()
    return sections


def _prediction_context(data: dict, ticker: str) -> str:
    """Kompaktowy zapis informacji dostępnych w chwili tworzenia prognozy."""
    details = (data.get("portfolio_details") or {}).get(ticker, {}) or {}
    technicals = details.get("technicals") or {}
    fundamentals = details.get("fundamentals") or {}
    insider_summary = fundamentals.get("insider_summary") or {}
    analyst = ((data.get("v2") or {}).get("analyst_recs") or {}).get(ticker) or {}
    earnings = (data.get("earnings_dates") or {}).get(ticker) or {}
    earnings_call = (data.get("earnings_call_context") or {}).get(ticker) or {}
    news = []
    for article in ((data.get("news_data") or {}).get(ticker) or [])[:3]:
        news.append({
            "date": article.get("date", ""),
            "title": article.get("title", ""),
            "url": article.get("url", ""),
        })
    context = {
        "earnings": {
            "date": earnings.get("date", ""),
            "days_until": earnings.get("days_until", ""),
        },
        "earnings_call": earnings_call,
        "macro_calendar": [
            {
                "date": item.get("date") or item.get("date_range", ""),
                "event": item.get("event", ""),
                "impact": item.get("impact", ""),
            }
            for item in (data.get("macro_calendar") or [])[:10]
        ],
        "news": news,
        "technicals": {
            "rsi_14": technicals.get("rsi_14"),
            "change_5d": technicals.get("change_5d"),
            "atr_pct": technicals.get("atr_pct"),
            "volume_ratio_10d": technicals.get("volume_ratio_10d"),
        },
        "insiders": {
            "status": fundamentals.get("insider_data_status", "unavailable"),
            "buy_value_90d": insider_summary.get("buy_value_90d"),
            "sell_value_90d": insider_summary.get("sell_value_90d"),
            "planned_sell_value_90d": insider_summary.get("planned_sell_value_90d"),
            "discretionary_sell_value_90d": insider_summary.get("discretionary_sell_value_90d"),
            "planned_buy_value_90d": insider_summary.get("planned_buy_value_90d"),
            "discretionary_buy_value_90d": insider_summary.get("discretionary_buy_value_90d"),
            "other_90d": insider_summary.get("recent_other_90d"),
            "signal": fundamentals.get("insider_signal"),
            "samples": (fundamentals.get("insider_transactions") or [])[:3],
        },
        "analysts": {
            "summary": analyst.get("rec_summary"),
            "price_targets": analyst.get("price_targets"),
            "recent_changes": (analyst.get("recent_changes") or [])[:5],
        },
    }
    return json.dumps(context, ensure_ascii=False, separators=(",", ":"), default=str)


def _ensure_saturday_feedback(data: dict) -> tuple[dict, dict]:
    """Zapewnia jednorazowe rozliczenie także w testach renderera wywołanego wprost."""
    evaluation = data.get("prediction_evaluation")
    if evaluation is None:
        evaluation = evaluate_previous_predictions(dry_run=not data.get("persist", True))
        data["prediction_evaluation"] = evaluation
    feedback = data.get("prediction_feedback")
    if feedback is None:
        feedback = load_accuracy_feedback(limit=20)
        data["prediction_feedback"] = feedback
    return evaluation, feedback


def _enrich_predictions(data: dict, predictions: list[dict], source: str) -> list[dict]:
    """Dodaje ATR i kontekst wejściowy bez zmiany parsowalnego formatu raportu."""
    for prediction in predictions:
        ticker = prediction.get("ticker")
        details = (data.get("portfolio_details") or {}).get(ticker, {}) or {}
        technicals = details.get("technicals") or {}
        prediction["source"] = source
        prediction["atr_pct"] = technicals.get("atr_pct")
        prediction["forecast_context"] = _prediction_context(data, ticker)
    return predictions


def _build_prompt_v2(data: dict) -> str:
    """Prompt Gemini dla 5 sekcji narracyjnych layoutu v2."""
    movers = data["today_movers"]
    today = data["now"].date()
    macro_cal_lines = "\n".join(
        f"    - {e.get('date') or e.get('date_range', 'b/d')} ({e.get('country', 'b/d')}): "
        f"{e.get('event', 'b/d')} [waga: {e.get('impact', 'b/d')}]"
        for e in data["macro_calendar"][:10]
    ) or "    - brak"

    portfolio_label = ", ".join(list(US_TICKERS) + list(GPW_TICKERS_MAP.values()))

    is_saturday = data.get("is_saturday", False)
    report_type_note = ""
    saturday_sections = ""
    feedback = data.get("prediction_feedback") or {"summary": "Brak historii prognoz.", "lessons": []}
    evaluation = data.get("prediction_evaluation") or {}
    evaluation_lines = "\n".join(
        _evaluation_prompt_line(data, item) for item in (evaluation.get("new") or [])
    ) or "    - brak nowych rozliczeń"
    lesson_lines = "\n".join(f"    - {lesson}" for lesson in feedback.get("lessons", [])) or "    - brak"
    if is_saturday:
        report_type_note = (
            "\n    Typ raportu: RAPORT SOBOTNI (podsumowanie tygodnia). W nagłówkach sekcji "
            "Monitoring spółek podawaj zmianę TYGODNIOWĄ (pole 'zmiana 5 sesji' w danych "
            "technicznych), a ruchy oceniaj w perspektywie całego tygodnia.\n"
        )
        saturday_sections = f"""
    {PREDICTION_REVIEW_HEADER}
    Przeanalizuj każdą nowo rozliczoną prognozę Gemini z listy poniżej. Używaj wyłącznie
    dostarczonych danych i linków. Jeśli nie ma dowodu na katalizator, napisz to wprost.
    Dla każdej spółki podaj DWA powody: (1) czy teza się sprawdziła i dlaczego,
    (2) co przeważyło za wzrostem albo spadkiem w tym tygodniu.
    Każdy wiersz MUSI mieć format:
    - YYYY-MM-DD TICKER: wynik=trafiona|nietrafiona; wyjaśnienie=...; przeważyło=...; lekcja=...

    {PREDICTIONS_HEADER}
    Dodaj prognozę dla KAŻDEJ aktywnej spółki portfela na horyzont 5 sesji.
    Połącz technikę z newsami, wynikami, rekomendacjami, insiderami, kalendarzem makro
    i innymi informacjami nadchodzącymi. Nie twórz katalizatora, którego nie ma w danych.
    Format każdej prognozy MUSI być:
    - TICKER: kierunek=up/down/neutral; horyzont=5 sesji; pewność=niska|średnia|wysoka; katalizatory=...; ryzyka=...; unieważnienie=...; teza=jednozdaniowa teza.
    Nie używaj kierunku=neutral, gdy ATR% > 5, chyba że teza podaje konkretny przedział cenowy
    (np. 100-110). Goły neutral przy wysokim ATR to błąd.
    """

    n_sections = "osiem" if is_saturday else "sześć"
    return f"""
    Jesteś starszym analitykiem rynków kapitałowych (NASDAQ i GPW). Piszesz po polsku,
    zwięźle, profesjonalnym językiem finansowym, bez marketingowego entuzjazmu.
    Dzisiejsza data: {today}.
    {report_type_note}
    Wygeneruj WYŁĄCZNIE {n_sections} sekcji Markdown o DOKŁADNIE tych nagłówkach (nic przed, nic po,
    bez głównego tytułu raportu):

    ## 1. Executive Summary
    5-7 zwięzłych punktów. Każdy punkt MUSI wskazywać ticker albo datę makro — bez ogólników
    o "sentymencie rynkowym" bez liczby. Wykorzystaj pola "co się zmieniło od poprzedniego raportu".
    Każdy punkt oznacz wagą: 🔴 (wysoki wpływ), 🟠 (średni), 🟢 (niski).

    ## 4. Monitoring spółek
    Dla KAŻDEJ spółki z portfela ({portfolio_label}) zwięzły blok narracyjny:
    - nagłówek: **Nazwa (TICKER) [zmiana%, Cena: X USD/PLN]:**
    - ruch kursu w kontekście wolumenu (pole "wolumen vs średnia"), newsy wplecione w tekst jako
      linki markdown [Tytuł newsa](URL); każdy istotny news oceń: *wpływ na tezę: wzmacnia /
      osłabia / neutralny*;
    - rekomendacje analityków, zmiany ceny docelowej, transakcje insiderów, nadchodzące wyniki —
      jeśli są w danych;
    - blok zakończ linią: **Wpływ: pozytywny / neutralny / negatywny** (jedna ocena łączna).

    ## 5. Top 5 wydarzeń makro
    Wymieszaj dwa rodzaje pozycji w jednej liście (dokładnie 5 pozycji):
    (a) bieżące wydarzenia makro z sekcji NEWSY MAKRO I INDEKSOWE — opisz narracyjnie
    (1-2 zdania, dlaczego to ważne dla rynku) i wpleć link markdown [Tytuł newsa](URL);
    (b) nadchodzące publikacje i posiedzenia z sekcji KALENDARZ MAKRO (CPI, PPI, payrolls,
    PCE, GDP, sprzedaż detaliczna, FOMC, RPP) — podaj datę i napisz, czego rynek oczekuje
    lub co jest stawką. Posortuj od najważniejszego; każdą pozycję oznacz wagą 🔴/🟠/🟢;
    NIE wymyślaj wydarzeń spoza dostarczonych danych.

    ## 7. Ryzyka
    3-6 punktów TYLKO z datą albo progiem w danych (FOMC/CPI, earnings ≤7 dni, short >10%,
    konkretny news regulacyjny). Bez spekulacji. Każde ryzyko z wagą 🔴/🟠/🟢.

    ## 9. Sentyment
    4-6 zdań o RYNKU (nie recytuj spółek portfela punkt po punkcie — to jest sekcja 4).
    Nazwij źródła: VIX, insiderzy łącznie, wolumen, kierunek rekomendacji, ton newsów.
    Werdykt: news flow pozytywny/negatywny/mieszany i czy sentyment się zmienia.

    ## 10. Watchlist
    TYLKO spółki SPOZA portfela (top movers / radar). Portfel jest w sekcji 4 — nie powtarzaj go.
    Dla każdej: dlaczego, katalizator, na co zwrócić uwagę (poziom, wskaźnik, data).
    Jeśli nie ma sensownego kandydata poza portfelem, napisz to wprost.
    {saturday_sections}
    ZASADY (bezwzględnie przestrzegaj):
    - NIE wymyślaj newsów, wydarzeń ani danych spoza dostarczonych poniżej.
    - Brak katalizatora ruchu -> napisz wprost: "brak jednoznacznego katalizatora w dostępnych informacjach".
    - Podawaj konkretne liczby (RSI, P/E, short float, ceny docelowe) — masz je w danych.
    - Linki do newsów ZAWSZE w formacie markdown [Tytuł newsa](URL).
    - Spółki z danymi oznaczonymi [STALE] oznaczaj gwiazdką (*).
    - Ruch przy wolumenie >1.5x średniej opisuj jako potwierdzony; <0.7x jako mało istotny.
    - Wartości 0.00% opisuj neutralnie, bez znaku plus.
    - Transakcji insiderów NIE opisuj jako "0 USD", gdy w danych jest kwota > 0 albo status b/d/unavailable.
      "brak zakupów" to nie to samo co zero obrotu — jeśli jest sprzedaż, podaj jej kwotę.
    - Sprzedaż zaplanowana (10b5-1 / formularz) to słabszy sygnał niż nagła sprzedaż uznaniowa.
      Grantów i awardów nie nazywaj sprzedażą.

    === DANE ===

    INDEKSY (poziom, zmiana dzienna):
{_fmt_quotes_block(data["v2"]["index_quotes"])}

    MAKRO DASHBOARD:
{_fmt_quotes_block(data["macro_quotes"])}

    KALENDARZ MAKRO:
{macro_cal_lines}

    SPÓŁKI PORTFELA (jedyny blok: notowania, technika, fundamenty, insiderzy, newsy z datą):
{_company_data_block(data)}

    NADCHODZĄCE WYNIKI:
{_fmt_earnings(data.get("earnings_dates") or {})}

    NEWSY MAKRO I INDEKSOWE (spoza portfela — do sekcji 5):
{_macro_news_block(data)}

    SYGNAŁY SENTYMENTU (proxy):
{_sentiment_data_block(data)}

    HISTORIA TRAFNOŚCI I LEKCJE:
    Podsumowanie: {feedback.get('summary', 'b/d')}
    Ostatnie lekcje:
{lesson_lines}

    NOWE ROZLICZENIA DO ANALIZY:
{evaluation_lines}

    TOP MOVERS USA:
{_fmt_movers(movers["us_winners"] + movers["us_losers"])}
    TOP MOVERS GPW:
{_fmt_movers(movers["gpw_winners"] + movers["gpw_losers"])}
    TOP MOVERS SMALL-CAP AI:
{_fmt_movers(movers["sc_winners"] + movers["sc_losers"])}
    """


def _week_facts_line(data: dict, ticker: str) -> str:
    """Fakty z bieżącego raportu: ruch 5 sesji, wolumen, 1-3 newsy."""
    details = (data.get("portfolio_details") or {}).get(ticker, {}) or {}
    tech = details.get("technicals") or {}
    parts = []
    change = tech.get("change_5d")
    if change is not None:
        parts.append(f"zmiana 5 sesji {change:+.2f}%")
    vol = tech.get("volume_ratio_10d")
    if vol is not None:
        parts.append(f"wolumen {vol:.2f}x")
    news = []
    for article in ((data.get("news_data") or {}).get(ticker) or [])[:3]:
        title = article.get("title") or ""
        url = article.get("url") or ""
        if title:
            news.append(f"{title}" + (f" ({url})" if url else ""))
    if news:
        parts.append("newsy: " + " | ".join(news))
    return "; ".join(parts) if parts else "brak jednoznacznego katalizatora w dostępnych informacjach"


def _evaluation_prompt_line(data: dict, item: dict) -> str:
    ticker = item.get("ticker", "b/d")
    return (
        f"    - {item.get('prediction_date', 'b/d')} {ticker}: "
        f"źródło={item.get('source', 'b/d')}, kierunek={item.get('direction', 'b/d')}, "
        f"cena={item.get('base_price', 'b/d')}→{item.get('horizon_price', 'b/d')}, "
        f"ruch={item.get('realized_pct', 'b/d')}%, próg_ATR={item.get('threshold_pct', 'b/d')}%, "
        f"wynik={'trafiona' if item.get('hit') else 'nietrafiona'}; "
        f"teza={item.get('thesis', '')}; "
        f"kontekst_z_dnia_prognozy={item.get('forecast_context') or 'b/d'}; "
        f"tydzień={_week_facts_line(data, ticker)}"
    )


def _saturday_extras_md(data: dict) -> str:
    """Sobotnie sekcje deterministyczne: trend + trafność prognoz."""
    evaluation, _feedback = _ensure_saturday_feedback(data)
    md = generate_trend_analysis_section(data["portfolio_details"], data["active_tickers"])
    md += "\n" + format_accuracy_section(evaluation)
    return md


def _humanize_predictions_section(section: str) -> str:
    """Tabela czytelna w mailu + oryginalne bullety dla parsera accuracy_tracker."""
    if not section:
        return section
    lines = section.splitlines()
    header = []
    bullets = []
    rest_pre = []
    seen_header = False
    for line in lines:
        if line.startswith(PREDICTIONS_HEADER):
            header.append(line)
            seen_header = True
            continue
        if seen_header and re.search(r"kierunek\s*[:=]\s*(up|down|neutral)", line, re.I):
            bullets.append(line)
        elif seen_header and not bullets:
            rest_pre.append(line)
        elif seen_header:
            rest_pre.append(line)
        else:
            header.append(line)

    if not bullets:
        return section

    table = [
        "| Spółka | Kierunek | Pewność | Teza |",
        "| :--- | :--- | :--- | :--- |",
    ]
    dir_pl = {"up": "wzrost", "down": "spadek", "neutral": "neutral"}
    for line in bullets:
        ticker_m = re.search(r"\b([A-Z]{1,6}(?:\.[A-Z]{1,3})?)\b", line)
        dir_m = re.search(r"kierunek\s*[:=]\s*(up|down|neutral)", line, re.I)
        conf_m = re.search(
            r"(?:pewność|pewnosc|confidence)\s*[:=]\s*(niska|średnia|srednia|wysoka|low|medium|high)",
            line,
            re.I,
        )
        thesis_m = re.search(r"teza\s*[:=]\s*(.+)$", line, re.I)
        ticker = ticker_m.group(1) if ticker_m else "b/d"
        direction = dir_pl.get((dir_m.group(1).lower() if dir_m else ""), "b/d")
        confidence = conf_m.group(1) if conf_m else "b/d"
        thesis = (thesis_m.group(1).strip() if thesis_m else line.strip("- ").strip())
        table.append(f"| **{ticker}** | {direction} | {confidence} | {thesis} |")

    out = header + rest_pre + [""] + table + ["", "*Format rozliczeniowy (parser):*", ""] + bullets
    return "\n".join(out)


def _source_footer(data: dict) -> str:
    source = data.get("analysis_source") or "b/d"
    n_chars = data.get("report_chars")
    extra = f", {n_chars} znaków" if n_chars else ""
    return f"*Źródło analizy: {source}{extra}.*"


def _assemble_report(data: dict, ai_sections: dict) -> str:
    """Składa finalny raport v2: nagłówek + pozycje + sekcje 1-10 + disclaimer."""
    predictions_md = _humanize_predictions_section(ai_sections.get(PREDICTIONS_HEADER, ""))
    parts = [
        _header_v2(data["now"], data["skip"], data["status"]).rstrip("\n"),
        "",
        build_positions_md(data),
        "---",
        "",
        ai_sections["## 1. Executive Summary"],
        "",
        "---",
        "",
        build_benchmarks_md(data),
        "---",
        "",
        build_macro_md(data),
        "---",
        "",
        ai_sections["## 4. Monitoring spółek"],
        "",
        "---",
        "",
        ai_sections["## 5. Top 5 wydarzeń makro"],
        "",
        "---",
        "",
        build_movers_md(data),
        "---",
        "",
        ai_sections["## 7. Ryzyka"],
        "",
        "---",
        "",
        build_valuation_md(data),
        "---",
        "",
        ai_sections["## 9. Sentyment"],
        "",
        "---",
        "",
        ai_sections["## 10. Watchlist"],
        "",
        "---",
        "",
    ]
    if data.get("is_saturday"):
        parts.extend([
            _saturday_extras_md(data),
            ai_sections.get(PREDICTION_REVIEW_HEADER, ""),
            "",
            predictions_md,
            "",
            "---",
            "",
        ])
    parts.append(
        "> **Disclaimer**: Raport ma charakter informacyjny i nie stanowi rekomendacji inwestycyjnej."
    )
    parts.append("")
    parts.append(_source_footer(data))
    return "\n".join(parts)


def render_ai_report_v2(data: dict, api_key: str) -> str:
    """Renderuje raport v2 przez Gemini. Rzuca wyjątek przy niepowodzeniu
    (report_builder.build_report łapie go i woła render_basic_report_v2)."""
    is_saturday = data.get("is_saturday", False)
    headers = V2_AI_HEADERS + ([PREDICTION_REVIEW_HEADER, PREDICTIONS_HEADER] if is_saturday else [])
    prompt = _build_prompt_v2(data)
    response_text, model_name = _call_gemini_v2(prompt, api_key)
    data["analysis_source"] = f"Gemini {model_name}"
    ai_sections = _parse_ai_sections(response_text, headers)
    gemini_predictions = []
    if is_saturday:
        gemini_predictions = extract_predictions_from_report(
            response_text, data["active_tickers"], data["current_prices"]
        )
        missing = sorted(set(data["active_tickers"]) - {p.get("ticker") for p in gemini_predictions})
        if missing:
            raise ValueError(
                "Gemini nie wygenerował prognoz dla aktywnych spółek: " + ", ".join(missing)
            )
    final_report = _assemble_report(data, ai_sections)

    # Zapis prognoz do trackera — tylko w sobotę, nie w podglądzie.
    if is_saturday and data.get("persist", True):
        baseline_details = {
            ticker: data["portfolio_details"].get(ticker, {})
            for ticker in data["active_tickers"]
        }
        technical_predictions = build_rule_based_predictions(baseline_details)
        _enrich_predictions(data, technical_predictions, "rule_based_pre_gemini")
        _enrich_predictions(data, gemini_predictions, "gemini_report")
        save_predictions(technical_predictions + gemini_predictions)
        record_prediction_reviews(extract_prediction_reviews(final_report))
    return final_report


# ============================================================
# Fallback regułowy (bez AI)
# ============================================================

def _basic_executive_summary(data: dict) -> str:
    """Regułowe Executive Summary: najlepszy/najgorszy z portfela, delty, makro, VIX."""
    lines = ["## 1. Executive Summary", ""]

    changes = []
    for t in data["active_tickers"]:
        q = (data["portfolio_details"].get(t, {}) or {}).get("quote") or {}
        if q.get("change_pct") is not None:
            changes.append((t, q["change_pct"]))
    if changes:
        best = max(changes, key=lambda x: x[1])
        worst = min(changes, key=lambda x: x[1])
        lines.append(
            f"- 🟠 Najlepsza spółka portfela: **{best[0]}** ({format_change(best[1])}); "
            f"najsłabsza: **{worst[0]}** ({format_change(worst[1])})."
        )

    n_deltas = 0
    for t in data["active_tickers"]:
        tech = (data["portfolio_details"].get(t, {}) or {}).get("technicals") or {}
        deltas = calculate_deltas(
            t,
            {
                "rsi_14": tech.get("rsi_14"),
                "price_vs_sma20": tech.get("price_vs_sma20"),
                "price_vs_sma50": tech.get("price_vs_sma50"),
                "macd_histogram": tech.get("macd_histogram"),
                "bollinger_position": tech.get("bollinger_position"),
            },
            (data.get("last_snapshots") or {}).get(t),
        )
        n_deltas += len(deltas)
    lines.append(
        f"- 🟠 Zmiany techniczne od poprzedniego raportu: {n_deltas} "
        f"przekroczeń progów (szczegóły w sekcji 4)."
        if n_deltas else "- 🟢 Brak istotnych zmian technicznych od poprzedniego raportu."
    )

    high_events = [e for e in data["macro_calendar"] if e.get("impact") == "high" and e.get("date")]
    if high_events:
        e = high_events[0]
        lines.append(f"- 🔴 Najbliższe wydarzenie makro wysokiej wagi: {e['date']} ({e['country']}) — {e['event']}.")

    vix = (data.get("macro_quotes") or {}).get("VIX") or {}
    if vix.get("price") is not None:
        mood = "risk-on" if vix["price"] < 15 else "risk-off" if vix["price"] > 25 else "neutralny"
        lines.append(f"- 🟠 VIX {vix['price']:.2f} ({format_change(vix.get('change_pct', 0))}) — sentyment {mood}.")

    lines.append("")
    lines.append("")
    return "\n".join(lines)


def _basic_company_impact(q: dict, tech: dict) -> str:
    """Regułowa ocena wpływu dnia: pozytywny/neutralny/negatywny."""
    change = q.get("change_pct")
    vol = tech.get("volume_ratio_10d")
    if change is not None and change > 2 and (vol or 0) > 1.2:
        return "pozytywny"
    if change is not None and change < -2:
        return "negatywny"
    return "neutralny"


def _basic_monitoring(data: dict) -> str:
    """Regułowa sekcja 4: fakty per spółka + ocena wpływu."""
    lines = ["## 4. Monitoring spółek", ""]
    for t in data["active_tickers"]:
        details = data["portfolio_details"].get(t, {}) or {}
        q = details.get("quote") or {}
        tech = details.get("technicals") or {}
        name = TICKER_NAMES.get(t, t)
        if q.get("price") is None:
            lines.append(f"### {name} ({t}) -- brak danych")
            lines.append("")
            continue
        ccy = "PLN" if t.endswith(".WA") else "USD"
        lines.append(f"### {name} ({t}) -- {q['price']:.2f} {ccy} ({format_change(q.get('change_pct', 0))})")
        vol = tech.get("volume_ratio_10d")
        if vol is not None:
            vol_note = "potwierdzony przez rynek" if vol > 1.5 else "o niskiej istotności" if vol < 0.7 else "przy typowym wolumenie"
            lines.append(f"Wolumen {vol}x średniej 10 sesji — ruch {vol_note}.")
        for a in (data.get("news_data") or {}).get(t, [])[:3]:
            if a.get("url"):
                lines.append(f"- [{a.get('publisher')}] [{a.get('title')}]({a['url']})")
            else:
                lines.append(f"- [{a.get('publisher')}] {a.get('title')}")
        recs = (data.get("v2", {}).get("analyst_recs") or {}).get(t, {})
        rec_lines = _fmt_analyst_recs(t, recs, q.get("price"))
        for rl in rec_lines:
            if "brak danych analitycznych" not in rl:
                lines.append(f"- {rl.strip()}")
        note = TICKER_NOTES.get(t)
        if note:
            lines.append(f"> {note}")
        lines.append(f"**Wpływ: {_basic_company_impact(q, tech)}**")
        lines.append("")
    lines.append("")
    return "\n".join(lines)


def _basic_macro_top5(data: dict) -> str:
    """Regułowa sekcja 5: do 5 wydarzeń z kalendarza makro (high-impact najpierw)."""
    lines = ["## 5. Top 5 wydarzeń makro", ""]
    today = data["now"].date()
    events = sorted(
        [e for e in data["macro_calendar"] if e.get("date") or e.get("date_range")],
        key=lambda e: (0 if e.get("impact") == "high" else 1, e.get("date") or ""),
    )
    for event in events[:5]:
        lines.append(_calendar_line(event, today))
    if len(lines) == 2:
        lines.append("- brak wydarzeń makro w danych")
    lines.append("")
    lines.append("")
    return "\n".join(lines)


def _basic_risks(data: dict) -> str:
    """Regułowa sekcja 7: ryzyka z kalendarza makro, short interest i bliskich wyników."""
    lines = ["## 7. Ryzyka", ""]
    for e in data["macro_calendar"]:
        if e.get("impact") == "high" and e.get("date"):
            lines.append(f"- 🔴 {e['date']} ({e['country']}): {e['event']} — możliwa zmienność.")
    for t in data["active_tickers"]:
        fund = (data["portfolio_details"].get(t, {}) or {}).get("fundamentals") or {}
        short_pct = fund.get("short_pct_float")
        if short_pct is not None and short_pct <= 1.0:
            short_pct = round(short_pct * 100.0, 1)
        if short_pct is not None and short_pct > 10:
            lines.append(f"- 🟠 **{t}**: wysoki short interest ({short_pct}% floatu) — ryzyko gwałtownych ruchów.")
    for t, info in (data.get("earnings_dates") or {}).items():
        days = info.get("days_until")
        if days is not None and 0 <= days <= 7:
            lines.append(f"- 🟠 **{t}**: publikacja wyników {info.get('date')} (za {days} dni) — ryzyko luki cenowej.")
    if len(lines) == 2:
        lines.append("- 🟢 Brak zidentyfikowanych ryzyk w dostępnych danych.")
    lines.append("")
    lines.append("")
    return "\n".join(lines)


def _basic_sentiment(data: dict) -> str:
    """Regułowa sekcja 9: sygnały proxy + werdykt na bazie VIX."""
    lines = ["## 9. Sentyment", ""]
    block = _sentiment_data_block(data)
    lines.extend(line.strip() and f"- {line.strip()}" or "" for line in block.splitlines())
    vix = (data.get("macro_quotes") or {}).get("VIX") or {}
    if vix.get("price") is not None:
        verdict = (
            "risk-on (niska zmienność implikowana)" if vix["price"] < 15
            else "risk-off (podwyższona zmienność)" if vix["price"] > 25
            else "neutralny"
        )
        lines.append("")
        lines.append(f"**Werdykt (regułowy, wg VIX): {verdict}.**")
    lines.append("")
    lines.append("")
    return "\n".join(lines)


def _basic_watchlist(data: dict) -> str:
    """Regułowa sekcja 10: tylko spółki spoza portfela (top movers)."""
    lines = ["## 10. Watchlist", ""]
    portfolio = set(data.get("active_tickers") or [])
    entries = []
    movers = data.get("today_movers") or {}
    for group in ("us_winners", "us_losers", "gpw_winners", "gpw_losers", "sc_winners", "sc_losers"):
        for ticker, q in movers.get(group) or []:
            if ticker in portfolio:
                continue
            change = q.get("change_pct")
            vol = q.get("volume_ratio")
            vol_txt = f", wolumen {vol:.2f}x" if vol is not None else ""
            note = RADAR_NOTES.get(ticker)
            note_txt = f" ({note})" if note else ""
            entries.append(
                f"- **{TICKER_NAMES.get(ticker, ticker)}** ({ticker}): "
                f"{format_change(change) if change is not None else 'b/d'}{vol_txt}{note_txt} "
                f"— poza portfelem, obserwuj kontynuację ruchu."
            )
    # unikalne, max 6
    seen = set()
    unique = []
    for item in entries:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
        if len(unique) >= 6:
            break
    lines.extend(unique or ["- Brak spółek spoza portfela wymagających szczególnej obserwacji."])
    lines.append("")
    lines.append("")
    return "\n".join(lines)


def _basic_prediction_reviews(data: dict) -> str:
    """Regułowa analiza trafień, gdy Gemini nie napisze recenzji."""
    evaluation, _feedback = _ensure_saturday_feedback(data)
    lines = [PREDICTION_REVIEW_HEADER, ""]
    items = [
        item for item in (evaluation.get("new") or [])
        if item.get("source") == "gemini_report"
    ] or list(evaluation.get("new") or [])
    if not items:
        lines.append("*Brak nowo rozliczonych prognoz do analizy.*")
        lines.append("")
        return "\n".join(lines)

    for item in items:
        ticker = item.get("ticker", "b/d")
        verdict = "trafiona" if item.get("hit") else "nietrafiona"
        realized = item.get("realized_pct")
        direction = item.get("direction") or "b/d"
        if realized is None:
            explanation = f"teza {direction} vs wynik {verdict}"
        else:
            try:
                realized_txt = f"{float(realized):+.2f}%"
            except Exception:
                realized_txt = str(realized)
            explanation = f"teza {direction} vs ruch {realized_txt} na koniec 5 sesji"
        driver = _week_facts_line(data, ticker)
        lesson = (
            "nie opierać werdyktu wyłącznie na technice, gdy brak katalizatora w newsach"
            if verdict == "nietrafiona" else
            "utrzymać wagę katalizatora, który potwierdził się w horyzoncie"
        )
        lines.append(
            f"- {item.get('prediction_date', 'b/d')} {ticker}: wynik={verdict}; "
            f"wyjaśnienie={explanation}; przeważyło={driver}; lekcja={lesson}"
        )
    lines.append("")
    lines.append("")
    return "\n".join(lines)


def _basic_predictions(data: dict) -> str:
    """Regułowa sobotnia sekcja prognoz (format parsowalny przez accuracy_tracker)."""
    baseline_details = {
        ticker: data["portfolio_details"].get(ticker, {})
        for ticker in data.get("active_tickers", data["portfolio_details"].keys())
    }
    predictions = build_rule_based_predictions(baseline_details)
    _enrich_predictions(data, predictions, "rule_based_pre_gemini")
    lines = [PREDICTIONS_HEADER, ""]
    lines.append("*Fallback techniczny: Gemini nie wygenerował prognozy wieloczynnikowej.*")
    lines.append("")
    for p in predictions:
        lines.append(
            f"- {p['ticker']}: kierunek={p['direction']}; "
            f"horyzont={p.get('horizon_days', 5)} sesji; teza={p['thesis']}"
        )
    if not predictions:
        lines.append("*Brak jednoznacznych sygnałów do prognoz w tym tygodniu.*")
    lines.append("")
    if data.get("persist", True):
        save_predictions(predictions)
    return "\n".join(lines)


def render_basic_report_v2(data: dict) -> str:
    """Pełny raport v2 bez AI — te same sekcje deterministyczne, narracyjne z reguł."""
    logger.info("Layout v2: generowanie raportu regułowego (fallback bez AI)...")
    data["analysis_source"] = "fallback regułowy"
    ai_sections = {
        "## 1. Executive Summary": _basic_executive_summary(data).rstrip("\n"),
        "## 4. Monitoring spółek": _basic_monitoring(data).rstrip("\n"),
        "## 5. Top 5 wydarzeń makro": _basic_macro_top5(data).rstrip("\n"),
        "## 7. Ryzyka": _basic_risks(data).rstrip("\n"),
        "## 9. Sentyment": _basic_sentiment(data).rstrip("\n"),
        "## 10. Watchlist": _basic_watchlist(data).rstrip("\n"),
    }
    if data.get("is_saturday"):
        review_md = _basic_prediction_reviews(data)
        ai_sections[PREDICTIONS_HEADER] = _basic_predictions(data).rstrip("\n")
        ai_sections[PREDICTION_REVIEW_HEADER] = review_md.rstrip("\n")
        if data.get("persist", True):
            record_prediction_reviews(extract_prediction_reviews(review_md))
    return _assemble_report(data, ai_sections)
