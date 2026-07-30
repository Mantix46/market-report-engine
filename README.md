# Market Report Engine

Market Report Engine to aplikacja w Pythonie, która pobiera dane rynkowe, buduje codzienny raport dla obserwowanych spółek z USA i GPW, opcjonalnie wzbogaca analizę przez Google Gemini i wysyła wynik e-mailem.

Projekt służy do automatyzacji researchu rynkowego. Nie jest systemem transakcyjnym ani źródłem rekomendacji inwestycyjnych.

## Najważniejsze funkcje

- notowania, zmiany cen i dane fundamentalne z Yahoo Finance,
- wskaźniki techniczne, m.in. RSI, średnie kroczące, MACD i Bollinger Bands,
- monitoring spółek z USA i GPW oraz radar small/mid caps,
- kalendarz makroekonomiczny i kontekst rynkowy,
- analiza treści przez Google Gemini z generatorem regułowym jako fallback,
- sobotnia analiza trendu, prognozy i pomiar ich historycznej trafności,
- archiwizacja raportów i wysyłka przez SMTP,
- uruchamianie lokalne, w schedulerze albo przez GitHub Actions.

## Wymagania

- Python 3.11,
- dostęp do internetu,
- konto pocztowe z obsługą SMTP, jeżeli raport ma być wysyłany,
- opcjonalnie klucz Google Gemini,
- opcjonalnie klucze FRED i Financial Modeling Prep.

## Instalacja

```powershell
git clone https://github.com/Mantix46/market-report-engine.git
cd market-report-engine
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item config.env.example .env
Copy-Item watchlists.example.json watchlists.json
```

W systemie Linux lub macOS aktywuj środowisko poleceniem `source .venv/bin/activate`, a pliki konfiguracyjne utwórz przez:

```bash
cp config.env.example .env
cp watchlists.example.json watchlists.json
```

Następnie uzupełnij `.env`:

```dotenv
SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=twoj_email@gmail.com
SMTP_PASSWORD=haslo_aplikacji
RECIPIENT_EMAIL=adres_odbiorcy@example.com

REPORT_TIME=07:30

GEMINI_API_KEY=twoj_klucz
GEMINI_MODEL=gemini-3.5-flash

FRED_API_KEY=
FMP_API_KEY=
```

Nie commituj pliku `.env`. Jest ignorowany przez Git.

## Uruchamianie

Najbezpieczniejszy podgląd, bez wysyłki e-maila i bez zapisu stanu:

```powershell
python main.py --preview
```

Generowanie raportu bez wysyłki, ale z zapisem lokalnego stanu:

```powershell
python main.py --no-email
```

Ten tryb nadal wymaga uzupełnionych zmiennych SMTP, ponieważ aplikacja waliduje konfigurację przed generowaniem.

Jednorazowe wygenerowanie i wysłanie raportu:

```powershell
python main.py
```

> Uwaga: powyższa komenda wysyła prawdziwy e-mail.

Scheduler lokalny:

```powershell
python main.py --schedule
```

Na Windows można też użyć `uruchom.bat --preview` albo `scheduler.bat`.

## Konfiguracja obserwowanych instrumentów

Listy spółek, zapytania newsowe i notatki znajdują się w prywatnym, ignorowanym przez Git pliku `watchlists.json`. Szablon publiczny to `watchlists.example.json`. Najważniejsze pola:

- `US_TICKERS` — główna lista spółek z USA,
- `GPW_TICKERS_MAP` — tickery GPW i nazwy wyświetlane w raporcie,
- `GPW_NEWS_QUERIES` — zapytania używane do wyszukiwania informacji,
- `TICKER_NOTES` — własne uwagi o instrumentach,
- `RADAR_SMALLCAPS` — lista spółek obserwowanych przez radar.

Zmiana list wpływa na zakres raportu, czas działania i liczbę zapytań do zewnętrznych źródeł.

## GitHub Actions

Workflow `.github/workflows/market_report.yml` można uruchomić ręcznie przez:

`Actions` → `Daily Market Report` → `Run workflow`

W repozytorium przejdź do:

`Settings` → `Secrets and variables` → `Actions` → `New repository secret`

Dodaj:

- `STATE_REPO_TOKEN` — fine-grained PAT z dostępem `Contents: Read and write` wyłącznie do prywatnego repo `market-report-state`,
- `GEMINI_API_KEY`,
- `GEMINI_MODEL`,
- `SMTP_SERVER`,
- `SMTP_PORT`,
- `SMTP_USER`,
- `SMTP_PASSWORD`,
- `RECIPIENT_EMAIL`,
- `FRED_API_KEY` — opcjonalnie,
- `FMP_API_KEY` — opcjonalnie.

Sekrety GitHub Actions nie są zapisywane w kodzie. Nie wklejaj ich do workflow ani do `config.env.example`.

Workflow pobiera `watchlists.json`, `daily_snapshots.csv` i `prediction_tracker.csv` z prywatnego repozytorium `Mantix46/market-report-state`. Po udanym raporcie zapisuje zaktualizowane CSV z powrotem do tego repo. Publiczne repo zawiera wyłącznie kod i przykładową konfigurację.

Prywatne repo stanu musi zawierać w katalogu głównym:

```text
watchlists.json
daily_snapshots.csv
prediction_tracker.csv
```

Jeżeli token wygaśnie albo któregoś pliku zabraknie, workflow zatrzyma się przed wygenerowaniem i wysłaniem raportu.

## Struktura projektu

```text
main.py                 uruchamianie, scheduler i wysyłka
report_fetcher.py       pobieranie i składanie danych raportu
report_builder.py       orkiestracja generatora raportu
layout_v2.py            aktywny układ i treść raportu
data_fetching.py        dane rynkowe i fundamentalne
news_fetcher.py         pobieranie wiadomości
technicals.py           wskaźniki techniczne
market_calendar.py      kalendarz sesji USA i GPW
email_sender.py         wysyłka SMTP
snapshot_store.py       historia dziennych snapshotów
accuracy_tracker.py     zapis i ocena prognoz
watchlists.example.json publiczny szablon obserwowanych instrumentów
```

## Testy

```powershell
pytest
```

Testy korzystają z mocków i nie powinny wysyłać e-maili.

## Dane i bezpieczeństwo

Przed ustawieniem repozytorium jako publiczne sprawdź:

- czy `.env`, logi i wygenerowane raporty nie są śledzone przez Git,
- czy historia Git nigdy nie zawierała prawdziwych kluczy lub haseł,
- czy `watchlists.json`, `daily_snapshots.csv` i `prediction_tracker.csv` znajdują się wyłącznie w prywatnym repo stanu,
- czy GitHub Actions używa wyłącznie wartości zapisanych jako repository secrets.

Samo dodanie pliku do `.gitignore` nie usuwa go z wcześniejszych commitów. Jeżeli sekret trafił do historii, najpierw unieważnij klucz lub hasło, a dopiero potem oczyść historię repozytorium.

## Ograniczenia

- dane z zewnętrznych serwisów mogą być opóźnione, niepełne lub niedostępne,
- raport AI może zawierać błędy i powinien być weryfikowany,
- aplikacja nie składa zleceń i nie uwzględnia indywidualnej sytuacji inwestora,
- wyniki historyczne i trafność prognoz nie gwarantują przyszłych rezultatów.

## Licencja

Repozytorium nie ma jeszcze pliku licencji. Publiczny kod bez licencji można przeglądać, ale inni nie otrzymują automatycznie prawa do jego kopiowania, modyfikowania i dystrybucji. Przed publikacją wybierz licencję świadomie, np. MIT dla prostego projektu open source.
