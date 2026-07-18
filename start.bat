@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Запуск бота косметолога Ашуры...
echo Убедитесь, что Happ/VPN включен!
echo.
"%~dp0venv\Scripts\python.exe" bot.py
pause