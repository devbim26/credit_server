@echo off
setlocal

REM ============================================================
REM run-tunnel-credits.bat
REM Публикация сервера списания кредитов через ОТДЕЛЬНЫЙ Cloudflare-туннель.
REM Маршрут: credits.dev-bim.com -> http://localhost:4010
REM
REM ОТДЕЛЬНЫЙ туннель 'credits' (UUID db3069c5-b8e7-491d-a433-b78f96c3ae57)
REM НЕ зависит от туннеля docx-gen — изоляция запуска/остановки.
REM
REM ПРЕДВАРИТЕЛЬНО (выполнено один раз):
REM   1. cloudflared залогинен (cert.pem в ~/.cloudflared)
REM   2. создан туннель credits (UUID db3069c5-...)
REM   3. конфиг ~/.cloudflared/config-credits.yml
REM   4. DNS credits.dev-bim.com -> db3069c5-...cfargotunnel.com
REM ============================================================

REM --- поиск cloudflared ---
REM ВАЖНО: не используем "where" - он отдаёт System32\cloudflared.exe,
REM который заблокирован к запуску (0 байт, "Отказано в доступе").
REM Берём рабочий экземпляр из Program Files (x86).
set "CFEXE="
if exist "%ProgramFiles(x86)%\cloudflared\cloudflared.exe" set "CFEXE=%ProgramFiles(x86)%\cloudflared\cloudflared.exe"
if not defined CFEXE if exist "%ProgramFiles%\cloudflared\cloudflared.exe" set "CFEXE=%ProgramFiles%\cloudflared\cloudflared.exe"
if not defined CFEXE if exist "%USERPROFILE%\.cloudflared\cloudflared.exe" set "CFEXE=%USERPROFILE%\.cloudflared\cloudflared.exe"

if not defined CFEXE (
    echo [ERROR] cloudflared не найден.
    echo Установите cloudflared в Program Files или в папку .cloudflared.
    pause
    exit /b 1
)

set "CF_DIR=%USERPROFILE%\.cloudflared"
set "CFG=%CF_DIR%\config-credits.yml"

if not exist "%CFG%" (
    echo [ERROR] нет конфига: %CFG%
    pause
    exit /b 1
)

echo Запуск Cloudflare-туннеля credits ...
echo Конфиг: %CFG%
echo credits.dev-bim.com направляется на http://localhost:4010
echo.

"%CFEXE%" tunnel --config "%CFG%" run credits

echo.
echo Туннель остановлен.
pause
