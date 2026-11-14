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
- codzienny przegląd formularzy SEC i wykorzystanie wyników w sobotnich prognozach,
- odczyty OFR Financial Stress Index i pozycjonowania funduszy z CFTC COT w ocenie sentymentu i prognozach,
- archiwizacja raportów i wysyłka przez SMTP,
- uruchamianie lokalne, w schedulerze albo przez GitHub Actions.

## Architektura repozytoriów

Kod aplikacji znajduje się w publicznym repozytorium `market-report-engine`.
Prywatna konfiguracja i stan działania są przechowywane oddzielnie:

- `watchlists.json` — prywatna lista obserwowanych instrumentów i notatki,
- `daily_snapshots.csv` — historia dziennych danych,
- `prediction_tracker.csv` — historia i ocena prognoz.

GitHub Actions pobiera te pliki z prywatnego repozytorium
`Mantix46/market-report-state` i po udanym raporcie zapisuje tam zaktualizowane
pliki CSV. Dzięki temu publiczna historia Git nie zawiera prywatnej watchlisty
ani danych powstających podczas działania aplikacji.

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
GEMINI_MODEL=gemini-4-hydrogen

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

Samo pobranie filingów do prywatnej kolejki, również w dni bez raportu:

```powershell
python main.py --sec-poll-only
```

## Konfiguracja obserwowanych instrumentów

Listy spółek, zapytania newsowe i notatki znajdują się w prywatnym, ignorowanym przez Git pliku `watchlists.json`. Szablon publiczny to `watchlists.example.json`. Najważniejsze pola:

- `US_TICKERS` — główna lista spółek z USA,
- `GPW_TICKERS_MAP` — tickery GPW i nazwy wyświetlane w raporcie,
- `GPW_NEWS_QUERIES` — zapytania używane do wyszukiwania informacji,
- `TICKER_NOTES` — własne uwagi o instrumentach,
- `RADAR_SMALLCAPS` — lista spółek obserwowanych przez radar.

Zmiana list wpływa na zakres raportu, czas działania i liczbę zapytań do zewnętrznych źródeł.

## SEC, wyniki i sentyment

- `SEC_CONTACT_EMAIL` to adres kontaktowy wymagany do automatycznego dostępu SEC. Ustaw go w lokalnym `.env` oraz w Actions Secrets lub Variables. `SEC_TICKERS` jest opcjonalną listą oddzieloną przecinkami. Ustaw ją lokalnie lub w Actions Variables, np. `NBIS,MU,FPS,AAOI,AVGO`. Pusta wartość używa `US_TICKERS` z prywatnej watchlisty. [Zasady dostępu SEC](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)
- Sekcja **SEC EDGAR** pokazuje nowe formularze ze źródłowym linkiem, datą i opisem. Pierwsze uruchomienie obejmuje ostatnie siedem dni. Filingi złożone po porannym raporcie albo w dni bez wysyłki trafiają do następnego maila. Kolejka jest potwierdzana po akceptacji maila przez SMTP. Awaria zapisu stanu po udanej wysyłce może skutkować powtórzeniem pozycji.
- W sobotę Gemini dostaje wyniki spółek portfela z ostatnich siedmiu dni, także gdy zgłoszenie było już w dziennym mailu. SEC Company Facts i oficjalne załączniki EDGAR dostarczają danych o wynikach i guidance. Porównywalny konsensus pochodzi z FMP lub historycznych szacunków EPS w Yahoo Finance. W mailu pojawia się jakościowy wpływ wyników na kierunek kursu w sekcji prognoz. Przy niedostępnym Gemini raport stosuje zwykłą prognozę regułową. [API SEC](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) · [FMP Earnings](https://site.financialmodelingprep.com/developer/docs/stable/earnings-company) · [FMP Analyst Estimates](https://site.financialmodelingprep.com/developer/docs/stable/financial-estimates)
- Sentyment obejmuje dzienny indeks stresu OFR z opóźnieniem dwóch dni roboczych i udział pozycji netto funduszy lewarowanych w kontraktach S&P 500 w open interest. Gemini uwzględnia te sygnały w Executive Summary, sekcji sentymentu i sobotnich prognozach. Każdy odczyt ma datę, zmianę od poprzedniego i źródło. Niedostępność SEC lub jednego wskaźnika jest widoczna w logu i raporcie. [OFR](https://www.financialresearch.gov/financial-stress-index/) · [CFTC](https://publicreporting.cftc.gov/Commitments-of-Traders/TFF-Futures-Only/gpe5-46if)

## GitHub Actions

Workflow `.github/workflows/market_report.yml` można uruchomić ręcznie przez:

`Actions` → `Daily Market Report` → `Run workflow`

W repozytorium przejdź do:

`Settings` → `Secrets and variables` → `Actions` → `New repository secret`

Sekrety wymagane do pełnego działania:

- `STATE_REPO_TOKEN` — fine-grained PAT z dostępem `Contents: Read and write` wyłącznie do prywatnego repo `market-report-state`,
- `GEMINI_API_KEY`,
- `SMTP_SERVER`,
- `SMTP_PORT`,
- `SMTP_USER`,
- `SMTP_PASSWORD`.

Sekrety opcjonalne:

- `RECIPIENT_EMAIL` — domyślnie aplikacja używa wartości `SMTP_USER`,
- `GEMINI_MODEL` — domyślnie aplikacja używa modelu skonfigurowanego w kodzie,
- `FRED_API_KEY` — opcjonalnie,
- `FMP_API_KEY` — opcjonalnie,
- `SEC_CONTACT_EMAIL` — wymagany tylko dla zapytań SEC. Może też być zmienną Actions.

Sekrety GitHub Actions nie są zapisywane w kodzie. Nie wklejaj ich do workflow ani do `config.env.example`.

Workflow pobiera `watchlists.json`, `daily_snapshots.csv` i `prediction_tracker.csv` z prywatnego repozytorium `Mantix46/market-report-state`. Po udanym raporcie zapisuje zaktualizowane CSV z powrotem do tego repo. Publiczne repo zawiera wyłącznie kod i przykładową konfigurację.

Opcjonalne pliki prywatnego repo stanu `sec_edgar.json` i `earnings_consensus.json` powstają automatycznie. Workflow odtwarza je w lokalnym katalogu `state/`. Harmonogram GitHub Actions uruchamia wyłącznie odczyt SEC w niedziele i poniedziałki. Wysyłki nadal uruchamia istniejący `workflow_dispatch`.

Prywatne repo stanu musi zawierać w katalogu głównym:

```text
watchlists.json
daily_snapshots.csv
prediction_tracker.csv
```

Jeżeli token wygaśnie albo któregoś pliku zabraknie, workflow zatrzyma się przed wygenerowaniem i wysłaniem raportu.

Automatyczne uruchomienie może być zlecane przez cron-job.org przez endpoint
GitHub `workflow_dispatch`. Token używany przez cron powinien mieć dostęp tylko
do tego repozytorium i uprawnienie `Actions: Read and write`. Nie należy używać
do tego celu `STATE_REPO_TOKEN`, ponieważ służy on wyłącznie do dostępu do
prywatnego repozytorium stanu.

## Wprowadzanie zmian

Zmiany rozwijane są na branchu `feature-market-report-enhancements`:

```powershell
git switch feature-market-report-enhancements
git pull
```

Przed wysłaniem zmian uruchom testy oraz bezpieczny podgląd raportu:

```powershell
python -m pytest
python main.py --preview
```

Następnie wykonaj commit i push na branch roboczy, utwórz pull request do
`main` i zmerguj go dopiero po sprawdzeniu zmian. Codzienny raport korzysta z
`main`, więc sam push na branch roboczy nie zmienia wersji produkcyjnej.

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
sec_edgar.py            pobieranie filingów SEC i prywatny stan dostarczenia
earnings_report.py      dane o wynikach i konsensusie do sobotnich prognoz
guidance_consensus.py   prywatne migawki szacunków przed publikacją guidance
market_sentiment.py     OFR Financial Stress Index i CFTC COT
watchlists.example.json publiczny szablon obserwowanych instrumentów
```

## Testy

```powershell
python -m pytest
```

Testy korzystają z mocków i nie powinny wysyłać e-maili.

## Dane i bezpieczeństwo

Repozytorium kodu jest publiczne. Przed każdym commitem sprawdź:

- czy `.env`, logi i wygenerowane raporty nie są śledzone przez Git,
- czy `watchlists.json`, `daily_snapshots.csv` i `prediction_tracker.csv` znajdują się wyłącznie w prywatnym repo stanu,
- czy GitHub Actions używa wyłącznie wartości zapisanych jako repository secrets.

Samo dodanie pliku do `.gitignore` nie usuwa go z wcześniejszych commitów.
Jeżeli sekret trafi do historii, najpierw unieważnij klucz lub hasło, a dopiero
potem oczyść historię repozytorium.

## Ograniczenia

- dane z zewnętrznych serwisów mogą być opóźnione, niepełne lub niedostępne,
- raport AI może zawierać błędy i powinien być weryfikowany,
- aplikacja nie składa zleceń i nie uwzględnia indywidualnej sytuacji inwestora,
- wyniki historyczne i trafność prognoz nie gwarantują przyszłych rezultatów.

## Licencja

Projekt jest udostępniany na licencji MIT. Można go używać, kopiować,
modyfikować i rozpowszechniać, również komercyjnie, pod warunkiem zachowania
informacji o prawach autorskich i treści licencji. Szczegóły znajdują się w
pliku [LICENSE](LICENSE).
