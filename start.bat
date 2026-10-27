@echo off
cd /d "%~dp0"
title HabitFlow - запуск

set "TOKEN="
if exist "bot_token.txt" set /p TOKEN=<"bot_token.txt"

set "PROXY="
if exist "proxy.txt" set /p PROXY=<"proxy.txt"

if not "%TOKEN%"=="" goto have_token

echo.
echo   Токен Telegram-бота не задан.
echo.
echo   1) Откройте в Telegram бота @BotFather
echo   2) Отправьте /newbot и придумайте имя
echo   3) Скопируйте токен вида 123456:ABC-DEF...
echo.
set /p TOKEN="Вставьте токен и нажмите Enter: "
if "%TOKEN%"=="" goto no_token
>"bot_token.txt" echo %TOKEN%
echo.
echo   Токен сохранён в bot_token.txt

:have_token
echo.
if not "%PROXY%"=="" echo   Использую прокси: %PROXY%
echo   Запускаю трекер...
start "HabitFlow - трекер" cmd /k "chcp 65001>nul & set PYTHONUTF8=1 & set HTTPS_PROXY=%PROXY% & set HTTP_PROXY=%PROXY% & set TELEGRAM_BOT_TOKEN=%TOKEN% & python app.py"
timeout /t 3 /nobreak >nul

echo   Запускаю бота...
start "HabitFlow - бот" cmd /k "chcp 65001>nul & set PYTHONUTF8=1 & set HTTPS_PROXY=%PROXY% & set HTTP_PROXY=%PROXY% & set TELEGRAM_BOT_TOKEN=%TOKEN% & python telegram_bot.py"
timeout /t 2 /nobreak >nul

echo   Открываю браузер...
start "" "http://127.0.0.1:5000"

echo.
echo   Готово! Трекер: http://127.0.0.1:5000
echo   В Telegram отправьте боту: /link ВАШ_API_КЛЮЧ
echo   (ключ - на странице /integrations)
echo.
echo   Если бот не отвечает - Telegram Bot API заблокирован
echo   вашим провайдером. Создайте файл proxy.txt с одной строкой:
echo       http://host:port   (или socks5://host:port)
echo   либо включите VPN, затем запустите start.bat снова.
echo.
echo   Остановка: stop.bat
echo.
pause
exit /b 0

:no_token
echo.
echo   Токен не введён. Запуск отменён.
echo.
pause
exit /b 1