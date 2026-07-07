# API de Monitoreo — Guía de integración para terceros

API de **solo lectura** para monitorear el estado del pipeline de automatización desde
páginas o dashboards externos, sin acceder al servidor directamente.

- **Base URL:** `https://TU-HOST` (dominio público) o `http://euroturbot-monitor:8000` (interno, ver abajo).
- **Formato:** JSON (REST) y `text/event-stream` (SSE) para tiempo real.
- **Autenticación:** API key en cada request (header o query param).

---

## Acceso interno contenedor → contenedor (escenario actual)

La app que consume corre en el mismo servidor, en **otro contenedor**, unida a la red
Docker externa `proxy`. En ese caso el backend llama a la API **por el nombre del
contenedor**, sin pasar por el dominio público y **sin CORS** (CORS solo lo aplica el
navegador; llamadas server-to-server no lo necesitan):

```
Base URL interna:  http://euroturbot-monitor:8000
```

Requisitos:
- Ambos contenedores deben estar en la red `proxy` (el monitor ya lo está por su compose).
- El backend manda la key en el header `X-API-Key`. La key queda **oculta en el servidor**,
  nunca llega al navegador.
- `MONITOR_CORS_ORIGINS` puede quedar **vacío**: no hace falta para este patrón.

Ejemplo (backend, cualquier lenguaje; acá con `curl`):

```bash
curl -H "X-API-Key: TU_KEY" http://euroturbot-monitor:8000/api/stats
curl -H "X-API-Key: TU_KEY" http://euroturbot-monitor:8000/api/report
```

Ejemplo Python (`httpx`/`requests`):

```python
import httpx
r = httpx.get("http://euroturbot-monitor:8000/api/report",
              headers={"X-API-Key": "TU_KEY"})
data = r.json()
```

> Solo si en el futuro una **página en el navegador** llama directo a la API (no el backend),
> ahí sí hay que completar `MONITOR_CORS_ORIGINS` con el dominio de esa página.

---

## Autenticación

Los endpoints de datos exigen una **API key** cuando se los llama desde un dominio
externo. Pasala de una de estas dos formas:

| Forma | Cómo | Cuándo |
|-------|------|--------|
| Header | `X-API-Key: TU_KEY` | Requests REST (`fetch`, `axios`, backend). **Preferida.** |
| Query param | `?api_key=TU_KEY` | **Obligatoria para SSE** (`EventSource` no permite headers custom). |

La key la entrega el administrador del monitor (variable `MONITOR_API_KEY` del servidor).
Además, tu dominio debe estar autorizado en la lista de CORS del servidor
(`MONITOR_CORS_ORIGINS`) para que el navegador permita las llamadas cross-origin.

> El control del pipeline (arrancar/parar/resetear) **no** está disponible para terceros.

### Códigos de error de auth

| Código | Significado |
|--------|-------------|
| `200` | OK. |
| `401` | API key ausente o inválida. |
| `403` | Intento de operar un endpoint de control (no permitido para terceros). |
| `503` | El servidor no tiene configurada la API key todavía. Contactá al administrador. |

---

## Endpoints (GET, solo lectura)

| Endpoint | Descripción |
|----------|-------------|
| `GET /api/summary` | **Estados persistentes desde la DB**: conteo de vouchers y cheques por estado. Disponible siempre, no depende de una corrida activa. |
| `GET /api/stats` | Snapshot puntual de la corrida **en curso** (estado, modo, progreso). Vacío/`null` si no hay nada corriendo. |
| `GET /api/stream` | **SSE**: stream en tiempo real del estado + eventos + vouchers nuevos. |
| `GET /api/tracker` | Resumen de archivos procesados + pendientes en `input/`. |
| `GET /api/history?limit=100&offset=0` | Vouchers procesados, paginados (más recientes primero). |
| `GET /api/report` | Resumen agregado por proveedor (totales ok/failed/skipped). |
| `GET /api/report/csv` | Descarga el reporte de proveedores como CSV (UTF-8 con BOM). |
| `GET /api/logs?lines=100` | Últimas N líneas del log de automatización. |
| `GET /api/sheets` | Hojas de cada `.xlsx` pendiente en `input/`. |

---

## Ejemplos

### 1. Estados desde la base (REST) — recomendado para monitoreo

```js
const HOST = 'https://TU-HOST';
const KEY = 'TU_KEY';

const res = await fetch(`${HOST}/api/summary`, {
  headers: { 'X-API-Key': KEY },
});
const s = await res.json();
console.log('vouchers ok:', s.vouchers.ok, '/', s.vouchers.total);
console.log('cheques ok :', s.cheques.ok, '/', s.cheques.total);
```

Respuesta:

```jsonc
{
  "vouchers": {                 // tabla processed_rows
    "pending": 314989,
    "processing": 0,
    "ok": 241585,
    "failed": 24831,
    "skipped": 12252,
    "total": 593657
  },
  "cheques": {                  // tabla processed_cheques
    "pending": 0,
    "ok": 1825,
    "failed": 224,
    "total": 2049
  }
}
```

Siempre trae todas las claves de estado (en 0 si no hay filas). Es una consulta simple
`GROUP BY status` a la base, así que refleja el acumulado histórico, no solo la corrida
actual.

### 2. Snapshot de la corrida en curso (REST)

Útil solo mientras el pipeline está corriendo (progreso en vivo). Fuera de una corrida,
`stats` viene en `null`.

```js
const res = await fetch(`${HOST}/api/stats`, {
  headers: { 'X-API-Key': KEY },
});
const stats = await res.json();
console.log(stats.state, stats.stats?.progress_pct);
```

### 3. Tiempo real (SSE con EventSource)

`EventSource` no permite headers, así que la key va **por query param**:

```js
const HOST = 'https://TU-HOST';
const KEY = 'TU_KEY';

const es = new EventSource(`${HOST}/api/stream?api_key=${encodeURIComponent(KEY)}`);

es.onmessage = (e) => {
  const snap = JSON.parse(e.data);
  console.log('estado:', snap.state, 'progreso:', snap.stats?.progress_pct);
  // snap.events   → eventos de log NUEVOS desde el último tick
  // snap.vouchers → vouchers procesados NUEVOS desde el último tick
};

es.onerror = () => {
  // 401/403 o desconexión: EventSource reintenta solo. Cerralo si querés parar:
  // es.close();
};
```

El stream emite un mensaje cada ~0.5 s. Cada mensaje trae el estado completo más
**solo los eventos y vouchers nuevos** (deltas) desde el tick anterior.

### 4. Historial paginado (REST)

```js
const res = await fetch(`${HOST}/api/history?limit=50&offset=0`, {
  headers: { 'X-API-Key': KEY },
});
const { vouchers, total, has_more } = await res.json();
```

### 5. Descargar CSV (link directo)

La key va por query param porque es una navegación, no un `fetch`:

```
https://TU-HOST/api/report/csv?api_key=TU_KEY
```

### 6. Desde backend / servidor (curl)

```bash
curl -H "X-API-Key: TU_KEY" https://TU-HOST/api/report
```

---

## Forma del objeto de estado (`/api/stats` y cada mensaje de `/api/stream`)

```jsonc
{
  "state": "idle | running | stopping | hung | finished | error",
  "mode": "full | pipeline | cheques | null",
  "thread_alive": true,
  "heartbeat_age": 3.2,              // segundos desde la última actividad
  "stats": {                         // null si nunca corrió en esta sesión
    "running": true,
    "finished": false,
    "error": null,
    "total": 10, "ok": 4, "failed": 1, "skipped": 0,
    "progress_pct": 50.0,
    "elapsed_seconds": 42.1,
    "last_activity_age": 3.2,
    "activity": { /* dict libre */ },
    "skipped_vouchers": [ /* ... */ ],
    "steps": [
      { "name": "...", "status": "pending|running|ok|failed|skipped",
        "duration": 1.23, "error": null }
    ]
  },
  "events": [   // en /api/stream: SOLO los nuevos desde el tick anterior
    { "seq": 12, "ts": "14:03:21", "level": "INFO", "message": "..." }
  ],
  "vouchers": [ // en /api/stream: SOLO los nuevos desde el tick anterior
    { "seq": 5, "ts": "14:03:22", "supplier_code": "...", "voucher": "...",
      "currency": "...", "status": "ok", "error": "" }
  ]
}
```

---

## Configuración del servidor (para el administrador)

Variables en `automatizacion/.env` (ver `.env.example`):

```bash
# Clave de lectura que exigen los endpoints GET a requests cross-origin.
# Generá una aleatoria y larga:
#   python -c "import secrets; print(secrets.token_urlsafe(32))"
MONITOR_API_KEY=una_key_larga_aleatoria

# Opcional: clave para operar el control (start/stop/reset) server-to-server.
# Vacía = el control solo funciona desde el dashboard local (same-origin).
MONITOR_ADMIN_KEY=

# Dominios de terceros autorizados a consumir la API por CORS (separados por comas).
MONITOR_CORS_ORIGINS=https://cliente-ejemplo.com,https://otro-dashboard.com
```

Notas:

- El **dashboard propio** (`GET /`) sigue funcionando sin key: se distingue por ser
  same-origin (header `Sec-Fetch-Site`). La key solo se exige a requests cross-origin.
- Si `MONITOR_API_KEY` queda vacía, los requests cross-origin reciben `503`
  (fail-closed): no se exponen datos por olvido de configuración.
- Un dominio que no esté en `MONITOR_CORS_ORIGINS` será bloqueado por el navegador
  (CORS), aun con key válida.
