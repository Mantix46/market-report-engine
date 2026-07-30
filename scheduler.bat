@echo off
chcp 65001 >nul
echo [SCHEDULER] Uruchamiam Market Report Engine w trybie schedulera...
echo [SCHEDULER] Raport bedzie wysylany codziennie wg ustawien w .env
echo.

cd /d "%~dp0"

if not exist ".env" (
    echo [!] Brak pliku .env!
    pause
    exit /b 1
)

pip install -r requirements.txt -q
python main.py --schedule
