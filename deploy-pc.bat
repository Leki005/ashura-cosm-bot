@echo off
chcp 65001 >nul
cd /d "%~dp0"
where pwsh >nul 2>&1 && (pwsh -ExecutionPolicy Bypass -File "%~dp0deploy-pc.ps1") || (powershell -ExecutionPolicy Bypass -File "%~dp0deploy-pc.ps1")
pause