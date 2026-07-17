@echo off
REM =====================================================================
REM  Build RMFantasyPickBot.exe   (run this on Windows)
REM  Requires: Python 3.10+ installed and on PATH, plus Google Chrome.
REM =====================================================================

echo.
echo === Installing dependencies ===
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :error

echo.
echo === Building single-file executable ===
pyinstaller --noconfirm --clean RMFantasyPickBot.spec
if errorlevel 1 goto :error

echo.
echo === Done ===
echo Your app is at:  dist\RMFantasyPickBot.exe
echo (Double-click it to run. Chrome must be installed.)
goto :end

:error
echo.
echo BUILD FAILED - see the messages above.

:end
pause
