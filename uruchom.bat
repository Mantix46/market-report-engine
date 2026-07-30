@echo off
chcp 65001 >nul
echo.
echo  ╔══════════════════════════════════════════╗
echo  ║  📊 Market Report Engine - Quick Start   ║
echo  ╚══════════════════════════════════════════╝
echo.

cd /d "%~dp0"

REM Sprawdź czy istnieje .env
if not exist ".env" (
    echo [!] Brak pliku .env — kopiuję z szablonu...
    copy config.env.example .env
    echo [!] Uzupełnij dane w pliku .env, a następnie uruchom ponownie.
    pause
    exit /b 1
)

REM Instaluj zależności jeśli potrzeba
pip install -r requirements.txt -q

REM Uruchom raport
python main.py %*

pause
