"""Consulta READ-ONLY: lista las referencias INV* existentes en TourplanNX para un
proveedor, vía la API GetAccountingTransactions (misma que usa _read_existing_references).

Sirve para verificar si ciertas facturas se llegaron a crear (aunque el pipeline las haya
marcado failed por timeout del SAVE). No modifica nada.

Uso (cwd = automatizacion/):
    python consultar_refs.py 6PAU01
"""
import sys

from core.browser import BrowserManager
from core.session import SessionStore
from config.urls import spa_url
from modules.login import is_logged_in

FETCH_JS = """
    async (code) => {
        try {
            const resp = await fetch(
                '/tourplannx/tourplanservices/Services/Accounting.svc/json/GetAccountingTransactions',
                {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
                    body: JSON.stringify({request: {
                        Code: code, Ledger: 'AccountsPayable', InCurrency: null, Branch: null,
                        OrderBy: 'TranDate', ShowTrans: 'All', DateFrom: '2019-10-01T00:00:00'
                    }})
                }
            );
            if (!resp.ok) return {__status: resp.status};
            const data = await resp.json();
            const lines = data.AccountingTransactionLines || [];
            const refs = {};
            for (const l of lines) {
                const ref = l.TransactionReference;
                if (!ref) continue;
                refs[ref] = (refs[ref] || 0) + 1;
            }
            return {refs, total: lines.length};
        } catch (e) { return {__error: String(e)}; }
    }
"""


def main():
    code = sys.argv[1] if len(sys.argv) > 1 else "6PAU01"
    store = SessionStore()
    browser = BrowserManager(headless=True)
    if store.exists():
        page = browser.start(storage_state=store.state_path(), init_script=store.init_script())
    else:
        page = browser.start()
    try:
        page.goto(spa_url("creditor"))
        page.wait_for_load_state("networkidle")
        if not is_logged_in(page):
            print("SESIÓN EXPIRADA — corré el pipeline una vez para renovar la sesión, o logueate.")
            return 2
        raw = page.evaluate(FETCH_JS, code) or {}
        if raw.get("__error") or raw.get("__status"):
            print(f"Error consultando API: {raw}")
            return 1
        refs = raw.get("refs", {})
        inv = {r: n for r, n in refs.items() if r.startswith("INV") and code in r}
        print(f"Proveedor {code}: {raw.get('total', 0)} líneas de transacción totales.")
        print(f"Referencias INV*{code} existentes ({len(inv)}):")
        for r in sorted(inv):
            print(f"  {r}  ({inv[r]} líneas)")
        if not inv:
            print("  (ninguna) — no hay facturas INV para este proveedor en TourplanNX.")
        # Chequeo puntual de las 5 del run 13:07
        objetivo = ["INV2473206PAU01", "INV1256696PAU01", "INV1296516PAU01",
                    "INV503246PAU01", "INV194796PAU01"]
        print("\nChequeo de las refs del run 13:07:")
        for r in objetivo:
            print(f"  {r}: {'EXISTE' if r in refs else 'NO existe'}")
    finally:
        try:
            browser.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
