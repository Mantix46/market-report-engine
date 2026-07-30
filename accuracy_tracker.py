import csv
import os
import re
from datetime import datetime, timedelta
from typing import Optional

import yfinance as yf

from data_fetching import clean_history, US_TICKERS, GPW_TICKERS_MAP
from market_calendar import warsaw_now, warsaw_today

import logging

logger = logging.getLogger(__name__)


DIRECTION_UP = "up"
DIRECTION_DOWN = "down"
DIRECTION_NEUTRAL = "neutral"

# Pełny schemat pliku prognoz (kolumny rozliczenia dopisywane przy ewaluacji).
# Stare pliki bez tych kolumn wczytują się poprawnie (DictReader -> None).
PREDICTION_FIELDNAMES = [
    "date", "ticker", "direction", "horizon_days", "base_price", "thesis", "source",
    "atr_pct", "evaluated_date", "realized_pct", "hit",
]

# Ile dni trzymamy historię prognoz w pliku
PREDICTION_RETENTION_DAYS = 180


def get_predictions_file_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "prediction_tracker.csv")


def _parse_float(value) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _direction_from_technicals(technicals: dict) -> tuple[str, str]:
    """Kierunek z TRZECH niezależnych grup wskaźników (1 głos na grupę).

    Wskaźniki trendu (EMA-stacking, Supertrend, Ichimoku, regresja...) to w praktyce
    ten sam sygnał liczony różnymi metodami — zsumowane dawały pseudo-konsensus,
    w którym pojedynczy trend wystarczał do werdyktu. Teraz kierunek wymaga
    zgody co najmniej DWÓCH niezależnych grup (trend / momentum / mean-reversion).
    """
    # --- Grupa TREND (skorelowane miary tego samego zjawiska -> 1 głos) ---
    trend_score = 0
    trend_reasons = []

    ema_stack = technicals.get("ema_stack")
    if ema_stack == "strong_up":
        trend_score += 1
        trend_reasons.append("EMA stack up")
    elif ema_stack == "strong_down":
        trend_score -= 1
        trend_reasons.append("EMA stack down")

    adx = technicals.get("adx")
    plus_di = technicals.get("plus_di")
    minus_di = technicals.get("minus_di")
    if adx is not None and plus_di is not None and minus_di is not None and adx >= 25:
        if plus_di > minus_di:
            trend_score += 1
            trend_reasons.append(f"ADX {adx:.0f} +DI")
        elif minus_di > plus_di:
            trend_score -= 1
            trend_reasons.append(f"ADX {adx:.0f} -DI")

    supertrend = technicals.get("supertrend_dir")
    if supertrend == "long":
        trend_score += 1
        trend_reasons.append("Supertrend long")
    elif supertrend == "short":
        trend_score -= 1
        trend_reasons.append("Supertrend short")

    ichimoku = technicals.get("ichimoku_cloud")
    if ichimoku == "above":
        trend_score += 1
        trend_reasons.append("nad chmura Ichimoku")
    elif ichimoku == "below":
        trend_score -= 1
        trend_reasons.append("pod chmura Ichimoku")

    reg_slope = technicals.get("reg_slope_pct")
    if reg_slope is not None and reg_slope != 0:
        trend_score += 1 if reg_slope > 0 else -1
        trend_reasons.append(("dodatnie" if reg_slope > 0 else "ujemne") + " nachylenie regresji")

    vs_sma20 = technicals.get("price_vs_sma20")
    vs_sma50 = technicals.get("price_vs_sma50")
    if vs_sma20 is not None and vs_sma50 is not None:
        if vs_sma20 > 0 and vs_sma50 > 0:
            trend_score += 1
            trend_reasons.append("cena nad SMA20/50")
        elif vs_sma20 < 0 and vs_sma50 < 0:
            trend_score -= 1
            trend_reasons.append("cena pod SMA20/50")

    # Glos grupy TREND wymaga wewnetrznego konsensusu (|suma| >= 2 z 6 miar)
    trend_vote = 1 if trend_score >= 2 else (-1 if trend_score <= -2 else 0)

    # --- Grupa MOMENTUM ---
    momentum_score = 0
    momentum_reasons = []

    macd_trend = technicals.get("macd_trend")
    if macd_trend in ("bullish_cross", "improving", "positive"):
        momentum_score += 1
        momentum_reasons.append(f"MACD {macd_trend}")
    elif macd_trend in ("bearish_cross", "weakening", "negative"):
        momentum_score -= 1
        momentum_reasons.append(f"MACD {macd_trend}")

    donchian = technicals.get("donchian_signal")
    if donchian == "breakout_up":
        momentum_score += 1
        momentum_reasons.append("wybicie Donchiana w gore")
    elif donchian == "breakout_down":
        momentum_score -= 1
        momentum_reasons.append("wybicie Donchiana w dol")

    momentum_vote = 1 if momentum_score > 0 else (-1 if momentum_score < 0 else 0)

    # --- Grupa MEAN-REVERSION (kontrarianska) ---
    meanrev_score = 0
    meanrev_reasons = []

    rsi = technicals.get("rsi_14")
    if rsi is not None:
        if rsi < 30:
            meanrev_score += 1
            meanrev_reasons.append(f"RSI {rsi:.0f} wyprzedanie")
        elif rsi > 70:
            meanrev_score -= 1
            meanrev_reasons.append(f"RSI {rsi:.0f} wykupienie")

    bollinger_signal = technicals.get("bollinger_signal")
    if bollinger_signal in ("near_lower_band", "below_lower_band"):
        meanrev_score += 1
        meanrev_reasons.append("przy dolnej wstedze Bollingera")
    elif bollinger_signal in ("near_upper_band", "above_upper_band"):
        meanrev_score -= 1
        meanrev_reasons.append("przy gornej wstedze Bollingera")

    meanrev_vote = 1 if meanrev_score > 0 else (-1 if meanrev_score < 0 else 0)

    # --- Werdykt: minimum 2 z 3 niezaleznych grup musi wskazac ten sam kierunek ---
    def _vote_txt(vote, reasons):
        label = "up" if vote > 0 else ("down" if vote < 0 else "flat")
        return f"{label}" + (f" ({', '.join(reasons)})" if reasons else "")

    thesis = (
        f"TREND: {_vote_txt(trend_vote, trend_reasons)}; "
        f"MOMENTUM: {_vote_txt(momentum_vote, momentum_reasons)}; "
        f"MEANREV: {_vote_txt(meanrev_vote, meanrev_reasons)}"
    )

    total = trend_vote + momentum_vote + meanrev_vote
    if total >= 2:
        return DIRECTION_UP, thesis
    if total <= -2:
        return DIRECTION_DOWN, thesis
    return DIRECTION_NEUTRAL, thesis


def build_rule_based_predictions(portfolio_details: dict, horizon_days: int = 5) -> list[dict]:
    predictions = []
    today = warsaw_now().strftime("%Y-%m-%d")
    for ticker, data in portfolio_details.items():
        quote = data.get("quote") or {}
        technicals = data.get("technicals") or {}
        direction, thesis = _direction_from_technicals(technicals)
        predictions.append({
            "date": today,
            "ticker": ticker,
            "direction": direction,
            "horizon_days": horizon_days,
            "base_price": quote.get("price"),
            "thesis": thesis,
            "source": "rule_based_pre_gemini",
            # ATR% z dnia prognozy — do normalizacji progu trafnosci przy rozliczeniu
            "atr_pct": technicals.get("atr_pct"),
        })
    return predictions


def extract_predictions_from_report(report: str, portfolio_tickers: list[str], current_prices: dict, horizon_days: int = 5) -> list[dict]:
    """Extract simple forecast bullets if Gemini includes a 'Prognozy do weryfikacji' section."""
    today = warsaw_now().strftime("%Y-%m-%d")
    predictions = []
    ticker_re = "|".join(re.escape(t) for t in sorted(portfolio_tickers, key=len, reverse=True))
    if not ticker_re:
        return predictions

    line_re = re.compile(
        rf"\b(?P<ticker>{ticker_re})\b.*?(kierunek|direction)\s*[:=]\s*(?P<direction>up|down|neutral|wzrost|spadek|neutralny)",
        re.IGNORECASE,
    )
    for line in report.splitlines():
        match = line_re.search(line)
        if not match:
            continue
        raw_direction = match.group("direction").lower()
        direction = {
            "wzrost": DIRECTION_UP,
            "spadek": DIRECTION_DOWN,
            "neutralny": DIRECTION_NEUTRAL,
        }.get(raw_direction, raw_direction)
        ticker = match.group("ticker")
        predictions.append({
            "date": today,
            "ticker": ticker,
            "direction": direction,
            "horizon_days": horizon_days,
            "base_price": current_prices.get(ticker),
            "thesis": line.strip("- ").strip(),
            "source": "gemini_report",
        })

    return predictions


def save_predictions(predictions: list[dict]) -> None:
    if not predictions:
        return

    path = get_predictions_file_path()
    fieldnames = PREDICTION_FIELDNAMES
    existing_rows = []
    today = warsaw_now().strftime("%Y-%m-%d")
    tickers = {p.get("ticker") for p in predictions}

    if os.path.exists(path):
        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("date") == today and row.get("ticker") in tickers:
                    continue
                existing_rows.append(row)

    existing_rows.extend(predictions)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in existing_rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def _horizon_close(hist, pred_date, horizon_days: int) -> Optional[float]:
    """Cena zamkniecia N-tej SESJI po dacie prognozy (sesje handlowe, nie dni kalendarzowe).
    Zwraca None, gdy tyle sesji jeszcze nie uplynelo."""
    if hist is None or hist.empty:
        return None
    after = [
        (idx.date(), close)
        for idx, close in zip(hist.index, hist["Close"])
        if idx.date() > pred_date
    ]
    if len(after) < horizon_days:
        return None
    value = after[horizon_days - 1][1]
    return float(value) if value == value else None  # NaN check


def _is_saturday_date(date_str: str) -> bool:
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date().weekday() == 5
    except Exception:
        return False


def evaluate_previous_predictions(dry_run: bool = False) -> dict:
    """Rozlicza dojrzale, NIEROZLICZONE prognozy po cenie z KONCA HORYZONTU
    (a nie po cenie biezacej). Rozliczone wiersze dostaja evaluated_date/realized_pct/hit,
    dzieki czemu kazda prognoza liczy sie do statystyk dokladnie raz.

    Prognozy sa TYGODNIOWE (sobotnie) — wiersze z datami nie-sobotnimi (era prognoz
    dziennych) oraz tickerami spoza aktualnego portfela sa usuwane przy zapisie.

    dry_run=True (np. --preview): liczy wyniki, ale NIE modyfikuje pliku CSV.

    Zwraca: {"new": [rozliczone w tym biegu], "total_hits": int,
             "total_evaluated": int, "pending": int}
    """
    path = get_predictions_file_path()
    empty = {"new": [], "total_hits": 0, "total_evaluated": 0, "pending": 0}
    if not os.path.exists(path):
        return empty

    today = warsaw_today()
    cutoff_str = (today - timedelta(days=PREDICTION_RETENTION_DAYS)).strftime("%Y-%m-%d")
    portfolio = set(US_TICKERS) | set(GPW_TICKERS_MAP.keys())

    rows = []
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Retencja — wiersze starsze niz PREDICTION_RETENTION_DAYS wypadaja z pliku
            if row.get("date") and row["date"] < cutoff_str:
                continue
            # Czystka: tylko prognozy sobotnie (cykl tygodniowy) i tylko aktualny portfel
            if row.get("date") and not _is_saturday_date(row["date"]):
                continue
            if row.get("ticker") and row["ticker"] not in portfolio:
                continue
            rows.append(row)

    hist_cache: dict = {}

    def _history(ticker: str):
        if ticker not in hist_cache:
            try:
                t = yf.Ticker(ticker)
                hist_cache[ticker] = clean_history(ticker, t.history(period="6mo"), t)
            except Exception as e:
                logger.warning(f"Brak historii do rozliczenia prognoz dla {ticker}: {e}")
                hist_cache[ticker] = None
        return hist_cache[ticker]

    new_evaluations = []
    pending = 0
    changed = False
    for row in rows:
        if row.get("evaluated_date"):
            continue  # juz rozliczona
        ticker = row.get("ticker")
        pred_date_raw = row.get("date")
        if not ticker or not pred_date_raw:
            continue
        try:
            pred_date = datetime.strptime(pred_date_raw, "%Y-%m-%d").date()
        except Exception:
            continue
        horizon_days = int(row.get("horizon_days") or 5)
        if (today - pred_date).days < horizon_days:
            pending += 1
            continue  # na pewno za wczesnie (dni kalendarzowe < sesje)

        base_price = _parse_float(row.get("base_price"))
        if not base_price:
            continue
        horizon_price = _horizon_close(_history(ticker), pred_date, horizon_days)
        if horizon_price is None:
            pending += 1
            continue  # horyzont sesyjny jeszcze nie uplynal (np. swieto — lub brak danych)

        realized_pct = ((horizon_price / base_price) - 1) * 100
        direction = row.get("direction") or DIRECTION_NEUTRAL

        # Prog trafnosci znormalizowany zmiennoscia spolki: 0.5 x ATR% z dnia prognozy.
        # NBIS (ATR ~6%) potrzebuje ruchu ~3%, XTB (ATR ~2%) ~1% — sprawiedliwe dla obu.
        # Stare wiersze bez atr_pct: dotychczasowe sztywne progi 1%/2%.
        atr_pct = _parse_float(row.get("atr_pct"))
        threshold = 0.5 * atr_pct if atr_pct and atr_pct > 0 else None
        if direction == DIRECTION_UP:
            hit = realized_pct > (threshold if threshold is not None else 1.0)
        elif direction == DIRECTION_DOWN:
            hit = realized_pct < -(threshold if threshold is not None else 1.0)
        else:
            hit = abs(realized_pct) <= (threshold if threshold is not None else 2.0)

        row["evaluated_date"] = today.strftime("%Y-%m-%d")
        row["realized_pct"] = f"{realized_pct:.2f}"
        row["hit"] = "1" if hit else "0"
        changed = True

        new_evaluations.append({
            "prediction_date": pred_date_raw,
            "ticker": ticker,
            "direction": direction,
            "base_price": round(base_price, 2),
            "horizon_price": round(horizon_price, 2),
            "realized_pct": round(realized_pct, 2),
            "threshold_pct": round(threshold, 2) if threshold is not None else None,
            "hit": hit,
            "thesis": row.get("thesis", ""),
            "source": row.get("source", ""),
        })

    # Zapis pliku (nowe kolumny + retencja + czystka) — pomijany w dry_run (--preview)
    if not dry_run:
        try:
            with open(path, "w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=PREDICTION_FIELDNAMES, extrasaction="ignore")
                writer.writeheader()
                for row in rows:
                    writer.writerow({key: row.get(key, "") for key in PREDICTION_FIELDNAMES})
        except Exception as e:
            logger.error(f"Blad zapisu prediction_tracker.csv: {e}")

    # Najnowsze prognozy najpierw (cykl tygodniowy: ostatnia sobota na gorze)
    new_evaluations.sort(key=lambda item: item.get("prediction_date", ""), reverse=True)

    evaluated_rows = [r for r in rows if r.get("evaluated_date")]
    total_hits = sum(1 for r in evaluated_rows if str(r.get("hit")) in ("1", "True", "true"))
    if changed and not dry_run:
        logger.info(f"Rozliczono {len(new_evaluations)} prognoz (lacznie: {total_hits}/{len(evaluated_rows)}).")
    return {
        "new": new_evaluations,
        "total_hits": total_hits,
        "total_evaluated": len(evaluated_rows),
        "pending": pending,
    }


def format_accuracy_section(result: dict) -> str:
    lines = ["### Trafnosc prognoz tygodniowych", ""]
    result = result or {}
    new_items = result.get("new") or []
    total_evaluated = result.get("total_evaluated", 0)
    total_hits = result.get("total_hits", 0)
    pending = result.get("pending", 0)

    if not total_evaluated and not new_items and not pending:
        lines.append("*Brak dojrzalych prognoz do rozliczenia w tym raporcie.*")
        lines.append("")
        return "\n".join(lines)

    if new_items:
        # Grupowanie po dacie prognozy (najnowsza sobota najpierw)
        by_date: dict = {}
        for item in new_items:
            by_date.setdefault(item.get("prediction_date", "b/d"), []).append(item)
        for pred_date in sorted(by_date.keys(), reverse=True):
            items = by_date[pred_date]
            date_hits = sum(1 for i in items if i.get("hit"))
            lines.append(f"**Prognozy z {pred_date}** (rozliczone po 5 sesjach): {date_hits}/{len(items)} trafione")
            for item in items[:8]:
                verdict = "trafiona" if item.get("hit") else "nietrafiona"
                threshold = item.get("threshold_pct")
                threshold_txt = f" (prog ±{threshold:.1f}%)" if threshold is not None else ""
                lines.append(
                    f"- **{item['ticker']}**: {item['direction']} -> "
                    f"{item['realized_pct']:+.2f}% na koniec horyzontu{threshold_txt}; {verdict}."
                )
            lines.append("")
    else:
        lines.append("*Brak nowych prognoz rozliczonych w tym tygodniu.*")
        lines.append("")

    if pending:
        lines.append(f"*{pending} prognoz oczekuje jeszcze na zamkniecie horyzontu (np. po dniach swiatecznych).*")
        lines.append("")

    if total_evaluated:
        pct = total_hits / total_evaluated * 100
        lines.append(f"Skutecznosc lacznie (prognozy sobotnie): **{total_hits}/{total_evaluated}** ({pct:.0f}%).")
        lines.append("")
    return "\n".join(lines)
