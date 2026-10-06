# EuroTur — Automatización de pagos a proveedores (TourplanNX)

Pipeline en Python + Playwright que carga pagos a proveedores desde Excel
directamente en **TourplanNX** (login, búsqueda de proveedor, creación de
Invoice en Accounting → Transactions) y, opcionalmente, emite los **Cheques**
(órdenes de pago) correspondientes. Incluye un dashboard web (FastAPI + SSE)
para arrancar, parar y monitorear corridas desde el navegador.

## Instalación rápida

```powershell
python -m venv venv
venv\Scripts\activate
cd automatizacion
pip install -r requirements.txt
playwright install chromium
copy .env.example .env   # completar credenciales reales
```

## Uso

```powershell
cd automatizacion
python main.py --test --row 0 --no-tracker --visible   # probar 1 fila
python main.py --headless                                # corrida real completa

python -m checks.main run --no-tracker --visible          # emitir cheques

python -m uvicorn monitor.app:app --host 0.0.0.0 --port 8000 --reload  # dashboard
```

O usar los helpers `run_automation.bat` / `run_monitor.bat` (Windows) que
activan el entorno virtual y se ubican en `automatizacion/` automáticamente.

## Documentación completa

- **[ONBOARDING.md](ONBOARDING.md)** — guía de traspaso: qué hace el proyecto
  y por qué está armado así, requisitos, todas las variables de `.env`
  (cuáles son obligatorias/sensibles), todos los comandos disponibles, Docker,
  y problemas conocidos del lado de TourplanNX.
- **[CLAUDE.md](CLAUDE.md)** — mapa técnico de la arquitectura y los
  selectores de TourplanNX, pensado como referencia rápida para trabajar en
  el código (con o sin asistente de IA).
- **STATE.md / DECISIONS.md / TASKS.md** — estado del proyecto, decisiones de
  arquitectura y tareas pendientes.

## Requisitos

- Python 3.13
- PostgreSQL (o SQLite local para desarrollo) para el tracker de progreso
- Acceso a la instancia de TourplanNX con una cuenta con permisos de
  Creditors/Accounting

Ver [ONBOARDING.md](ONBOARDING.md) para el detalle de cada variable de
entorno y el resto de los requisitos.
