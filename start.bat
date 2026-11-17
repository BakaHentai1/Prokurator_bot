@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist .venv (
    echo Создаю виртуальное окружение...
    py -3 -m venv .venv || python -m venv .venv
)

call .venv\Scripts\activate.bat
echo Проверяю библиотеки...
python -m pip install -q -r requirements.txt

if not exist .env (
    copy .env.example .env >nul
    echo.
    echo Создан файл .env. Откройте его, заполните и запустите start.bat снова.
    pause
    exit /b
)

python bot.py
pause
