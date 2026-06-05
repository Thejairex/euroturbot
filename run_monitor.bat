@echo off
cd /d "%~dp0"
echo Activando entorno virtual...
call venv\Scripts\activate.bat
echo Instalando dependencias si falta alguna...
pip install -q fastapi uvicorn jinja2 playwright openpyxl pandas 2>nul
echo Iniciando monitor de automatizacion...
echo Abre http://localhost:8000 en tu navegador
cd automatizacion
python -m uvicorn monitor.app:app --host 0.0.0.0 --port 8000 --reload
pause
