# STATE.md

> Generado el 2026-08-18 a partir de CLAUDE.md, historial de commits y el diff sin commitear en el working tree. Ver notas "Por confirmar" donde no hay evidencia directa en el repo.

## Objetivo principal

Automatizar la carga de pagos/facturas de proveedores en TourplanNX (facturación de vouchers) y la creación de cheques asociados, a partir de archivos Excel, con un dashboard web (FastAPI + SSE) para arrancar, monitorear y controlar las corridas.

## Estado general

**En desarrollo activo.** Hay 7 archivos modificados sin commitear (`automatizacion/checks/main.py`, `config/settings.py`, `core/pipeline.py`, `core/stats.py`, `data/tracker.py`, `main.py`, `modules/creditor_search.py`) y el historial reciente muestra commits casi diarios sobre selección de vouchers, cheques y tracker.

## Funcionalidades principales implementadas

- **Pipeline de facturación de vouchers** (`core/pipeline.py`, `modules/`): login en TourplanNX, búsqueda de proveedor, navegación a Accounting → Transactions, carga de Reference/Voucher/Currency, selección de vouchers (masiva y por fila), commit de transacción.
- **Pipeline de cheques** (`automatizacion/checks/`): `cheque_pipeline.py`, `cheque_creator.py`, `voucher_filter.py` — creación de cheques con manejo de `invoice_reference` y proveedores exentos (`proveedores_exentos.csv`).
- **Tracker de estado migrado a PostgreSQL** (`data/tracker.py` + `migrate_to_pg.py`): reemplaza el SQLite (`outputs/tracker.db`) descripto en CLAUDE.md. Guarda estado por archivo y por fila (`processed_files`, `processed_rows`), dedup por hash y backfill de referencias.
- **Dashboard de monitoreo** (`monitor/`, FastAPI + SSE): start/stop, stream de `StatsTracker`, estado del tracker, reset, endpoint de resumen persistente de vouchers/cheques, autenticación por API key + CORS para acceso de terceros.
- **Detección de proveedores DELETED**: si el registro de un proveedor está marcado DELETED en TourplanNX (solo lectura permanente, INSERT nunca se habilita), el pipeline lo marca como estado terminal `disabled` en vez de reintentar indefinidamente (en curso, ver abajo).
- **Reportes de ejecución en Markdown** (`save_summary_report` en `core/stats.py`): resumen legible de cada corrida (OK/failed/skipped, última actividad, estado acumulado del tracker, últimos eventos de log).
- **Dockerización**: `Dockerfile` + `docker-compose.yml` para correr el monitor como contenedor (`euroturbot-monitor`, puerto 8500→8000).

## Funcionalidades en progreso (working tree sin commitear)

- Estado terminal `disabled` para proveedores DELETED: nuevas columnas/métodos en el tracker (`mark_row_disabled`, `mark_rows_disabled_bulk`, `reset_disabled_to_pending`, `get_status_counts`) y detección en `pipeline.py` (`_insert_disabled`) + `creditor_search.py` (`open_supplier` ahora devuelve `bool` indicando DELETED). Nuevo subcomando CLI `--tracker reset-disabled`.
- Filtro de corrida por rango de `Service_Date` (`PROCESS_SERVICE_DATE_FROM`/`_TO` en `config/settings.py`, actualmente fijado a 2007-01-01..2010-12-31) — filas fuera de rango quedan `pending` sin tocar.
- `save_summary_report` extendido a `checks/main.py` (pipeline de cheques) con su propio manejo de excepciones/`KeyboardInterrupt` y reporte `resumen_cheques.md`.

## Funcionalidades pendientes importantes

- Por confirmar: si el `disabled` para proveedores DELETED ya fue validado en TourplanNX real o sigue en prueba (el diff está sin commitear).
- Por confirmar: alcance final del rango `PROCESS_SERVICE_DATE_FROM/TO` — los valores actuales (2007-2010) parecen un rango de prueba/backfill puntual, no un filtro permanente.
- Investigación abierta sobre vouchers "fantasma" en TourplanNX (ver `conclusion.md`): cuantificar cuántos de los ~332k pending restantes corresponden a proveedores DELETED (detectables sin abrir la lupa) vs. vouchers puntuales inexistentes en AP — mencionado como propuesta, sin confirmar si se ejecutó.
- No hay suite de tests ni linter configurado (confirmado en CLAUDE.md) — sigue siendo un pendiente de calidad, no bloqueante.

## Bloqueos conocidos

- **Servidor TourplanNX** devuelve 500 intermitente en `GetSessionData` (login falla con `TypeError: Cannot read properties of null`) — no es un bug propio.
- **Cuenta `PPROVEEDORES`** tiene límite de sesiones concurrentes: si alguien más está logueado, el pipeline no puede conectar.
- **VOUCHER NO.** se autoformatea con comas — comportamiento esperado del sistema, no un bug.
- **CURRENCY dropdown** solo funciona si el campo trae un valor predefinido para poder identificarlo.
- Producción sin entorno de prueba (ver memoria de gobernanza): TourplanNX es el único entorno; no hay registros de prueba, así que cualquier cambio de comportamiento del pipeline es irreversible en producción.

## Última decisión arquitectónica relevante

Migración del tracker de **SQLite → PostgreSQL** (`migrate_to_pg.py`, `data/tracker.py` reescrito con `psycopg2`/placeholders `%s`). Esto reemplaza la descripción de CLAUDE.md ("outputs/tracker.db" SQLite) — el CLAUDE.md del proyecto está desactualizado en ese punto. Ver [DECISIONS.md](DECISIONS.md).
