import os
import csv
from datetime import datetime, timedelta
from typing import Optional

from market_calendar import warsaw_now

import logging

logger = logging.getLogger(__name__)

# Ile dni historii snapshotów zachowujemy w pliku (reszta jest przycinana przy zapisie)
SNAPSHOT_RETENTION_DAYS = 60

def get_snapshot_file_path() -> str:
    """Zwraca bezwzględną ścieżkę do pliku daily_snapshots.csv."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "daily_snapshots.csv")


def load_last_snapshots(before: Optional[str] = None) -> dict:
    """Wczytuje ostatnie zapisane snapshoty dla każdego tickera sprzed podanej daty.

    before — data graniczna "YYYY-MM-DD" (wyłącznie); domyślnie dziś, czyli
    porównanie dzień-do-dnia. Raport sobotni podaje datę sprzed tygodnia,
    by sekcja zmian obejmowała cały tydzień.
    """
    snapshot_file = get_snapshot_file_path()
    if not os.path.exists(snapshot_file):
        return {}

    snapshots = {}
    cutoff_str = before or warsaw_now().strftime("%Y-%m-%d")

    try:
        with open(snapshot_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                ticker = row.get("ticker")
                date_val = row.get("date")
                if not ticker or not date_val:
                    continue
                if date_val < cutoff_str:
                    if ticker not in snapshots or date_val > snapshots[ticker]["date"]:
                        snapshots[ticker] = {
                            "date": date_val,
                            "price": float(row["price"]) if row.get("price") else None,
                            "change_pct": float(row["change_pct"]) if row.get("change_pct") else None,
                            "rsi_14": float(row["rsi_14"]) if row.get("rsi_14") else None,
                            "price_vs_sma20": float(row["price_vs_sma20"]) if row.get("price_vs_sma20") else None,
                            "price_vs_sma50": float(row["price_vs_sma50"]) if row.get("price_vs_sma50") else None,
                            "macd_histogram": float(row["macd_histogram"]) if row.get("macd_histogram") else None,
                            "bollinger_position": float(row["bollinger_position"]) if row.get("bollinger_position") else None,
                        }
    except Exception as e:
        logger.warning(f"Błąd odczytu snapshotów: {e}")
    
    return snapshots


def save_today_snapshots(current_metrics: dict):
    """Zapisuje lub aktualizuje dzisiejsze snapshoty w daily_snapshots.csv."""
    snapshot_file = get_snapshot_file_path()
    today_str = warsaw_now().strftime("%Y-%m-%d")
    
    fieldnames = ["date", "ticker", "price", "change_pct", "rsi_14",
                  "price_vs_sma20", "price_vs_sma50", "macd_histogram", "bollinger_position"]

    cutoff_str = (warsaw_now().date() - timedelta(days=SNAPSHOT_RETENTION_DAYS)).strftime("%Y-%m-%d")

    existing_rows = []
    if os.path.exists(snapshot_file):
        try:
            with open(snapshot_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # Pomijamy dzisiejsze wpisy dla aktualizowanych tickerów (zostaną nadpisane)
                    if row.get("date") == today_str and row.get("ticker") in current_metrics:
                        continue
                    # Przycinamy zbyt stare wpisy, by plik nie puchł w nieskończoność
                    if row.get("date") and row["date"] < cutoff_str:
                        continue
                    existing_rows.append(row)
        except Exception as e:
            logger.warning(f"Błąd odczytu przy aktualizacji snapshotów: {e}")
            
    for ticker, metrics in current_metrics.items():
        existing_rows.append({
            "date": today_str,
            "ticker": ticker,
            "price": metrics.get("price"),
            "change_pct": metrics.get("change_pct"),
            "rsi_14": metrics.get("rsi_14"),
            "price_vs_sma20": metrics.get("price_vs_sma20"),
            "price_vs_sma50": metrics.get("price_vs_sma50"),
            "macd_histogram": metrics.get("macd_histogram"),
            "bollinger_position": metrics.get("bollinger_position"),
        })

    try:
        with open(snapshot_file, "w", newline="", encoding="utf-8") as f:
            # extrasaction='ignore' — stare wiersze bez nowych kolumn są bezpieczne
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in existing_rows:
                writer.writerow(row)
    except Exception as e:
        logger.error(f"Błąd zapisu snapshotów do {snapshot_file}: {e}")


def calculate_deltas(ticker: str, current: dict, previous: Optional[dict]) -> list[str]:
    """Zwraca listę TYLKO istotnych zmian dzień-do-dnia (przekroczenia progów),
    bez szumu typu drobny ruch RSI. Wykrywa:
      - przekroczenia stref RSI 70 / 30,
      - przecięcia ceny przez SMA20 / SMA50,
      - zmianę znaku histogramu MACD,
      - wybicia wstęg Bollingera (pozycja >1 = górna, <0 = dolna).
    """
    if not previous:
        return []

    changes = []

    # 1. RSI — wejście/wyjście ze stref wykupienia (70) i wyprzedania (30)
    curr_rsi = current.get("rsi_14")
    prev_rsi = previous.get("rsi_14")
    if curr_rsi is not None and prev_rsi is not None:
        if prev_rsi < 70 <= curr_rsi:
            changes.append(f"RSI wybił ponad 70 — wykupienie ({prev_rsi:.0f}→{curr_rsi:.0f})")
        elif prev_rsi >= 70 > curr_rsi:
            changes.append(f"RSI schłodzony poniżej 70 ({prev_rsi:.0f}→{curr_rsi:.0f})")
        if prev_rsi > 30 >= curr_rsi:
            changes.append(f"RSI spadł poniżej 30 — wyprzedanie ({prev_rsi:.0f}→{curr_rsi:.0f})")
        elif prev_rsi <= 30 < curr_rsi:
            changes.append(f"RSI wrócił ponad 30 ({prev_rsi:.0f}→{curr_rsi:.0f})")

    # 2. Przecięcia ceny przez SMA20 / SMA50
    for label, key in (("SMA20", "price_vs_sma20"), ("SMA50", "price_vs_sma50")):
        cur = current.get(key)
        prev = previous.get(key)
        if cur is not None and prev is not None:
            if prev < 0 <= cur:
                changes.append(f"Wybicie ponad {label}")
            elif prev > 0 >= cur:
                changes.append(f"Spadek poniżej {label}")

    # 3. MACD — zmiana znaku histogramu (przecięcie linii sygnału)
    curr_macd = current.get("macd_histogram")
    prev_macd = previous.get("macd_histogram")
    if curr_macd is not None and prev_macd is not None:
        if prev_macd <= 0 < curr_macd:
            changes.append("MACD: pozytywne przecięcie")
        elif prev_macd >= 0 > curr_macd:
            changes.append("MACD: negatywne przecięcie")

    # 4. Bollinger — wybicie wstęg (pozycja >1 = nad górną, <0 = pod dolną)
    curr_bb = current.get("bollinger_position")
    prev_bb = previous.get("bollinger_position")
    if curr_bb is not None and prev_bb is not None:
        if prev_bb <= 1.0 < curr_bb:
            changes.append("Wybicie ponad górną wstęgę Bollingera")
        elif prev_bb >= 0.0 > curr_bb:
            changes.append("Spadek pod dolną wstęgę Bollingera")

    return changes
