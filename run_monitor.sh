#!/bin/bash
cd "$(dirname "$0")"
echo "Activando entorno virtual..."
source venv/bin/activate
echo "Instalando dependencias si falta alguna..."
pip install -q fastapi uvicorn jinja2 playwright openpyxl pandas 2>/dev/null
echo "Iniciando monitor de automatización..."
echo "Abre http://localhost:8000 en tu navegador"
cd automatizacion
python -m uvicorn monitor.app:app --host 0.0.0.0 --port 8000 --reload
