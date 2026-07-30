"""
main.py — Market Report Engine

Główny skrypt łączący generowanie raportu z wysyłką mailową.
Może działać jako:
  1. Jednorazowe uruchomienie: python main.py
  2. Scheduler: python main.py --schedule
  3. Tylko generowanie: python main.py --no-email
"""

import argparse
import os
import sys
import time
import io
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)

import schedule
from dotenv import load_dotenv

from report_fetcher import build_report
from report_builder import DataQualityError
from email_sender import send_report_email
from market_calendar import market_status, warsaw_now

import logging

# Konfiguracja logów całej aplikacji: poziom + timestamp (czytelne w GitHub Actions)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def load_config() -> dict:
    """Ładuje konfigurację z pliku .env."""
    
    env_path = Path(__file__).parent / ".env"
    
    if env_path.exists():
        load_dotenv(env_path)
    else:
        logger.info("Brak lokalnego pliku .env, próba odczytu ze zmiennych środowiskowych...")
    
    config = {
        "smtp_server": os.getenv("SMTP_SERVER") or "smtp.gmail.com",
        "smtp_port": int(os.getenv("SMTP_PORT") or "587"),
        "smtp_user": os.getenv("SMTP_USER") or "",
        "smtp_password": os.getenv("SMTP_PASSWORD") or "",
        "recipient": os.getenv("RECIPIENT_EMAIL") or os.getenv("SMTP_USER") or "",
        "report_time": os.getenv("REPORT_TIME") or "07:30",
    }
    
    # Walidacja
    missing = [k for k in ["smtp_user", "smtp_password", "recipient"] if not config[k]]
    if missing:
        logger.error(f"Brak wymaganych zmiennych w .env: {', '.join(missing)}")
        sys.exit(1)
    
    return config


def generate_and_send(config: dict, send_email: bool = True):
    """Generuje raport i opcjonalnie wysyła mailem."""
    
    now = warsaw_now()
    print(f"\n{'='*60}")
    print(f"  📊 Market Report Engine - {now.strftime('%d.%m.%Y %H:%M')}")
    print(f"{'='*60}\n")
    
    # Sprawdzenie dnia: Nie wysyłamy w Niedzielę (6) i Poniedziałek (0)
    if now.weekday() in [0, 6]:
        logger.info("Dzień wolny od raportów (Niedziela / Poniedziałek). Pomijam generowanie.")
        return

    # Jeśli OBA rynki są dziś zamknięte z powodu święta — pomijamy raport
    status = market_status()
    if not status["US"]["open"] and not status["PL"]["open"] and not status["US"]["weekend"]:
        us_h = status["US"]["holiday"] or "święto"
        pl_h = status["PL"]["holiday"] or "święto"
        logger.info(f"Oba rynki zamknięte dziś (USA: {us_h}, GPW: {pl_h}). Pomijam generowanie.")
        return

    # 1. Generowanie raportu
    print("[1/2] Pobieram dane rynkowe...")
    try:
        report = build_report()
    except DataQualityError as e:
        # Awaria źródła danych — zamiast raportu pełnego "b/d" wysyłamy krótkie ostrzeżenie
        logger.error(f"Wstrzymano raport — problem z jakością danych: {e}")
        if send_email:
            warning_md = (
                f"# ⚠ Raport wstrzymany — problem z danymi\n\n"
                f"{e}\n\n"
                f"Raport nie został wygenerowany, aby nie wysyłać niekompletnych danych. "
                f"Spróbuj uruchomić go ponownie później (Actions → Run workflow) "
                f"lub sprawdź status Yahoo Finance.\n"
            )
            send_report_email(
                smtp_server=config["smtp_server"],
                smtp_port=config["smtp_port"],
                smtp_user=config["smtp_user"],
                smtp_password=config["smtp_password"],
                recipient=config["recipient"],
                md_report=warning_md,
                subject=f"⚠ Raport Rynkowy — problem z danymi ({now.strftime('%d.%m.%Y')})",
            )
        return
    logger.info(f"Raport wygenerowany ({len(report)} znaków)")
    
    # Zapisz kopię archiwalną lokalnie
    try:
        archive_dir = Path(__file__).parent / "raporty" / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / f"raport_{now.strftime('%Y-%m-%d')}.md"
        with open(archive_path, "w", encoding="utf-8") as f:
            f.write(report)
        logger.info(f"Zapisano kopię archiwalną: {archive_path}")
    except Exception as e:
        logger.warning(f"Błąd zapisu kopii archiwalnej: {e}")
        
    # 2. Wysyłka mailowa
    if send_email:
        print("[2/2] Wysyłam e-mail...")
        success = send_report_email(
            smtp_server=config["smtp_server"],
            smtp_port=config["smtp_port"],
            smtp_user=config["smtp_user"],
            smtp_password=config["smtp_password"],
            recipient=config["recipient"],
            md_report=report,
        )
        if not success:
            logger.warning("Wysyłka maila nie powiodła się.")
    else:
        print("[2/2] Pominięto wysyłkę e-mail (--no-email)")
    
    print(f"\n{'='*60}\n")


def run_scheduler(config: dict):
    """Uruchamia scheduler — raport wysyłany codziennie o zadanej godzinie."""
    
    report_time = config["report_time"]
    logger.info(f"Raport będzie wysyłany codziennie o {report_time}")
    logger.info(f"Na adres: {config['recipient']}")
    logger.info(f"Naciśnij Ctrl+C aby zatrzymać.\n")
    
    schedule.every().day.at(report_time).do(generate_and_send, config)
    
    # Nieskończona pętla schedulera
    try:
        while True:
            schedule.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        print("\n[SCHEDULER] Zatrzymano.")


def main():
    parser = argparse.ArgumentParser(
        description="📊 Market Report Engine - codzienny raport rynkowy na e-mail"
    )
    parser.add_argument(
        "--schedule", 
        action="store_true",
        help="Uruchom scheduler (wysyłka codziennie o ustawionej godzinie)"
    )
    parser.add_argument(
        "--no-email",
        action="store_true", 
        help="Tylko generuj raport, nie wysyłaj e-maila"
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Wyświetl raport w konsoli bez wysyłki"
    )
    args = parser.parse_args()
    
    # Załaduj .env, aby klucz Gemini był dostępny również dla preview
    from dotenv import load_dotenv
    from pathlib import Path
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    
    if args.preview:
        try:
            # Podgląd nie zapisuje snapshotów ani prognoz — nie psuje danych CI
            report = build_report(persist_state=False)
        except DataQualityError as e:
            logger.error(f"Wstrzymano raport — problem z jakością danych: {e}")
            return
        print(report)
        return
    
    config = load_config()
    
    if args.schedule:
        run_scheduler(config)
    else:
        generate_and_send(config, send_email=not args.no_email)


if __name__ == "__main__":
    main()
