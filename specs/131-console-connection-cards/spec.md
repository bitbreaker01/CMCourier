# 131 — Consola: credenciales y conexiones por alias

## Por qué

La pestaña `[2] CREDENCIALES` tiene dos tarjetas fijas (`cmis`, `as400`)
porque `SessionCredentials` copia los cuatro campos de `Secrets`, el
estado `conn` es un dict de dos claves hardcodeadas y el lockout de AS400
se decide por el literal `"as400"`. Con el registro (129) una config puede
necesitar tres conexiones con credenciales distintas, y la consola debe
mostrarlas TODAS, o el operador no tiene dónde cargarlas.

## Qué

**REQ-001 — Estado por alias.** `SessionCredentials` pasa a un mapa
`alias → Credential` mutable (`set(alias, username, password)`,
`get(alias)`, `complete(alias)`), `from_env(aliases)` lee `<ALIAS>_*` para
`cmis` + cada alias pedido, `to_secrets()` devuelve `Secrets`.
`ConsoleState.conn` se construye desde `["cmis", *aliases]` con
`ConsoleState.for_config(config)` (helper que usa
`config.connection_refs()`); `record_conn_result` cuenta intentos sólo si
`kind == "as400"` (el kind viaja en `ConnState.kind`), y
`as400_needs_lockout_confirm(alias)` / `reset_attempts(alias)` reciben el
alias. `creds_ready(required)` y `next_steps(required)` reciben el
conjunto de aliases requeridos en lugar del booleano `as400_required`.

**REQ-002 — Tarjetas dinámicas.** `CredsPane` compone una tarjeta por
alias: `cmis` primero (título `CMIS · Alfresco`), después cada
`ConnectionRef` con título `<alias> · <kind> · <host>` y subtítulo con los
sitios que la usan (`indexing`, `metadata:clientes`, `tracking`). Los ids
de widget son `<rol>-<alias>` y el ruteo parsea el prefijo con
`removeprefix` (nunca `endswith`). El contador de intentos/lockout aparece
sólo en tarjetas `as400`. Si la config cambia de conexiones por un
override de sesión (127 puede cambiar el `indexing.source`), las tarjetas
se recomponen al entrar a la pestaña.

**REQ-003 — Prueba por alias.** `run_single_check(alias, console)` prueba
`cmis` con `check_cmis` y cualquier otro alias con un nuevo
`cli/doctor.py:check_connection(config, secrets, alias) -> CheckResult`
(credenciales presentes + probe por kind: `SELECT 1 FROM SYSIBM.SYSDUMMY1`
/ `SELECT 1`). `as400_required()` de la app se reemplaza por
`required_aliases() -> tuple[str, ...]` derivado de
`connection_refs()` sobre la config efectiva; INICIO lista una línea por
conexión (`  <alias>  <estado>  <mensaje>`), el launcher y el doctor usan
el mismo conjunto. `[8] SYNC` toma la credencial del alias del sync
(`sync_unavailable_reason` ya lo hace por 129) y su pista de navegación
nombra ese alias.

**REQ-004 — Guía.** `docs/how-to/probar-la-consola.md`: `[2]` con N
tarjetas, ejemplo con `config-local-mssql.yaml` (tarjeta `clientes_sql ·
mssql · 127.0.0.1`), env vars por alias, y "Qué NO hace todavía" sin la
viñeta de alias.

## Escenarios

**E1 —** Con el config local (sin conexiones) `[2]` muestra sólo la
tarjeta `cmis`; INICIO no lista AS400. Con `config-local-mssql.yaml`
muestra `cmis` y `clientes_sql · mssql · 127.0.0.1`, y `next_steps`
exige ambas credenciales antes de tildar el primer paso.

**E2 —** Config con `indexing.source` AS400 inline + sync habilitado:
una sola tarjeta `as400` (deduplicada) con subtítulo `indexing ·
tracking`, con contador de intentos; tres fallos consecutivos piden
confirmación (lockout) igual que hoy. Una tarjeta `mssql` NUNCA muestra
el contador.

**E3 —** Cargar usuario/clave en la tarjeta `clientes_sql` y probar llama
`check_connection(config, secrets, "clientes_sql")` con la config
efectiva; OK → chip verde y `conn["clientes_sql"].status == "ok"`; editar
la clave invalida sólo esa conexión.

**E4 —** `SessionCredentials.from_env(["clientes_sql"])` con
`CLIENTES_SQL_USERNAME/PASSWORD` en el entorno precarga esa tarjeta;
`to_secrets().get("clientes_sql")` devuelve la credencial.

**E5 —** Los tests existentes de la consola (`tests/unit/cli/console`)
quedan en verde con las adaptaciones de firma (`as400_required` →
`required_aliases`, ids de widget por alias).

## Notas de implementación

- `state.py` suma `ConnInfo` + `connection_infos(config)` (una entrada por
  alias con `sites`, dedupe en orden de config; `tracking.as400_sync` se
  muestra como `tracking`) y `ConsoleState.rebuild_conn(config)`, que
  recompone `conn` conservando el estado de los alias que sobreviven con
  la misma kind. `CredsPane.rebuild_cards` sólo remonta la grilla si el
  conjunto cambió; las contraseñas ya tipeadas se restauran desde
  `creds` (se guardan en cada `Input.Changed`).
- El ruteo de ids usa `partition("-")` sobre `user-`/`pass-`: los alias
  del registro no admiten guiones, así que el prefijo es inequívoco.
- `ConsoleApp.effective_config()` centraliza `apply_overrides(config,
  overrides)`; `required_aliases()` sale de ahí. `check_as400` del doctor
  desaparece (su único consumidor era la tarjeta fija).
- `Input.value = ...` desde un test dispara `Input.Changed` como mensaje
  asíncrono: hace falta `await pilot.pause()` antes de leer `creds`.
- La pista de `[8] SYNC` nombra la tarjeta del alias del sync
  (`config.connection_ref("tracking.as400_sync")`).
