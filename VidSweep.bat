@echo off
rem VidSweep launcher — finds a Python that actually has the required libraries.
cd /d "%~dp0"

for %%P in ("%LOCALAPPDATA%\Python\bin" "C:\Python311" "C:\Python312" "C:\Python313" "C:\Python314") do (
    if exist "%%~P\python.exe" (
        "%%~P\python.exe" -c "import PIL, imagehash, tkinter, send2trash" >nul 2>&1
        if not errorlevel 1 (
            start "" "%%~P\pythonw.exe" "%~dp0gui.py"
            exit /b 0
        )
        set "FIRSTPY=%%~P\python.exe"
    )
)

echo One-time setup: installing required libraries...
"%FIRSTPY%" -m pip install --quiet pillow imagehash send2trash
if errorlevel 1 (
    echo.
    echo Setup failed. Check your internet connection, or run manually:
    echo     "%FIRSTPY%" -m pip install pillow imagehash send2trash
    echo.
    pause
    exit /b 1
)
start "" "%FIRSTPY:python.exe=pythonw.exe%" "%~dp0gui.py"
