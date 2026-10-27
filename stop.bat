@echo off
title HabitFlow - остановка

echo   Останавливаю трекер и бота...
taskkill /f /im python.exe >nul 2>&1
echo   Готово (процессы Python остановлены или их не было).
echo.
pause
exit /b 0