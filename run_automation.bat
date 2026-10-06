@echo off
cd /d "%~dp0"
echo Activando entorno virtual...
call venv\Scripts\activate.bat
cd automatizacion
python main.py --headless %*
if errorlevel 1 (
  echo Error durante la automatizacion. Revisa los logs.
  pause
)
