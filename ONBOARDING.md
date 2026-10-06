# EuroTur — Automatización de pagos a proveedores (TourplanNX)

Documentación de traspaso del proyecto. Si estás leyendo esto porque te acaban de
asignar este proyecto: andá directo a "Cómo correrlo" y "Variables de entorno
(.env)" — es lo mínimo para dejarlo andando. El resto es contexto para entender
por qué está armado así.

## Qué hace este proyecto

Es un pipeline de automatización (Playwright, no usa ninguna API oficial —
TourplanNX no tiene una) que:

1. Lee pagos a proveedores desde archivos Excel (`.xlsx`) que llegan a mano.
2. Hace login en **TourplanNX** (sistema de gestión de la agencia, una SPA Angular).
3. Por cada proveedor: lo busca, abre su ficha, navega a
   Accounting → Transactions, y crea un **Invoice** (factura) cargando los
   vouchers del Excel.
4. Opcionalmente, con los invoices ya cargados, emite **Cheques** (órdenes de
   pago) que aplican esos invoices.
5. Todo el progreso se guarda en una base de datos (el "tracker") para poder
   cortar y reanudar sin reprocesar ni duplicar nada.
6. Hay un dashboard web (FastAPI + Server-Sent Events) para arrancar, parar y
   ver el progreso en vivo desde el navegador, sin tocar la terminal.

Esto reemplaza carga manual fila por fila en TourplanNX, que para archivos de
decenas de miles de filas no es viable a mano.

## Por qué está hecho así (cosas no obvias)

- **TourplanNX es una SPA Angular vieja, sin API pública.** Todo el código en
  `modules/` son "clicks" de Playwright sobre selectores CSS frágiles. Si
  TourplanNX cambia su frontend, estos selectores se rompen. Están comentados en
  el código y en `CLAUDE.md` los más importantes (hamburger menu, dropdown de
  búsqueda, el campo CURRENCY que necesita un truco especial porque `.fill()` no
  sirve con el binding de Angular, etc.).
- **El servidor de TourplanNX tiene timeouts SQL genuinos e intermitentes**,
  sobre todo con proveedores muy grandes (decenas de miles de vouchers). El
  código ya maneja esto con reintentos, partición en "chunks" y un tope de
  tiempo por proveedor (`MAX_CHUNKS_PER_SUPPLIER_PER_RUN` en
  `config/settings.py`) — no es un bug si un proveedor gigante queda `pending`
  y se termina en otra corrida.
- **Nunca se tocan invoices/facturas que no creó este pipeline.** Los invoices
  que crea el pipeline llevan siempre una referencia con el patrón
  `INV{row_index}{Supplier_Code}` — es la forma de distinguir "lo que cargamos
  nosotros" de facturas históricas ya existentes en TourplanNX. La emisión de
  cheques filtra explícitamente por esta referencia para no barrer con
  historial viejo. **No cambiar este criterio sin pensarlo dos veces** — fue un
  bug real en una versión anterior (cheques se armaron con el total de la
  grilla completa, que incluía facturas viejas).
- **La cuenta de TourplanNX (`PPROVEEDORES`) tiene límite de sesiones
  concurrentes.** Si hay otra persona/proceso logueado con la misma cuenta, no
  podés conectar. Antes de correr algo en modo visible para probar, confirmá
  que no haya otro proceso corriendo (ver abajo).

## Requisitos para correrlo

- **Windows** (está pensado y probado así; también hay `Dockerfile` /
  `docker-compose.yml` por si se prefiere contenedor — ver más abajo).
- **Python 3.13** (`venv/` del repo ya está armado con esa versión).
- **PostgreSQL** accesible en red (o nada, si se usa SQLite local — ver sección
  de base de datos) donde vive la base `euroturbot` (el "tracker").
- Acceso a Internet/VPN hacia la URL de TourplanNX y credenciales válidas de
  una cuenta con permisos de Creditors/Accounting.
- Los archivos `.xlsx` de proveedores a procesar (los pasa quien gestiona los
  pagos; van en `automatizacion/input/`).

### Instalación desde cero

```powershell
cd D:\Trabajo\EuroTur
python -m venv venv
venv\Scripts\activate
cd automatizacion
pip install -r requirements.txt
playwright install chromium
copy .env.example .env
notepad .env   # completar credenciales reales, ver sección siguiente
```

## Variables de entorno (`.env`)

El archivo vive en `automatizacion/.env` (NO en la raíz del repo — se carga
explícitamente desde ahí en `config/settings.py`). Nunca se commitea (está en
`.gitignore`); el que sí está versionado es `automatizacion/.env.example` como
plantilla.

| Variable | Obligatoria | Qué es |
|---|---|---|
| `LOGIN_USERNAME` | Sí | Usuario de TourplanNX (la cuenta `PPROVEEDORES` u otra con los mismos permisos). |
| `LOGIN_PASSWORD` | Sí | Contraseña de esa cuenta. **Dato sensible** — no compartir por chat/email sin cifrar. |
| `LOGIN_URL` | Sí | URL base de la instancia de TourplanNX (sin el `#/login` final). |
| `DB_CONNECTION` | No (default `sqlite`) | `pgsql` para usar PostgreSQL, `sqlite` para una base local en `outputs/tracker.db`. En producción se usa `pgsql`. |
| `DB_HOST` | Si `DB_CONNECTION=pgsql` | Host del servidor PostgreSQL. |
| `DB_PORT` | Si `pgsql` | Puerto (default 5432). |
| `DB_DATABASE` | Si `pgsql` | Nombre de la base (`euroturbot`). |
| `DB_USERNAME` / `DB_PASSWORD` | Si `pgsql` | Credenciales de esa base. **Dato sensible.** |
| `MONITOR_API_KEY` | Solo si el dashboard se expone a terceros | Clave de lectura que exigen los endpoints `GET` del monitor cuando el pedido viene de otro origen (cross-origin). El dashboard local (same-origin) no la necesita. Generarla con `python -c "import secrets;print(secrets.token_urlsafe(32))"`. |
| `MONITOR_ADMIN_KEY` | No (vacía = deshabilitado) | Clave para poder hacer `start`/`stop`/`reset` del pipeline desde afuera (server-to-server). Si queda vacía, esas acciones solo funcionan same-origin (desde el propio dashboard). |
| `MONITOR_CORS_ORIGINS` | No | Lista de dominios (separados por coma) autorizados a consumir la API del monitor desde otro dominio. Vacío = no se permite ningún origen externo. |

**La base de datos (`DB_*`) es la fuente de verdad de qué proveedor/fila/cheque
ya se procesó.** Si se pierde o se apunta mal, el pipeline no sabe qué ya se
hizo y puede reprocesar o (peor) no tener forma de confirmar qué falta. Hacer
backup de esa base con la misma seriedad que de cualquier base productiva.

## Cómo correrlo

Siempre con el entorno virtual activado y parado en `automatizacion/` (los
imports del proyecto son absolutos, así que correrlo desde otro directorio
rompe).

```powershell
cd D:\Trabajo\EuroTur
venv\Scripts\activate
cd automatizacion
```

### Antes de cualquier prueba en vivo

Confirmar que no haya ya un proceso corriendo (por el límite de sesiones
concurrentes de la cuenta de TourplanNX):

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' } | Select-Object ProcessId, CommandLine
```

### Carga de invoices (pipeline principal)

```powershell
python main.py --test --row 0 --no-tracker --visible      # probar 1 fila puntual, navegador visible
python main.py --test --no-tracker                         # probar 1 archivo, 1 fila, headless
python main.py --file "archivo.xlsx" --sheet "HOJA" --no-tracker
python main.py --headless                                  # corrida real completa (usa el tracker)
python main.py --supplier 1ABC01 --no-tracker --visible    # un solo proveedor puntual
python main.py --tracker status                            # estado general del tracker
python main.py --tracker reset --all                       # resetear tracker entero (CUIDADO)
python main.py --tracker reset --file "archivo.xlsx"        # resetear solo un archivo
```

`--no-tracker` es para pruebas: ignora y no actualiza la base, así que se puede
repetir sin "gastar" el proveedor como ya procesado.

### Emisión de cheques

```powershell
python -m checks.main run --suppliers 1ABC01,1DEF02 --no-tracker --visible
python -m checks.main run --no-tracker --visible   # todos los proveedores con invoices ok
python -m checks.main filter --status ok --count   # consultar vouchers sin abrir navegador
```

### Dashboard (monitor)

```powershell
cd automatizacion
python -m uvicorn monitor.app:app --host 0.0.0.0 --port 8000 --reload
```

Abrir `http://localhost:8000`. Desde ahí se puede arrancar/parar la carga de
invoices o de cheques y ver el progreso en vivo (vía Server-Sent Events) sin
usar la terminal. Los endpoints de control (`/api/start`, `/api/stop`,
`/api/tracker/reset`) requieren estar en el mismo origen o mandar
`MONITOR_ADMIN_KEY`; los de solo lectura (`/api/stats`, `/api/tracker`,
`/api/stream`, etc.) piden `MONITOR_API_KEY` solo si el pedido es cross-origin.

### Helpers desde la raíz del repo

Activan el venv y hacen `cd automatizacion` por vos:

- `run_automation.bat [args]` (Windows) / `run_automation.sh` (Bash)
- `run_monitor.bat` / `run_monitor.sh`

### Con Docker (alternativa)

Hay `Dockerfile` (en `automatizacion/`) y `docker-compose.yml` (en la raíz) ya
armados: levantan el dashboard en el puerto `8500` (mapeado al `8000` interno),
leen el `.env` de `automatizacion/.env`, y montan `input/`, `processed/` y
`outputs/` como volúmenes para no perder datos al recrear el contenedor.

```powershell
docker compose up -d --build
```

## Flujo de archivos (para quien carga los Excel)

- Los `.xlsx` a procesar van en `automatizacion/input/`.
- Al terminar de procesar un archivo completo, se mueve solo a
  `automatizacion/processed/`.
- **No volver a poner el mismo archivo en `input/`** salvo que se haya
  reseteado el tracker para ese archivo a propósito — el hash del archivo lo
  detecta como "ya procesado" y lo saltea.
- Formato esperado: headers en la fila 2 o 3 (hay detección automática),
  datos debajo, columnas clave `Supplier_Code`, `Voucher_Number`,
  `Service_Cost_Currency`.

## Dónde mirar si algo se rompe

- `automatizacion/outputs/logs/` — logs de cada corrida.
- `automatizacion/outputs/screenshots/` — capturas automáticas en error.
- `automatizacion/outputs/reports/` — resúmenes de cada corrida.
- `CLAUDE.md` (raíz del repo) — mapa de arquitectura y selectores de TourplanNX,
  pensado originalmente para un asistente de IA pero sirve igual como
  referencia técnica rápida del código.
- `STATE.md` / `DECISIONS.md` / `TASKS.md` (raíz del repo) — estado del
  proyecto, decisiones de arquitectura tomadas y pendientes al momento del
  traspaso.

## Problemas conocidos (no son bugs nuestros)

- TourplanNX devuelve 500 intermitente en `GetSessionData` — el login falla
  solo; reintentar.
- La cuenta `PPROVEEDORES` no admite sesiones concurrentes.
- El campo VOUCHER NO. se autoformatea con comas (ej. `2,149,637`) — es
  comportamiento esperado del sistema, no un error de carga.
- Proveedores muy grandes (decenas de miles de vouchers) pueden sufrir
  timeouts SQL genuinos del servidor de TourplanNX en momentos puntuales; el
  pipeline ya tiene reintentos y partición en chunks para esto, pero no puede
  eliminar el timeout del lado servidor — algunos proveedores quedan
  `pending`/`failed` y se retoman en otra corrida.
