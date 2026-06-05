#!/bin/bash
cd "$(dirname "$0")"
source venv/bin/activate
cd automatizacion
python main.py "$@"
