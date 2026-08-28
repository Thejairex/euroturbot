# TASKS.md

> Generado a partir del diff sin commitear, `conclusion.md` y CLAUDE.md. IDs asignados por esta sesión de gobierno — no existían antes.

| ID | Descripción | Estado | Prioridad | Dependencias |
|----|-------------|--------|-----------|--------------|
| T001 | Commitear y validar en TourplanNX el cambio de proveedores DELETED → estado `disabled` (`pipeline.py`, `tracker.py`, `creditor_search.py`) | in_progress | high | — |
| T002 | Confirmar/ajustar el rango `PROCESS_SERVICE_DATE_FROM`/`_TO` en `config/settings.py` (actualmente 2007-01-01..2010-12-31) y decidir si vuelve a `None`/`None` tras el backfill puntual | in_progress | high | T001 |
| T003 | Commitear extensión de `save_summary_report` a `checks/main.py` (reporte `resumen_cheques.md`) | in_progress | medium | — |
| T004 | Actualizar CLAUDE.md: corregir la sección de Tracker (dice SQLite/`outputs/tracker.db`, el código ya usa PostgreSQL vía `migrate_to_pg.py`) | pending | high | — |
| T005 | Cuantificar en la DB cuántos de los ~332k vouchers `pending` restantes son de proveedores DELETED (detectables sin abrir la lupa) vs. vouchers puntuales inexistentes en AP (propuesta abierta en `conclusion.md`) | pending | medium | T001 |
| T006 | Confirmar si el caso puntual 1AIR01 (vouchers 1683852/1683853, pending sin decisión en la corrida anterior) quedó resuelto como `skipped`/`disabled` en la próxima pasada | pending | medium | T001 |
| T007 | Documentar/confirmar variables de entorno nuevas para Postgres (`DB_HOST`, `DB_PORT`, `DB_DATABASE`, `DB_USERNAME`, `DB_PASSWORD`) en `.env.example` | pending | medium | T004 |
| T008 | Evaluar agregar tests o al menos smoke tests para el pipeline (hoy no hay suite ni linter, verificación manual vía `--test --row N`) | pending | low | — |
| T009 | Revisar si el trade-off de selección masiva de vouchers (SELECT ALL + rangos densos, fail-closed) sigue vigente o si ya se resolvió por completo (ver memoria `project_bug_sobreseleccion_vouchers`) | pending | medium | — |

## Notas

- Prioridades asignadas por criterio propio dado el contexto (riesgo de producción, trabajo sin commitear); ajustar si el equipo tiene otro orden real.
- No se listan tareas que no tengan evidencia en el repo o en `conclusion.md` — evitar inventar roadmap no confirmado.
