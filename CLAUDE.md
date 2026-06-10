# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Propósito

Pipeline que lee pagos a proveedores desde Excel, hace login en TourplanNX, busca cada proveedor, navega a Accounting → Transactions, crea un transaction llenando Reference/Voucher/Currency, y sale del proveedor para la siguiente fila. Incluye un dashboard FastAPI (SSE) para arrancar, parar y monitorear desde el navegador.

## Setup inicial

```powershell
python -m venv venv
venv\Scripts\activate
pip install fastapi uvicorn jinja2 playwright openpyxl pandas python-dotenv
playwright install chromium
copy automatizacion\.env.example automatizacion\.env   # editar con credenciales reales
```

`.env` requiere `LOGIN_USERNAME`, `LOGIN_PASSWORD`, `LOGIN_URL`. Se carga desde `automatizacion/.env` (no desde la raíz, ver `config/settings.py`).

No hay suite de tests ni linter configurados — no buscar `pytest`/`ruff`/`mypy`. La verificación se hace corriendo `--test --row N --no-tracker --visible` contra TourplanNX.

## Cómo ejecutar

CLI — **cwd debe ser `automatizacion/`** porque los imports son absolutos (`from core...`, `from modules...`):

```powershell
cd automatizacion
python main.py --test --row 0 --no-tracker --visible           # prueba 1 fila, navegador visible
python main.py --test --no-tracker                             # 1 archivo, 1 fila, headless
python main.py --file "archivo.xlsx" --sheet "HOJA" --no-tracker
python main.py --tracker status                                # estado del tracker
python main.py --tracker reset --all                           # resetear tracker entero
python main.py --tracker reset --file "archivo.xlsx"           # resetear un archivo
```

Helpers desde la raíz que activan `venv\` y hacen `cd automatizacion`:

- `run_automation.bat [args]` (Windows) / `run_automation.sh` (Bash)
- `run_monitor.bat` / `run_monitor.sh`

Monitor (FastAPI + SSE en `http://localhost:8000`):

```powershell
cd automatizacion
python -m uvicorn monitor.app:app --host 0.0.0.0 --port 8000 --reload
```

Endpoints clave: `POST /api/start`, `POST /api/start/pipeline` (sin re-login), `POST /api/stop`, `GET /api/stream` (SSE de `StatsTracker`), `GET /api/tracker`, `POST /api/tracker/reset?all=true|file=...`.

## Arquitectura

```
automatizacion/
├── main.py                      # CLI + threading entry points (run_automation_thread, stop_automation)
├── config/{settings,urls}.py    # ENV, paths, DEFAULT_TIMEOUT=30000; spa_url("creditor") → URL+"#/creditor"
├── core/
│   ├── pipeline.py              # run_pipeline(), process_row(), get_data_rows()
│   ├── browser.py               # BrowserManager (Playwright sync, chromium, es-ES, 1920x1080)
│   ├── stats.py                 # StatsTracker con RLock — estado compartido CLI ↔ monitor SSE
│   └── exceptions.py, cache_manager.py
├── modules/                     # "verbos del dominio" sobre Playwright
│   ├── login.py                 # do_login(), ensure_logged_in()
│   ├── creditor_search.py       # open_supplier(code)
│   ├── supplier_nav.py          # navigate_to_transactions() / exit_supplier()
│   └── transaction_creator.py   # create_transaction() — REFERENCE, VOUCHER NO., CURRENCY
├── data/
│   ├── tracker.py               # SQLite ProcessTracker (processed_files + processed_rows)
│   └── excel_reader.py, excel_writer.py
├── monitor/                     # FastAPI dashboard + SSE
├── utils/{logger,wait_helpers}.py
├── input/                       # xlsx pendientes (orden alfabético)
├── processed/                   # xlsx movidos tras completar (shutil.move)
└── outputs/                     # tracker.db, logs/, screenshots/, reports/, cache/
```

### Modelo de ejecución (threading)

`main.run_automation()` corre **sincrónico** desde el CLI (Playwright sync API). El monitor lo lanza en un **thread daemon** vía `run_automation_thread()` y comparte un `StatsTracker` global. Hay un único `_stop_event: threading.Event` — `stop_automation()` lo setea y el pipeline lo chequea **entre archivos y entre filas** (cooperativo: no interrumpe la fila en curso).

Al finalizar, `_finish()` cierra el navegador en otro thread con `timeout=15s` y luego llama `os._exit()` para garantizar salida limpia (Playwright sync deja threads pegados a veces).

### Flujo por fila

1. `open_supplier(page, supplier_code)` — escribe en `#searchSupplier input[type='text']`, espera `.dropdown table tr`, clickea la fila con el código, espera botón "Save".
2. `navigate_to_transactions(page)` — click hamburguesa vía `page.evaluate`, ACCOUNTING + TRANSACTIONS con `force=True`.
3. Click `INSERT`.
4. `create_transaction(page, row, row_index)`:
   - Espera que desaparezca `.spinner`.
   - **REFERENCE** (`input.tpdescription-transactionreference`) → `{row_index}_{Supplier_Code}_{Voucher_Number}`.
   - **VOUCHER NO.** (`input.tpnumber-vouchernumber`) → del Excel.
   - **CURRENCY**: `page.evaluate()` encuentra el input por su valor predefinido ("ARS"/"Pesos"), setea con `Object.getOwnPropertyDescriptor(...).set` (no `.fill()` porque rompe el binding Angular), espera 2s, `ArrowDown`, clickea fila del dropdown con el código.
5. EXIT del modal → `exit_supplier()` → vuelta al creditor search.

### Datos del Excel

| Columna | Uso |
|---------|-----|
| `Supplier_Code` | Buscar proveedor |
| `Voucher_Number` | VOUCHER NO. en formulario |
| `Service_Cost_Currency` | CURRENCY en formulario |

Headers en fila 2 (0-indexed), datos desde fila 3. Columna A (NaN) se descarta. Hojas llamadas `Sheet2` se ignoran (`get_sheet_names`).

### Tracker (SQLite)

`outputs/tracker.db` con tablas `processed_files` (status por archivo + hash sha256) y `processed_rows` (status por fila + índice por status). Se usa para dedup entre corridas y reanudar archivos parcialmente procesados. En desarrollo usar `--no-tracker`. El monitor lo expone en `/api/tracker`.

### Flujo de archivos

Los `.xlsx` se ponen en `automatizacion/input/`. Al terminar de procesar un archivo (con o sin errores en filas) se mueve a `automatizacion/processed/` con `shutil.move`. No reponer el mismo archivo en `input/` salvo reseteo previo del tracker (el hash sha256 lo detectaría como ya procesado).

## Decisiones técnicas clave

### Selectores TourplanNX (Angular, frágiles)

- **Hamburger**: `page.evaluate("document.querySelector('.hamburger').click()")` — no hay role accesible.
- **ACCOUNTING / TRANSACTIONS**: `click(force=True)` — `div.click-area` intercepta eventos.
- **CURRENCY**: identificar por valor predefinido; setear con descriptor nativo, no `.fill()`.
- **Modal dialog**: `get_by_role("dialog")` funciona vía a11y tree aunque el DOM no tenga `role="dialog"`.
- **Dropdown search**: `.dropdown table tr` — combo Angular con `<table>` interno.

### Autenticación

- Login: fill `input.username`, `input.password`, click `button.tpbutton.login`, espera hash `#/login`.
- Variables: `LOGIN_USERNAME`, `LOGIN_PASSWORD`, `LOGIN_URL` en `automatizacion/.env`.

## Issues conocidos

- **Servidor TourplanNX** devuelve 500 intermitente en `GetSessionData` — login falla con `TypeError: Cannot read properties of null`. No es problema nuestro.
- **Cuenta `PPROVEEDORES`** tiene límite de sesiones concurrentes — si alguien más está logueado, no podemos conectar.
- **VOUCHER NO.** se autoformatea con comas (2,149,637) — comportamiento esperado del sistema.
- **CURRENCY dropdown** solo funciona si el campo tiene valor predefinido ("ARS - Pesos Argentinos" o similar) para identificarlo.
