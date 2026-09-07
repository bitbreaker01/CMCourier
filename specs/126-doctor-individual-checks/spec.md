# 126 — Doctor: selección por check individual (consola + CLI)

## Por qué

El operador pidió correr checks del doctor de a uno y ver cuáles
existen. Hoy `run_doctor(selected=)` solo entiende GRUPOS
(`_CHECK_GROUPS`), y en la consola el selector de grupo estaba
**invisible** (Select con `width: 1fr` dentro de un `Horizontal` →
ancho 1) y con `all` duplicado. Además el check `as400_sync` no
pertenece a ningún grupo: solo corre con `all` y `--check as400_sync`
es imposible desde el CLI.

## Qué

**REQ-001 — `_selected` acepta nombres de check.** `run_doctor(...,
selected=X)` corre el check si `X == "all"`, si `X` es un grupo que
lo contiene, o si `X` es exactamente el nombre del check. Se publica
`CHECK_NAMES` (tupla ordenada de los checks seleccionables) y
`group_of(name) -> str` (primer grupo que lo contiene, o `"—"`).
`as400_sync` pasa a estar en el grupo `connections` (es un check de
conectividad; SKIP cuando el sync está deshabilitado, así no cambia el
veredicto de nadie que no lo use).

**REQ-002 — CLI `doctor --check`** acepta también nombres de check.

**REQ-003 — Consola.** El selector del doctor lista `all`, los grupos
(`grupo · X`) y los checks individuales (`check · X`); tiene ancho
fijo y sin duplicados. Cada fila de resultado muestra el grupo del
check. La ayuda (`?`) lista los checks disponibles.

## Escenarios

**E1 —** `run_doctor(selected="log_dir_writable")` devuelve exactamente
ese check (más el WARN incondicional de PII si aplica).
**E2 —** `run_doctor(selected="connections")` incluye `as400_sync`
(SKIP con sync deshabilitado).
**E3 —** El Select de la consola tiene ancho > 10 y sin valores
repetidos; incluye `log_dir_writable`.
