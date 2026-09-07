# 127 — Consola: elegir el pipeline desde el launcher

## Por qué

En [5] CORRER el pipeline aparecía como `csv (fijado por el YAML)`: para
correr `rvabrep` o `local_scan` el operador tenía que editar el YAML y
reabrir la consola. El resto de la consola ya trabaja con overrides de
sesión que no tocan el YAML (124) — el `trigger` es el único bloque
que no se podía sobreescribir. `build_pipeline` despacha la estrategia
S0 exclusivamente por `config.trigger` (wiring.py, `_build_trigger_strategy`),
así que un override del bloque `trigger` alcanza a toda la corrida sin
tocar el wiring.

## Qué

**REQ-001 — Override de sesión del trigger.** `SessionOverrides` gana
`trigger: TriggerConfigUnion | None`. `apply_overrides` lo aplica con
`model_copy(update={"trigger": ...})`. `summary()` lo muestra como
`pipeline=<kind>`; `to_json()` (auditoría del batch) lo serializa con
`model_dump(mode="json")`. `cleared()` (botón "descartar overrides" de
[3]) NO lo toca: el pipeline se elige en [5], no en [3]. El draft de
[3] arrastra el trigger aplicado, así el aviso "SIN GUARDAR" no se
dispara por elegir un pipeline.

**REQ-002 — Selector en [5].** Fila `pipeline` con un `Select`
(`csv` / `rvabrep` / `local_scan` / `single_doc`), valor inicial = el
`kind` del YAML. Debajo, las filas de parámetros del kind elegido (las
otras quedan ocultas):

| kind | parámetros |
| --- | --- |
| csv | `csv_path`, columnas shortname / cif / system (pre-cargadas del YAML si el YAML es csv; si no, los defaults del schema) |
| rvabrep | `systems`, `document_types` (separados por coma; vacío = sin filtro) |
| local_scan | `scan_path`, `recursive` (no/sí) |
| single_doc | shortname, system, cif — van al `LaunchSpec`, no al trigger |

Cada cambio recalcula el trigger: si coincide con el del YAML el
override queda en `None`; si valida (pydantic: el CSV / la carpeta
deben existir) se aplica **de inmediato** a `state.overrides.trigger` y
el doctor queda stale; si NO valida, lo aplicado no cambia y el error
se muestra en la línea de guardas. `build_spec()` re-valida: nunca se
lanza con un trigger inválido ni con uno "viejo" mientras el operador
tipea uno nuevo.

**REQ-003 — Doctor sobre la config efectiva.** El doctor de la consola
corre sobre `apply_overrides(config, overrides)`, no sobre el YAML
crudo (antes marcaba *stale* al cambiar overrides y después los
ignoraba — inconsistente). `sample_dry_run` pasa a ejercitar el
pipeline que realmente se va a lanzar.

**REQ-004 — Resumen y auditoría.** El resumen de [5] muestra
`pipeline  <kind>` (+ ` (override)` cuando difiere del YAML). El
`pipeline_kind` de la auditoría del batch sale de la config efectiva
(ya era así en `ConsoleRunManager._audit`).

## Escenarios

**E1 —** `apply_overrides(cfg, SessionOverrides(trigger=Rvabrep…))`
devuelve una config cuyo `trigger.kind == "rvabrep"`; el YAML original
no cambia; `summary()` contiene `pipeline=rvabrep`.
**E2 —** En [5], elegir `rvabrep` muestra las filas de filtros, oculta
las de csv, y `state.overrides.trigger.kind == "rvabrep"`; el doctor
queda stale. Volver a `csv` con el mismo path del YAML deja
`overrides.trigger is None`.
**E3 —** Elegir `local_scan` con una carpeta inexistente: el override
no se aplica, la guarda muestra el error y `build_spec()` devuelve
`None`.
**E4 —** Elegir `single_doc` y lanzar produce un `LaunchSpec` con
shortname/system y el `run_manager` recibe una config efectiva con
`trigger.kind == "single_doc"`.
**E5 —** "descartar overrides" en [3] conserva el pipeline elegido en [5].
