# 114 — `s4_smart_routing` default on

## Por qué

Combinación de defaults incoherente detectada por la auditoría:

* `processing.s4_use_processes` default **True** (066) — S4 corre en un
  `ProcessPoolExecutor`.
* `processing.s4_smart_routing` default **False** (094) — TODO va al
  pool, incluidos los PDF nativos.

El trabajo real de un PDF nativo es `shutil.copy2` (I/O que libera el
GIL) — mandarlo al process pool le suma pickle del `RVABREPDocument`,
IPC ida y vuelta y contención de los slots del pool que los TIFF
paginados (CPU-bound de verdad) sí necesitan. El comentario de 094
(`staged.py:938-945`) cifra el costo del spawn en ~30 s/doc en Windows
en el peor caso.

094 dejó el default en `False` por prudencia de rollout ("preservar el
comportamiento pre-094"). Ya corrió en staging sin regresiones; la
prudencia hoy cuesta rendimiento en el default.

## Qué

**REQ-001 — Default a `True`.** `ProcessingConfig.s4_smart_routing`
pasa a `True`. El operador puede volver al comportamiento 094-off con
`s4_smart_routing: false` en el YAML (el opt-out queda).

**REQ-002 — Sin cambio de lógica.** El ruteo (`route_inline = pool is
None or (smart_routing and doc.is_pdf)`) no se toca. Solo cambia el
default del schema.

**REQ-003 — Docs actualizadas.** `01-the-yaml-config.md` refleja el
default nuevo.

## Escenarios

**E1 — Default:** `ProcessingConfig()` → `s4_smart_routing is True`.
**E2 — Opt-out:** `ProcessingConfig(s4_smart_routing=False)` respeta el
override.
**E3 — Ruteo con default:** con pool activo y default, un PDF nativo va
inline y un TIFF paginado va al pool.
