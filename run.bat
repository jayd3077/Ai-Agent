@echo off
cd /d "%~dp0"
echo.
echo === Grid Control – Datacenter AI Agent ===
echo.

if not exist "env\Scripts\activate.bat" (
    echo Creating virtual environment...
    python -m venv env
)

call env\Scripts\activate.bat

echo Installing dependencies...
pip install -q -r requirements.txt

echo.
echo Checking setup...
python -c "import server" 2>&1
if errorlevel 1 (
    echo.
    echo ERROR: Setup check failed. See message above.
    pause
    exit /b 1
)

echo.
echo Starting server on http://localhost:8000
echo Press Ctrl+C to stop.
echo.
uvicorn server:app --port 8000
pause
