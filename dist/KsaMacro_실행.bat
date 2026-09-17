@echo off
setlocal
cd /d "%~dp0"

where pythonw >nul 2>nul
if not errorlevel 1 (
    if exist ..\ksa_macro_main.py (
        start "" /d "%~dp0.." pythonw.exe ksa_macro_main.py
        exit /b
    )
)

if exist bin\KsaMacro_core.dat (
    start "" /d "%~dp0bin" KsaMacro_core.dat
    exit /b
)
