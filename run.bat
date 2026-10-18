@echo off
setlocal
cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
    echo [ERROR] Virtual environment not found at venv\Scripts\activate.bat.
    echo Please create it with: python -m venv venv
    echo And install dependencies: venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

if not exist "yolov8n.pt" (
    echo [WARNING] yolov8n.pt not found in repository root.
    echo Offline demo requires pre-downloaded YOLO weights to avoid download delays.
)

echo Starting IBVAP Backend on http://127.0.0.1:8000 ...
start "IBVAP-Backend" cmd /k "call venv\Scripts\activate.bat && uvicorn backend.main:app --host 127.0.0.1 --port 8000"

echo Starting IBVAP Frontend on http://127.0.0.1:8080 ...
start "IBVAP-Frontend" cmd /k "call venv\Scripts\activate.bat && python -m http.server 8080 -d frontend --bind 127.0.0.1"

echo Waiting for services to initialize...
timeout /t 8 /nobreak >nul

echo Opening browser at http://localhost:8080/ ...
start http://localhost:8080/

echo IBVAP is running. Run stop.bat to terminate both services.
