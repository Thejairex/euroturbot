# DECISIONS.md

> Decisiones inferidas de CLAUDE.md, mensajes de commit y el código/diff actual. Donde no hay evidencia textual del "por qué", se marca "Por confirmar".

---

### Migración del tracker de SQLite a PostgreSQL

- **Contexto**: el tracker original (`ProcessTracker`, SQLite en `outputs/tracker.db`) documentado en CLAUDE.md dedupica archivos/filas procesadas. El código actual de `data/tracker.py` ya usa `psycopg2` con placeholders `%s` y existe `migrate_to_pg.py` con modo `--import-sqlite` para migrar los datos existentes.
- **Decisión tomada**: reemplazar SQLite por PostgreSQL como store del tracker, manteniendo el mismo esquema lógico (`processed_files`, `processed_rows`).
- **Consecuencias / trade-offs**: Por confirmar el motivo exacto (concurrencia, acceso remoto desde el dashboard, volumen de filas). Como efecto colateral, requiere una instancia Postgres accesible vía `DB_HOST/DB_PORT/DB_DATABASE/DB_USERNAME/DB_PASSWORD` (variables no documentadas en el CLAUDE.md actual) y el `.env` debe actualizarse. CLAUDE.md quedó desactualizado en este punto y debería corregirse.

---

### Threading cooperativo con `_stop_event` + `os._exit()` al finalizar

- **Contexto**: Playwright sync API es bloqueante; el dashboard necesita poder arrancar/parar el pipeline sin congelar el servidor FastAPI.
- **Decisión tomada**: correr `run_pipeline`/`run_automation` en un thread daemon desde el monitor, compartiendo un único `StatsTracker` global y un `threading.Event` (`_stop_event`) que se chequea entre archivos y entre filas (no interrumpe la fila en curso). Al finalizar, se cierra el navegador con timeout de 15s en un thread separado y luego se fuerza `os._exit()`.
- **Consecuencias / trade-offs**: la parada es "cooperativa", no inmediata (una fila en curso siempre termina). `os._exit()` evita que Playwright deje threads colgados, pero salta el shutdown normal de Python (no se ejecutan otros `atexit`/finally fuera de lo ya envuelto explícitamente en `_cleanup`/`_finish`).

---

### Proveedores DELETED como estado terminal (`disabled`), no reintentable

- **Contexto**: la investigación documentada en `conclusion.md` encontró proveedores marcados "DELETED" en TourplanNX cuyo registro completo (incluido Transactions/INSERT) queda de solo lectura para siempre — no es un error transitorio.
- **Decisión tomada** (en curso, sin commitear): detectar el texto "deleted" en la fila del dropdown de búsqueda (`open_supplier` ahora devuelve `bool`) y, si además se confirma que el botón INSERT está deshabilitado (`_insert_disabled`), marcar la fila/proveedor como `disabled` en el tracker en vez de `failed`. A diferencia de `failed`/`skipped`, `disabled` **nunca** se resetea automáticamente al arrancar — solo manualmente vía `--tracker reset-disabled`.
- **Consecuencias / trade-offs**: evita reintentar indefinidamente proveedores que ya no existen en TourplanNX (ahorro de tiempo de corrida). Riesgo: si la heurística de detección de "DELETED" tiene falsos positivos, esas filas quedan atascadas hasta un reset manual explícito.

---

### Filtro opcional de corrida por rango de `Service_Date`

- **Contexto**: necesidad de poder acotar qué filas pending se levantan en una corrida puntual (p. ej. para un backfill de un rango de fechas viejo), sin tocar el resto del tracker.
- **Decisión tomada**: `PROCESS_SERVICE_DATE_FROM`/`PROCESS_SERVICE_DATE_TO` en `config/settings.py`, comparación lexicográfica sobre el string ISO `YYYY-MM-DD` de la columna `Service_Date` del Excel. Las filas fuera de rango quedan `pending` intactas (no se marcan de ninguna forma), para poder recogerlas solas si el rango se amplía o se quita.
- **Consecuencias / trade-offs**: es un filtro global de proceso (constante en `settings.py`), no un parámetro de CLI/dashboard — cambiarlo requiere editar código y redeployar. Por confirmar si esto es temporal (valores actuales 2007-01-01/2010-12-31 parecen un backfill puntual) o pensado como mecanismo permanente.

---

### Reportes de ejecución en Markdown (`save_summary_report`)

- **Contexto**: el log crudo es difícil de revisar para saber "qué pasó" en una corrida, especialmente si se cortó a mitad (Ctrl+C, excepción, force-stop del monitor, o un kill duro del proceso).
- **Decisión tomada**: generar un reporte Markdown (`outputs/reports/resumen_ejecucion.md` / `resumen_cheques.md`) con estado final, duración, progreso, última actividad conocida, proveedores fallidos y estado acumulado del tracker — llamado desde todos los caminos de salida que pasan por código Python (`_cleanup`/`_finish` en `main.py`, `finally` en `checks/main.py`).
- **Consecuencias / trade-offs**: un kill duro del proceso (taskkill /F, corte de luz) no genera reporte de esa corrida — limitación reconocida explícitamente en el propio código, no cubierta ni cubrible.

---

### Selección de vouchers: SELECT ALL + rangos densos con Service Date To (fail-closed)

- **Contexto**: bug de sobre-selección de vouchers ajenos a un proveedor en producción (ver memoria `project_bug_sobreseleccion_vouchers`), con 1006 facturas afectadas.
- **Decisión tomada**: priorizar integridad de datos (fail-closed: si hay ambigüedad, no se guarda) sobre velocidad de carga masiva; camino de solución identificado: SELECT ALL con Service Date To + rangos densos para hacer la selección determinística.
- **Consecuencias / trade-offs**: la carga masiva por selección amplia es "no confiable" según la memoria del proyecto — mitigado con lectura de referencias INV* existentes antes de cada corrida (ver `pipeline.py`, comentario sobre error 1038 "Reference Exists"). Por confirmar el estado final de este trade-off (si ya se resolvió por completo o sigue mitigado parcialmente).

---

### Dockerización del monitor (no del pipeline CLI)

- **Contexto**: `docker-compose.yml` define solo el servicio `euroturbot-monitor` (FastAPI + dashboard), montando `input/`, `processed/`, `outputs/` como volúmenes y exponiendo el puerto 8500→8000.
- **Decisión tomada**: correr el dashboard containerizado con `shm_size: 256mb` (necesario para Chromium/Playwright dentro del contenedor) y red externa `proxy` (sugiere reverse proxy compartido con otros servicios).
- **Consecuencias / trade-offs**: Por confirmar si el CLI (`main.py` directo) se sigue usando fuera de Docker para pruebas puntuales (`--test --row N --visible`) mientras el servicio productivo corre en contenedor, según sugiere CLAUDE.md.
