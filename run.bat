@echo off
REM ============================================================
REM  Traffic Intersection Vehicle Detection - launcher (Windows)
REM ============================================================
cd /d "%~dp0"

echo Installing / updating dependencies...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo Dependency installation failed. See messages above.
    pause
    exit /b 1
)

echo.
echo Starting server at http://127.0.0.1:8000
echo Press CTRL+C to stop.
echo.
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000

pause
