# 136 — Refresh de documentación: consola, registro de conexiones, MSSQL

## Por qué

La 100 puso las referencias al día hasta la 0.100.0. Desde entonces
entraron 35 specs (101–135) y la documentación de operador no las
refleja. Verificado con `rg` sobre `docs/`:

* `cmcourier console` (123–135): CERO menciones en `docs/reference/cli.md`
  y `docs/tutorials/04-all-commands-tour.md`. El único documento es la
  guía de prueba `docs/how-to/probar-la-consola.md`.
* Registro de conexiones `connections:` con alias (129) y fuente de
  metadata MSSQL (130): ausentes de `docs/tutorials/01-the-yaml-config.md`
  y de `docs/reference/config-reference.yaml`.
* `config-reference.yaml` sigue diciendo `version 0.100.0`.
* `--max-duration` (103), `--total`/desglose de errores (104), instalador
  offline (101), sync recover batched (117/118): sin rastro en las
  referencias.
* `docs/diagrams/` no tiene el subsistema de sync NIARVILOG ni la consola.
* `docs/explanation/` no explica la consola (modelo draft/applied de
  overrides, pausa cooperativa, techo manual vs AIMD, ETA por ventana).

## Qué

**REQ-001 — `docs/reference/cli.md`.** Sección `console` (flags
`--config`, `--log-level`; pestañas 1–8; tabla de teclas por pestaña
tomada de `HelpScreen.HELP` en `cli/console/app.py`; lock de config
compartido con `run`; overrides de sesión vs `w`). Flags nuevos de los
comandos `*-pipeline run` desde 101 (verificar contra `--help`).

**REQ-002 — `docs/tutorials/04-all-commands-tour.md`.** Sección
`console` con un recorrido corto (credenciales → doctor → config →
correr → monitor) que remita a la guía how-to para el paso a paso.

**REQ-003 — `docs/tutorials/01-the-yaml-config.md` y
`docs/reference/config-reference.yaml`.** Bloque `connections:` (129)
con alias, `kind: mssql`, variables de entorno `<ALIAS>_USERNAME/_PASSWORD`;
`indexing.source.kind: mssql` (130) apuntando al alias; toda clave del
schema agregada entre 101 y 135 (verificar contra `config/schema.py`,
no contra la memoria). Header `version 0.100.0` → `0.111.0`. El YAML
sigue parseando.

**REQ-004 — `docs/diagrams/`.** `sync-subsystem.md` (SQLite ↔ AS400
NIARVILOG: claim / periodic / recover, con los comandos y la pestaña
`[8]`) y `console-flow.md` (máquina de estados de la consola: creds →
doctor → overrides draft/applied → lock → corrida → pausa/re-auth →
cierre). Mermaid, como los existentes.

**REQ-005 — `docs/explanation/`.** `operations-console.md`: por qué una
TUI de operación, draft/applied, por qué el trigger es del launcher y no
del YAML, pausa cooperativa y re-auth 401, techo manual vs AIMD (quién
gana y por qué el AIMD ve el valor cappeado), tasa por ventana vs
acumulada y por qué la ETA exige `total`. Referencias cruzadas a
`aimd-auto-tuning.md` y `heavy-light-lanes.md`.

**REQ-006 — Índices.** `docs/INDEX.md`, `docs/reference/README.md`,
`docs/explanation/README.md`, `docs/diagrams/README.md`,
`docs/tutorials/README.md` listan lo nuevo. `specs/100-docs-refresh/spec.md`
recibe una nota "continuado en 136".

## Escenarios

**E1 —** `rg -c "console" docs/reference/cli.md` > 0 y la sección lista
las mismas teclas que `HelpScreen.HELP`.

**E2 —** `python -c "import yaml; yaml.safe_load(open('docs/reference/config-reference.yaml'))"`
no falla y el archivo contiene `connections:` y `kind: mssql`.

**E3 —** Cada clave de `PipelineConfig` (recorrido recursivo del schema)
aparece al menos una vez en `config-reference.yaml`.

## Notas de implementación

- Sólo documentación: cero cambios en `src/`. Lo que la doc afirme se
  verifica contra el código o el `--help`, no contra specs viejas.
- Los diagramas siguen la convención Mermaid de `docs/diagrams/README.md`.
