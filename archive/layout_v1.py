"""ARCHIWUM — layout v1 raportu (NIEUŻYWANY).

Kod zachowany wyłącznie referencyjnie po przejściu na layout v2 (layout_v2.py)
jako jedyny aktywny układ raportu (09.07.2026). NIE importować z produkcyjnego
kodu. Zawiera: dashboard portfela, sekcje I/II/III, renderer AI v1 z sekcją
"Prognozy do weryfikacji" i rendererem podstawowym.

Uwaga: generate_trend_analysis_section pozostała w report_builder.py — jest
nadal używana przez sobotni raport v2.
"""

import os
import time
from datetime import datetime, timedelta
from typing import Optional

try:
    from google import genai
except ImportError:
    genai = None

import logging

from data_fetching import (
    US_TICKERS, GPW_TICKERS_MAP, INDEX_TICKERS, RADAR_SMALLCAPS,
    TICKER_NAMES, TICKER_NOTES, US_BENCHMARK, PL_BENCHMARK,
    get_top_movers, fetch_earnings_dates, fetch_period_change,
)
from technicals import generate_technical_signal, generate_alerts, trend_label
from snapshot_store import calculate_deltas
from accuracy_tracker import (
    build_rule_based_predictions, evaluate_previous_predictions,
    extract_predictions_from_report, format_accuracy_section, save_predictions,
)
from report_builder import (
    _MARKET_LABELS, format_change, format_pct, _report_header,
    _v, _fmt_quote_line, _fmt_quotes_block, _fmt_movers, _fmt_earnings,
    _fmt_portfolio_block, generate_trend_analysis_section, collect_report_data,
)

logger = logging.getLogger(__name__)

def _build_dashboard_md(data: dict) -> str:
    """Dashboard portfela + (w sobotę) analiza trendu i trafność prognoz.

    UWAGA: wywoływać RAZ na raport — evaluate_previous_predictions() oznacza
    prognozy jako rozliczone (zapis do CSV).
    """
    deltas_label = "Co sie zmienilo w tym tygodniu" if data["is_saturday"] else "Co sie zmienilo od wczoraj"
    dashboard_md = generate_portfolio_dashboard(
        data["portfolio_details"],
        data["last_snapshots"],
        data["today_movers"],
        data["active_tickers"],
        data["earnings_dates"],
        deltas_label,
    )
    if data["is_saturday"]:
        dashboard_md += "\n" + generate_trend_analysis_section(data["portfolio_details"], data["active_tickers"])
        dashboard_md += "\n" + format_accuracy_section(
            evaluate_previous_predictions(dry_run=not data.get("persist", True))
        )
    return dashboard_md


def render_ticker_section(ticker: str, name: str, data: dict, news_data: dict,
                          earnings_call_context: dict, is_us: bool) -> list[str]:
    """Sekcja pojedynczej spółki (komentarz + earnings call + newsy) — wspólna dla US i GPW."""
    lines = []
    q = (data or {}).get("quote", {}) or {}

    if q.get("error") or q.get("price") is None:
        lines.append(f"### {name} ({ticker}) -- brak danych")
    else:
        change_str = format_change(q["change_pct"])
        price_str = f"${q['price']:.2f}" if is_us else f"{q['price']:.2f} PLN"
        lines.append(f"### {name} ({ticker}) -- {price_str} ({change_str})")

        comment = generate_rule_based_comment(ticker, data)
        lines.append(comment)
        lines.append("")

        call_items = earnings_call_context.get(ticker, [])
        if call_items:
            lines.append("**Kontekst earnings call / guidance:**")
            for item in call_items[:2]:
                title = item.get("title") or f"Transcript {item.get('quarter', '')} {item.get('year', '')}".strip()
                summary = item.get("summary") or ""
                url = item.get("url")
                if url:
                    lines.append(f"- [{title}]({url}) - {summary[:240]}")
                else:
                    lines.append(f"- {title}: {summary[:240]}")
            lines.append("")

        ticker_news = news_data.get(ticker, [])
        if ticker_news:
            lines.append("**Najnowsze wiadomości:**")
            for a in ticker_news:
                if a.get("url"):
                    lines.append(f"- [{a['publisher']}] [{a['title']}]({a['url']})")
                else:
                    lines.append(f"- [{a['publisher']}] {a['title']}")
            lines.append("")

    note = TICKER_NOTES.get(ticker)
    if note:
        lines.append(f"> {note}")
        lines.append("")

    lines.append("---")
    lines.append("")
    return lines


def render_basic_report(data: dict, dashboard_md: str) -> str:
    """Renderuje podstawowy raport rynkowy (fallback, bez AI) z zebranych danych."""
    logger.info("Generowanie podstawowego raportu...")

    us_active = data["us_active"]
    pl_active = data["pl_active"]
    portfolio_details = data["portfolio_details"]
    news_data = data["news_data"]
    earnings_call_context = data["earnings_call_context"]
    movers = data["today_movers"]
    us_winners, us_losers = movers["us_winners"], movers["us_losers"]
    gpw_winners, gpw_losers = movers["gpw_winners"], movers["gpw_losers"]
    sc_winners, sc_losers = movers["sc_winners"], movers["sc_losers"]

    lines = [_report_header(data["now"], data["skip"], data["status"]).rstrip("\n"), ""]
    lines.append(dashboard_md)

    # Kontekst indeksowy
    lines.append("## Kontekst indeksowy")
    lines.append("")
    lines.append("| Indeks | Zmiana dzienna | Poziom |")
    lines.append("|--------|---------------|--------|")
    for name, q in data["index_quotes"].items():
        if q.get("error"):
            lines.append(f"| **{name}** | b/d | b/d |")
        else:
            lines.append(f"| **{name}** | **{format_change(q['change_pct'])}** | {q['price']:,.2f} |")
    lines.append("")

    # Wskaźniki makro
    lines.append("### Wskaźniki makro")
    lines.append("| Wskaźnik | Poziom | Zmiana |")
    lines.append("|----------|--------|--------|")
    for name, q in data["macro_quotes"].items():
        if q.get("error"):
            lines.append(f"| {name} | b/d | b/d |")
        else:
            lines.append(f"| {name} | {q['price']:.2f} | {format_change(q['change_pct'])} |")

    lines.append("")
    lines.append("### Kalendarz makro na najblizsze dni")
    for event in data["macro_calendar"][:6]:
        event_date = event.get("date") or event.get("date_range") or "b/d"
        estimate = event.get("estimate")
        estimate_txt = f", konsensus: {estimate}" if estimate not in (None, "") else ""
        lines.append(f"- **{event_date}** ({event.get('country', 'b/d')}): {event.get('event', 'b/d')}{estimate_txt}")

    lines.append("")
    lines.append("---")
    lines.append("")

    # === US Stocks ===
    if us_active:
        lines.append("## I. Spółki USA -- Tech / Semiconductors")
        lines.append("")
        for ticker in US_TICKERS:
            name = TICKER_NAMES.get(ticker, ticker)
            lines.extend(render_ticker_section(
                ticker, name, portfolio_details.get(ticker, {}),
                news_data, earnings_call_context, is_us=True,
            ))

    # === GPW Stocks ===
    if pl_active:
        lines.append("## II. Spółki GPW -- Polska")
        lines.append("")
        for ticker, name in GPW_TICKERS_MAP.items():
            lines.extend(render_ticker_section(
                ticker, name, portfolio_details.get(ticker, {}),
                news_data, earnings_call_context, is_us=False,
            ))

    # === Radar rynkowy ===
    lines.append("## III. Radar rynkowy -- Najciekawsze ruchy")
    lines.append("")

    if us_active:
        lines.append("### USA -- Top movers")
        lines.append("")
    if us_winners or us_losers:
        lines.append("| Spółka | Kurs | Zmiana |")
        lines.append("|--------|------|--------|")
        for ticker, q in us_winners + us_losers:
            name_disp = TICKER_NAMES.get(ticker, ticker)
            lines.append(f"| **{name_disp}** ({ticker}) | ${q['price']:.2f} | {format_change(q['change_pct'])} |")
        lines.append("")

    if pl_active:
        lines.append("### GPW -- Top movers")
        lines.append("")
    if gpw_winners or gpw_losers:
        lines.append("| Spółka | Kurs | Zmiana |")
        lines.append("|--------|------|--------|")
        for ticker, q in gpw_winners + gpw_losers:
            name_disp = TICKER_NAMES.get(ticker, ticker)
            lines.append(f"| **{name_disp}** ({ticker}) | {q['price']:.2f} PLN | {format_change(q['change_pct'])} |")
        lines.append("")

    if us_active:
        lines.append("### Sektor AI Bottlenecks (Small/Mid-Caps) -- Top movers")
        lines.append("")
    if sc_winners or sc_losers:
        lines.append("| Spółka | Kurs | Zmiana |")
        lines.append("|--------|------|--------|")
        for ticker, q in sc_winners + sc_losers:
            name_disp = TICKER_NAMES.get(ticker, ticker)
            price_disp = f"${q['price']:.2f}" if ".DE" not in ticker and ".PA" not in ticker and ".L" not in ticker else f"{q['price']:.2f}"
            lines.append(f"| **{name_disp}** ({ticker}) | {price_disp} | {format_change(q['change_pct'])} |")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("> **Disclaimer**: Raport ma charakter informacyjny i nie stanowi rekomendacji inwestycyjnej.")

    # Prognozy generujemy i zapisujemy tylko w sobotę (raz w tygodniu); nie w podglądzie
    if data["is_saturday"] and data.get("persist", True):
        save_predictions(build_rule_based_predictions(portfolio_details))

    return "\n".join(lines)


def render_ai_report(data: dict, dashboard_md: str, api_key: str) -> str:
    """Renderuje raport przez Gemini z zebranych danych. Rzuca wyjątek przy niepowodzeniu."""
    is_saturday = data["is_saturday"]
    skip = data["skip"]
    portfolio_details = data["portfolio_details"]
    active_tickers = data["active_tickers"]
    current_prices = data["current_prices"]
    movers = data["today_movers"]
    us_winners, us_losers = movers["us_winners"], movers["us_losers"]
    gpw_winners, gpw_losers = movers["gpw_winners"], movers["gpw_losers"]
    sc_winners, sc_losers = movers["sc_winners"], movers["sc_losers"]

    # Notowania portfela per rynek (do prompta) — tylko rynki otwarte
    us_quotes = (
        {t: (portfolio_details.get(t, {}).get("quote") or {}) for t in US_TICKERS}
        if data["us_active"] else {}
    )
    gpw_quotes = (
        {t: (portfolio_details.get(t, {}).get("quote") or {}) for t in GPW_TICKERS_MAP}
        if data["pl_active"] else {}
    )

    weekly_data = ""
    if is_saturday:
        report_type = "RAPORT SOBOTNI (Piątkowa sesja + Podsumowanie Tygodnia)"

        # Realne zmiany TYGODNIOWE (zamknięcie vs zamknięcie sprzed tygodnia) —
        # change_pct z quote'ów jest zawsze 1-sesyjny i NIE nadaje się na tydzień.
        index_week_lines = []
        for name, ticker in INDEX_TICKERS.items():
            q = data["index_quotes"].get(name) or {}
            weekly = fetch_period_change(ticker, period="7d")
            index_week_lines.append(
                f"    - {name}: zamknięcie {q.get('price', 'b/d')}, "
                f"zmiana dzienna {_v(q.get('change_pct'), '%')}, zmiana tygodniowa {_v(weekly, '%')}"
            )

        portfolio_week_lines = []
        for t, d in portfolio_details.items():
            q = d.get("quote") or {}
            tech = d.get("technicals") or {}
            portfolio_week_lines.append(
                f"    - {TICKER_NAMES.get(t, t)} ({t}): zamknięcie {q.get('price', 'b/d')}, "
                f"zmiana dzienna {_v(q.get('change_pct'), '%')}, zmiana tygodniowa (5 sesji) {_v(tech.get('change_5d'), '%')}"
            )

        w_sc_winners, w_sc_losers = get_top_movers(RADAR_SMALLCAPS, top_n=5, period="5d")

        weekly_data = f"""
    DANE TYGODNIOWE (zmiana tygodniowa = zamknięcie vs zamknięcie sprzed tygodnia; to INNA wartość niż zmiana dzienna):
    INDEKSY TYDZIEŃ:
{chr(10).join(index_week_lines)}
    PORTFEL TYDZIEŃ:
{chr(10).join(portfolio_week_lines)}
    SMALL-CAP RADAR (ostatnia sesja):
{_fmt_movers(w_sc_winners + w_sc_losers)}
    """
    else:
        report_type = "RAPORT DZIENNY"

    # Data ostatniej sesji z realnych danych (a nie "wczoraj" kalendarzowo —
    # po świętach/weekendach etykieta była myląca)
    session_dates = [
        (d.get("quote") or {}).get("data_date")
        for d in portfolio_details.values()
        if (d.get("quote") or {}).get("data_date")
    ]
    session_date = max(session_dates) if session_dates else (data["now"] - timedelta(days=1)).strftime("%Y-%m-%d")

    # Formatuj newsy do czytelnej postaci dla prompta
    news_context = ""
    if data["news_data"]:
        news_lines = []
        for ticker, articles in data["news_data"].items():
            name = TICKER_NAMES.get(ticker, ticker)
            news_lines.append(f"\n--- {name} ({ticker}) ---")
            for a in articles:
                news_lines.append(f"  • [{a['publisher']}] {a['title']} | Link: {a.get('url', '')}")
                if a.get('summary'):
                    news_lines.append(f"    Streszczenie: {a['summary']}")
        news_context = "\n".join(news_lines)

    # Kalendarz makro — zwięzłe linie zamiast repr() listy słowników
    macro_cal_lines = "\n".join(
        f"    - {e.get('date') or e.get('date_range', 'b/d')} ({e.get('country', 'b/d')}): "
        f"{e.get('event', 'b/d')}" + (f", konsensus: {e['estimate']}" if e.get("estimate") not in (None, "") else "")
        for e in data["macro_calendar"][:10]
    ) or "    - brak"

    call_lines = []
    for t, items in (data["earnings_call_context"] or {}).items():
        call_lines.append(f"    {t}:")
        for it in items[:2]:
            title = it.get("title") or f"Transcript Q{it.get('quarter', '?')} {it.get('year', '')}".strip()
            call_lines.append(f"      - {title}: {(it.get('summary') or '')[:400]}")
    call_context_txt = "\n".join(call_lines) if call_lines else "    - brak"

    raw_data = f"""
    DATA OSTATNIEJ SESJI W DANYCH: {session_date}

    MAKRO DASHBOARD (VIX, rentowności, USD):
{_fmt_quotes_block(data["macro_quotes"])}

    KALENDARZ MAKRO NA NAJBLIZSZE 10 DNI:
{macro_cal_lines}

    INDEKSY GŁÓWNE:
{_fmt_quotes_block(data["index_quotes"])}

    DANE DZIENNE PORTFELA (wynik z ostatniej sesji):
    USA PORTFEL:
{_fmt_quotes_block(us_quotes)}
    GPW PORTFEL:
{_fmt_quotes_block(gpw_quotes)}

    WSKAŹNIKI I ANALIZA PORTFELA (RSI, SMA, MACD, trend, P/E, 52w, Alpha, Insiderzy, Short interest):
{_fmt_portfolio_block(portfolio_details)}

    NADCHODZĄCE EARNINGS DLA PORTFELA:
{_fmt_earnings(data["earnings_dates"])}

    KONTEKST EARNINGS CALL / GUIDANCE / TRANSKRYPTY:
{call_context_txt}

    TOP MOVERS USA (NASDAQ-100):
    Zyskujący:
{_fmt_movers(us_winners)}
    Tracący:
{_fmt_movers(us_losers)}

    TOP MOVERS GPW (GPW-100):
    Zyskujący:
{_fmt_movers(gpw_winners)}
    Tracący:
{_fmt_movers(gpw_losers)}

    SMALL-CAP AI BOTTLENECKS RADAR:
    Zyskujący:
{_fmt_movers(sc_winners)}
    Tracący:
{_fmt_movers(sc_losers)}

    NEWSY I KONTEKST INFORMACYJNY (z Google News RSS + Yahoo Finance):
    {news_context}

    {weekly_data}
    """

    # Dynamiczna lista spółek portfela do promptów — zawsze zgodna z konfiguracją (watchlists.json)
    portfolio_label = ", ".join(list(US_TICKERS) + list(GPW_TICKERS_MAP.values()))

    if is_saturday:
        prompt_structure = f"""
    Struktura raportu:
    1. **Kontekst indeksowy**: Tabela z wynikami (pokaż wynik piątkowy oraz w nawiasie wynik tygodniowy) i ocena obu okresów.
    2. **Najważniejsze wydarzenia piątkowej sesji (Portfel)**: Dla KAŻDEJ spółki z portfela ({portfolio_label}) dwa elementy:
       - Akapit otwierany pogrubionym nagłówkiem w tej samej linii: **Nazwa (TICKER) [zmiana tygodniowa%, Cena: X USD/PLN]:** po którym następuje narracyjne podsumowanie zachowania spółki w tym tygodniu ZBUDOWANE NA PODSTAWIE WIADOMOŚCI — wpleć najważniejszy news bezpośrednio w tekst jako link markdown [Tytuł newsa](URL), wyjaśnij DLACZEGO kurs się zmienił i co to oznacza dla akcjonariuszy; oceniaj ruch w kontekście wolumenu. Gdy brak newsów wyjaśniających ruch — napisz to wprost.
       - Poniżej jedna linia: **Technicznie:** 1-2 zdania — tylko 2-3 NAJWAŻNIEJSZE dla tej spółki wskaźniki (np. RSI, układ średnich, ADX, Alpha vs benchmark) z wartościami i jasny werdykt, czy obraz techniczny jest dobry czy zły i dlaczego. NIE wypisuj wszystkich wskaźników.
    3. **Podsumowanie Tygodnia (Makro & Trendy)**: Merytoryczna ocena tego, co działo się przez cały tydzień (na podst. danych tygodniowych i makro dashboardu). Jakie były główne powody spadku/wzrostu indeksów, rentowności obligacji, dolara czy bitcoina?
    4. **AI Bottlenecks (Small/Mid Caps - Okiem Tygodnia)**: Wyłoń jednego-dwóch liderów tego sektora. Przeanalizuj fundamentalnie dlaczego ich rola jest kluczowa (upside potential).
    5. **Na co zwrócić uwagę w nadchodzącym tygodniu**: Wypisz 5 najważniejszych rzeczy, na które inwestorzy będą patrzeć od poniedziałku. Wymieszaj w jednej liście: nadchodzące publikacje i posiedzenia z sekcji KALENDARZ MAKRO (CPI, PPI, payrolls, PCE, GDP, sprzedaż detaliczna, FOMC, RPP — z datami) oraz tematy z dostarczonych NEWSÓW (narracyjnie, 1-2 zdania z linkiem markdown [Tytuł newsa](URL)). NIE wymyślaj wydarzeń spoza dostarczonych danych.
        """
    else:
        prompt_structure = f"""
    Struktura raportu:
    1. **Kontekst indeksowy**: Tabela i krótkie podsumowanie zachowania głównych indeksów. Dodaj krótki komentarz do wskaźników Makro (VIX, 10Y Treasury, dolar, Bitcoin).
    2. **Najważniejsze wydarzenia (Portfel)**: Dla KAŻDEJ spółki z portfela ({portfolio_label}) dwa elementy:
       - Akapit otwierany pogrubionym nagłówkiem w tej samej linii: **Nazwa (TICKER) [zmiana%, Cena: X USD/PLN]:** po którym następuje narracyjne podsumowanie ruchu ZBUDOWANE NA PODSTAWIE WIADOMOŚCI — wpleć najważniejszy news bezpośrednio w tekst jako link markdown [Tytuł newsa](URL), wyjaśnij DLACZEGO kurs się zmienił i co to oznacza dla akcjonariuszy; oceniaj ruch w kontekście wolumenu. Gdy brak newsów wyjaśniających ruch — napisz to wprost.
       - Poniżej jedna linia: **Technicznie:** 1-2 zdania — tylko 2-3 NAJWAŻNIEJSZE dla tej spółki wskaźniki (np. RSI, układ średnich, ADX, Alpha vs benchmark) z wartościami i jasny werdykt, czy obraz techniczny jest dobry czy zły i dlaczego. NIE wypisuj wszystkich wskaźników.
    3. **Radar rynkowy (Large Caps)**: Krótkie wspomnienie najciekawszych zyskujących i tracących z sekcji TOP MOVERS USA (NASDAQ-100) oraz TOP MOVERS GPW. Wskaż te ruchy i ewentualnie wyjaśnij je, jeśli newsy dają uzasadnienie.
    4. **AI Bottlenecks (Small/Mid Caps)**: Z dostarczonych danych "SMALL-CAP AI BOTTLENECKS RADAR" wypisz liderów wzrostów/spadków z tego sektora. Wybierz 1 lub 2 spółki z tej listy i napisz merytoryczny, silny komentarz o ich 'upside potential' w związku z wąskimi gardłami AI.
    5. **Top 5 wydarzeń makro**: Wymieszaj dwa rodzaje pozycji w jednej liście:
       (a) bieżące wydarzenia makro z dostarczonych NEWSÓW — opisz narracyjnie (1-2 zdania, dlaczego to ważne dla rynku) i wpleć link markdown [Tytuł newsa](URL);
       (b) nadchodzące publikacje i posiedzenia z sekcji KALENDARZ MAKRO (CPI, PPI, payrolls, PCE, GDP, sprzedaż detaliczna, FOMC, RPP) — podaj datę i napisz, czego rynek oczekuje lub co jest stawką.
       Posortuj od najważniejszego; NIE wymyślaj wydarzeń spoza dostarczonych danych.
    6. **Czynnik ryzyka (Wildcard)**: Wskaż jedną rzecz, która może pójść niezgodnie z oczekiwaniami rynku (podejście sceptyczne).
        """

    # Prognozy do weryfikacji generujemy tylko w sobotę (raz w tygodniu)
    if is_saturday:
        prompt_structure += """

    7. **Prognozy do weryfikacji**: Dodaj 3-6 krotkich prognoz dla spolek z portfela na horyzont 5 sesji.
    Format kazdej prognozy musi byc latwy do parsowania:
    - TICKER: kierunek=up/down/neutral; horyzont=5 sesji; teza=jednozdaniowe uzasadnienie oparte na danych.
        """

    # Informacja o zamkniętych rynkach (święta) dla modelu
    closed_markets_note = ""
    if skip:
        closed_labels = ", ".join(_MARKET_LABELS.get(m, m) for m in skip)
        closed_markets_note = (
            f"\n    UWAGA: Dziś zamknięte rynki (święto): {closed_labels}. "
            f"NIE analizuj spółek z tych rynków jako dzisiejszej sesji i pomiń je w raporcie.\n"
        )

    prompt = f"""
    Jesteś starszym analitykiem rynków kapitałowych z doświadczeniem (NASDAQ i GPW).
    Otrzymujesz surowe dane za podany okres.
    Typ raportu do wygenerowania: {report_type}

    Pisz zwięźle, profesjonalnym językiem finansowym, unikaj entuzjazmu.
    Otrzymujesz również najnowsze wiadomości (newsy) z Yahoo Finance dla spółek z portfela, indeksów i radaru.
    Wykorzystaj je, aby uzasadnić DLACZEGO dana spółka wzrosła lub spadła — nie tylko o ile procent.
    If news wyjaśnia ruch ceny, połącz te informacje bezpośrednio w analizie.
    Użyj formatu Markdown, upewnij się, że struktura jest czytelna.

    ZASADY ANALIZY (bezwzględnie przestrzegaj):
    - NIE pisz o ogólnych "obawach inflacyjnych" czy "niepewności geopolitycznej", chyba że masz KONKRETNY news to potwierdzający z dostarczonych danych.
    - Jeśli brak newsów wyjaśniających ruch ceny — napisz wprost: "brak jednoznacznego katalizatora w dostępnych informacjach".
    - Podawaj konkretne wartości liczbowe: RSI, Forward P/E, SMA, Alpha vs benchmark, short interest — masz je w dostarczonych danych.
    - Oznaczaj gwiazdką (*) spółki, dla których dane zostały oznaczone jako "stale" (przestarzałe).
    - NIE wymyślaj newsów ani wydarzeń, które nie zostały Ci dostarczone.
    - ZAWSZE wklejaj odpowiednie linki URL do newsów, o których piszesz, używając formatu markdown [Tytuł newsa](Link do newsa) z danych w sekcji NEWSY.
    - W tabeli kontekstu indeksowego nie umieszczaj kolumny z wolumenem (wolumen dla całego indeksu nie niesie przydatnych informacji).
    - Zmiana dzienna i zmiana tygodniowa to RÓŻNE wartości — używaj dokładnie tych podanych w danych i nigdy nie wpisuj tej samej liczby w obu kolumnach.
    - Wartości 0.00% opisuj jako neutralne i zapisuj BEZ znaku plus (np. "0.00%", nie "+0.00%").
    - Kapitalizacje spółek podane są w walucie notowania (GPW w PLN, USA w USD) — nie przeliczaj i nie zmieniaj waluty.
    - Ruchy cen oceniaj w kontekście wolumenu (pole "wolumen ... śr. 10d" w danych portfela): ruch przy wolumenie >1.5x średniej opisuj jako potwierdzony przez rynek; przy wolumenie <0.7x zaznaczaj, że ruch ma niską istotność.
    - Alpha portfela liczona jest względem benchmarku SEKTOROWEGO (USA: SMH — semiconductors, GPW: EPOL), nie szerokiego rynku.
    - NIE twórz głównego tytułu raportu (# RAPORT RYNKOWY...) ani sekcji oznaczających dashboard czy alerty na początku. Rozpocznij bezpośrednio od analizy: '## Kontekst indeksowy'.
    {closed_markets_note}
    Oto surowe dane z yfinance:
    {raw_data}

    {prompt_structure}
    """

    client = genai.Client(api_key=api_key)

    # Model konfigurowalny przez .env (GEMINI_MODEL). Domyślnie gemini-3.5-flash.
    # Można nadpisać bez zmiany kodu, np. gdy dany model ma limit 0 na free tier.
    model_name = os.getenv("GEMINI_MODEL") or "gemini-3.5-flash"
    logger.info(f"Używany model Gemini: {model_name}")

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"Próba {attempt}/{max_retries} — wysyłam zapytanie do Gemini...")
            response = client.models.generate_content(
                model=model_name,
                contents=prompt
            )
            if not response.text:
                raise Exception("Pusta odpowiedź z Gemini.")

            header = _report_header(data["now"], skip, data["status"])
            final_report = header + dashboard_md + "\n" + response.text

            # Prognozy generujemy i zapisujemy tylko w sobotę (raz w tygodniu); nie w podglądzie
            if is_saturday and data.get("persist", True):
                gemini_predictions = extract_predictions_from_report(
                    final_report,
                    active_tickers,
                    current_prices,
                )
                # ATR% z dnia prognozy — próg trafności normalizowany zmiennością spółki
                for pred in gemini_predictions:
                    technicals = (portfolio_details.get(pred.get("ticker"), {}) or {}).get("technicals") or {}
                    pred["atr_pct"] = technicals.get("atr_pct")
                save_predictions(gemini_predictions or build_rule_based_predictions(portfolio_details))
            return final_report
        except Exception as e:
            logger.warning(f"Próba {attempt} nieudana: {e}")
            # Błędy quota/limitu (429 / RESOURCE_EXHAUSTED) nie miną po krótkim retry —
            # przerywamy od razu i przechodzimy do fallbacku, zamiast czekać ~30s.
            err_text = str(e).lower()
            is_quota_error = (
                "resource_exhausted" in err_text
                or "429" in err_text
                or "quota" in err_text
                or "rate limit" in err_text
            )
            if is_quota_error:
                logger.info("Wykryto błąd limitu/quota — pomijam dalsze próby i przechodzę do fallbacku.")
                raise
            if attempt < max_retries:
                wait = attempt * 10
                logger.info(f"Czekam {wait}s przed kolejną próbą...")
                time.sleep(wait)
            else:
                raise


def build_basic_report(persist_state: bool = True) -> str:
    """Buduje podstawowy raport (bez AI) — zachowane dla kompatybilności."""
    data = collect_report_data(persist_state)
    return render_basic_report(data, _build_dashboard_md(data))


def generate_portfolio_dashboard(portfolio_details: dict, last_snapshots: dict, today_movers: dict,
                                 active_tickers: Optional[list] = None,
                                 earnings_dates: Optional[dict] = None,
                                 deltas_label: str = "Co sie zmienilo od wczoraj") -> str:
    """Create portfolio dashboard with RSI, SMA, MACD, Bollinger, alerts and day-over-day changes.

    active_tickers — lista spółek do pokazania (z pominięciem rynków zamkniętych dziś).
    Gdy None, pokazywany jest cały portfel.
    earnings_dates — słownik z collect_report_data (unikamy drugiego pobierania);
    gdy None, pobierany jest na miejscu.
    """
    lines = []
    lines.append("## Dashboard Portfela & Alerty")
    lines.append("")
    lines.append("### Tabela wskaznikow technicznych")
    lines.append("| Spolka | Cena | Zmiana 1D | Zmiana 5 sesji | RSI | vs SMA20 | vs SMA50 | MACD hist. | Bollinger | Alpha 1M (SMH/EPOL) | Wolumen | Trend | Sygnal |")
    lines.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")

    portfolio_tickers = active_tickers if active_tickers is not None else US_TICKERS + list(GPW_TICKERS_MAP.keys())
    stale_dates = {}
    for ticker in portfolio_tickers:
        data = portfolio_details.get(ticker, {})
        quote = data.get("quote") or {}
        technicals = data.get("technicals") or {}
        ticker_display = ticker
        if quote.get("is_stale"):
            ticker_display = f"{ticker}*"
            stale_dates[ticker] = quote.get("data_date", "brak daty")

        price = quote.get("price")
        price_str = "b/d"
        if price is not None:
            price_str = f"${price:,.2f}" if ticker in US_TICKERS else f"{price:,.2f} PLN"

        rsi = technicals.get("rsi_14")
        vs_sma20 = technicals.get("price_vs_sma20")
        vs_sma50 = technicals.get("price_vs_sma50")
        macd_hist = technicals.get("macd_histogram")
        bollinger_position = technicals.get("bollinger_position")
        vol_ratio = technicals.get("volume_ratio_10d")
        signal = generate_technical_signal(
            rsi,
            vs_sma20,
            vs_sma50,
            technicals.get("macd_trend"),
            technicals.get("bollinger_signal"),
        )

        macd_str = f"{macd_hist:+.3f}" if macd_hist is not None else "b/d"
        bollinger_str = f"{bollinger_position:.2f}" if bollinger_position is not None else "b/d"
        vol_str = f"{vol_ratio:.1f}x" if vol_ratio is not None else "b/d"
        lines.append(
            f"| **{ticker_display}** | {price_str} | "
            f"{format_pct(quote.get('change_pct'))} | "
            f"{format_pct(technicals.get('change_5d'))} | "
            f"{rsi if rsi is not None else 'b/d'} | "
            f"{format_pct(vs_sma20)} | "
            f"{format_pct(vs_sma50)} | "
            f"{macd_str} | "
            f"{bollinger_str} | "
            f"{format_pct(data.get('alpha_1m_vs_benchmark'))} | "
            f"{vol_str} | "
            f"{trend_label(technicals)} | "
            f"{signal} |"
        )

    lines.append("")
    if stale_dates:
        for ticker, data_date in stale_dates.items():
            lines.append(f"* Dane dla {ticker} pochodza z sesji: {data_date}")
        lines.append("")

    lines.append("### Alerty dnia")
    if earnings_dates is None:
        earnings_dates = fetch_earnings_dates(portfolio_tickers)
    all_alerts = []
    for ticker in portfolio_tickers:
        data = portfolio_details.get(ticker, {})
        data["earnings"] = earnings_dates.get(ticker, {})
        alerts = generate_alerts(ticker, data, last_snapshots.get(ticker))
        for alert in alerts:
            all_alerts.append(f"- **{ticker}**: {alert}")
    lines.extend(all_alerts or ["*Brak krytycznych alertow technicznych dla portfela na dzisiejsza sesje.*"])
    lines.append("")

    lines.append(f"### {deltas_label}")
    delta_lines = []
    for ticker in portfolio_tickers:
        technicals = (portfolio_details.get(ticker, {}) or {}).get("technicals") or {}
        deltas = calculate_deltas(
            ticker,
            {
                "rsi_14": technicals.get("rsi_14"),
                "price_vs_sma20": technicals.get("price_vs_sma20"),
                "price_vs_sma50": technicals.get("price_vs_sma50"),
                "macd_histogram": technicals.get("macd_histogram"),
                "bollinger_position": technicals.get("bollinger_position"),
            },
            last_snapshots.get(ticker),
        )
        if deltas:
            delta_lines.append(f"- **{ticker}**: {', '.join(deltas)}")
    lines.extend(delta_lines or ["*Brak istotnych zmian technicznych w tym okresie.*"])
    lines.append("")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def generate_rule_based_comment(ticker: str, data: dict) -> str:
    """Generate a compact non-AI analytical comment for a single ticker."""
    quote = data.get("quote") or {}
    fundamentals = data.get("fundamentals") or {}
    technicals = data.get("technicals") or {}
    alpha = data.get("alpha_1m_vs_benchmark")

    price = quote.get("price")
    change = quote.get("change_pct")
    if price is None:
        return "Brak dostepnych szczegolowych danych rynkowych dla tej spolki."

    currency = "$" if ticker in US_TICKERS else " PLN"
    comments = [f"Kurs wynosi {price:.2f}{currency} ({format_change(change) if change is not None else 'b/d'})."]

    if alpha is not None:
        rel = "silniejsza" if alpha > 0 else "slabsza"
        comments.append(f"Relatywnie spolka jest {rel} od benchmarku o {abs(alpha):.2f} pp. w miesiac.")

    rsi = technicals.get("rsi_14")
    if rsi is not None:
        comments.append(f"RSI wynosi {rsi}.")

    vs_sma20 = technicals.get("price_vs_sma20")
    vs_sma50 = technicals.get("price_vs_sma50")
    if vs_sma20 is not None and vs_sma50 is not None:
        comments.append(f"Cena jest {vs_sma20:+.2f}% od SMA20 i {vs_sma50:+.2f}% od SMA50.")

    macd_hist = technicals.get("macd_histogram")
    macd_trend = technicals.get("macd_trend")
    if macd_hist is not None:
        comments.append(f"MACD histogram: {macd_hist:+.3f} ({macd_trend}).")

    bollinger_position = technicals.get("bollinger_position")
    bollinger_signal = technicals.get("bollinger_signal")
    if bollinger_position is not None:
        comments.append(f"Pozycja w kanale Bollingera: {bollinger_position:.2f} ({bollinger_signal}).")

    pe = fundamentals.get("forward_pe")
    if pe is not None:
        comments.append(f"Forward P/E: {pe:.1f}.")

    short_pct = fundamentals.get("short_pct_float")
    if short_pct is not None:
        pct_val = short_pct if short_pct > 1.0 else short_pct * 100.0
        if pct_val > 10.0:
            comments.append(f"Short interest jest wysoki: {pct_val:.1f}%.")

    insider_summary = fundamentals.get("insider_summary") or {}
    insider_signal = fundamentals.get("insider_signal")
    if insider_summary:
        val_ccy = "PLN" if ticker.endswith(".WA") else "USD"
        buy_val = insider_summary.get("buy_value_90d") or 0
        sell_val = insider_summary.get("sell_value_90d") or 0
        comments.append(
            "Insiderzy 90d: "
            f"kupno za {buy_val / 1e6:.2f} mln {val_ccy}, "
            f"sprzedaz za {sell_val / 1e6:.2f} mln {val_ccy}; sygnal: {insider_signal}."
        )

    insiders = fundamentals.get("insider_transactions") or []
    if insiders:
        sample = []
        for ins in insiders[:2]:
            sample.append(f"{ins.get('insider')} {ins.get('transaction')} {ins.get('shares')} akcji ({ins.get('date')})")
        comments.append(f"Ostatnie transakcje insiderow: {'; '.join(sample)}.")

    return " ".join(comments)


