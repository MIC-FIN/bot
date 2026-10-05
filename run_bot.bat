@echo off
chcp 65001 >nul
REM ============================================================
REM  Запуск торгового бота (T-Bank Invest API) с автоустановкой
REM  зависимостей и проверкой Python. Двойной клик = запуск.
REM ============================================================

cd /d "%~dp0"

REM ── 1. Найти Python ────────────────────────────────────────
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo [ОШИБКА] Python не найден в PATH.
    echo Установите Python 3.10+ с https://www.python.org/downloads/
    echo И поставьте галочку "Add Python to PATH" при установке.
    pause
    exit /b 1
)

REM Проверка, что это реальный Python, а не заглушка Microsoft Store
%PY% --version >nul 2>nul
if errorlevel 1 (
    echo [ОШИБКА] Найден нерабочий Python ^(возможно, заглушка из Microsoft Store^).
    echo Установите Python с python.org и добавьте его в PATH.
    pause
    exit /b 1
)

echo [%date% %time%] Python: 
%PY% --version

REM ── 2. Установить зависимости (один раз, по requirements.txt) ─
if exist requirements.txt (
    %PY% -m pip install --disable-pip-version-check --quiet --upgrade -r requirements.txt
    if errorlevel 1 (
        echo [ОШИБКА] Не удалось установить зависимости.
        echo Попробуйте запустить файл от имени администратора
        echo или уберите галочку "Use admin" и попробуйте %PY% -m pip install --user -r requirements.txt
        pause
        exit /b 1
    )
    echo Зависимости обновлены/проверены.
)

REM ── 3. Выбрать скрипт для запуска ──────────────────────────
REM  По умолчанию запускается bot_v3.py.
REM  Аргументы:
REM    run_bot.bat backtest   -> запуск бэктеста (backtest_mp.py)
REM    run_bot.bat bot.py     -> запуск любого другого скрипта
set "SCRIPT=bot_v3.py"
if /i "%~1"=="backtest" set "SCRIPT=backtest_mp.py"
if not "%~1"=="" if /i not "%~1"=="backtest" set "SCRIPT=%~1"

if not exist "%SCRIPT%" (
    echo [ОШИБКА] Файл %SCRIPT% не найден в папке %~dp0
    pause
    exit /b 1
)

REM ── 4. Запуск с перезапуском при падении ───────────────────
:loop
echo.
echo ============================================================
echo  Запуск: %SCRIPT%   ^(Ctrl+C - остановить, закройте окно - выйти^)
echo ============================================================
%PY% "%SCRIPT%"
set "EC=%errorlevel%"

echo.
echo [%date% %time%] Скрипт завершился с кодом %EC%.
if %EC%==0 goto end

REM Краш (не нулевой код) — ждём 30 сек и перезапускаем
choice /C RN /T 30 /D R /M "Перезапустить бота через 30 сек? (R-да, N-нет)"
if errorlevel 2 goto end
goto loop

:end
pause
