@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if exist "%PY%" goto run
echo [WARN] .venv python not found, fallback to python on PATH
set "PY=python"
:run

echo ============================================================
echo  Working dir : %CD%
echo  Interpreter : %PY%
echo ============================================================
"%PY%" -c "import sys,agentscope;print('python',sys.version.split()[0]);print('agentscope',agentscope.__version__)"
echo ------------------------------------------------------------
"%PY%" Hello.py
echo.
echo [exited] code=%errorlevel%
pause
