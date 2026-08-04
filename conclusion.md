* ### Conclusión de la investigación en vivo 
    Con la lupa real de TourplanNX (sin código, sin caché, sin Excel de por medio) verifiqué 3 cosas directamente:

1. Proveedores completos como "DELETED": 1ACA09 y 1AGUA2 aparecen literalmente como DELETED -Hotel... en el buscador de creditors. Sus vouchers nunca van a aparecer en ninguna lupa — no es un bug, el proveedor ya no existe como entidad activa.

2. 1AIR01 (proveedor activo, "Air France"): la lupa sin ningún filtro solo devuelve 1 voucher disponible en total (uno viejo de 2019, monto $0). Al buscar puntualmente el voucher 1683852 (uno de los 2 pending que tiene este proveedor), TourplanNX respondió con un error explícito del servidor: "1040 — No vouchers exist with this criteria".

3. Crucé eso con la DB: 1AIR01 tiene 96 filas ok (ya facturadas en corridas previas) y solo 2 pending (1683852/1683853). En el Excel esos 2 tienen ProductCost real (156 y 136.5 USD), Voucher_Status=OK Confirmed, InvoicedServiceCurrency=0 — es decir, el Excel dice "sin facturar todavía", pero TourplanNX dice que el voucher no existe en el módulo de Accounts Payable.

 ### Veredicto
Mi hipótesis original ("Excel viejo, ya facturado") tenía razón en el fondo, pero el diagnóstico anterior desde el Excel estaba mal enfocado. El Excel es un snapshot de un momento anterior — no puede ver lo que pasó después: facturación manual por un contador, facturación por otro proceso, o cancelación/ajuste del voucher directamente en TourplanNX. Cuando eso pasa, el voucher deja de existir en AP aunque el Excel siga mostrándolo como pendiente con monto. El servidor lo confirma con un error explícito, no es un timeout ni un fallo silencioso nuestro.

No until este caso puntual sugiere que sea un bug de nuestro código (rango, normalización de número, filtro de estado) — la búsqueda sin ningún filtro tampoco lo encuentra, y el servidor da error explícito, no vacío ambiguo.

 ### Lo que esto significa para el cambio que ya hice
El commit anterior (dejar skipped como terminal) sigue siendo correcto — de hecho ahora está mejor justificado: estos vouchers no son reintentables porque ya no existen en TourplanNX, sin importar cuántas veces se reintenten. Eso sí, hay que corregir un matiz: como en 1AIR01 estos casos siguen en estado pending (no llegaron a marcarse skipped en la corrida anterior), confirmá si el pipeline los marca skipped en la próxima pasada — si no, valdría la pena revisar por qué quedaron pending sin decisión.

¿Querés que arme un query sobre toda la DB para cuantificar cuántos de los 332k pending restantes son de proveedores DELETED (fácil de detectar de antemano y saltar sin ni abrir la lupa) versus vouchers puntuales tipo 1683852?