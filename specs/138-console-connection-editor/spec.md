# 138 — Editor de conexiones con alias en `[2] CREDENCIALES`

## Por qué

129 introdujo el registro `connections:` con alias, y 131 muestra una
tarjeta por alias en `[2]`. Pero agregar una conexión, cambiarle el host
o rebautizarla sigue exigiendo salir de la consola y editar el YAML a
mano — y el operador pidió explícitamente poder dar de alta conexiones
con alias DESDE la TUI. Con 137 (`YamlDocument`) escribir un bloque
anidado al YAML es una operación segura y verificada; este cambio le
pone la interfaz.

## Qué

**REQ-001 — Módulo puro `cli/console/connection_edit.py`.** Sin Textual.

- `ConnectionDraft(alias: str, kind: Literal["as400","mssql"], fields:
  dict[str, str])` — `fields` son los valores CRUDOS de los inputs
  (strings); las claves son los campos del modelo de esa `kind` menos
  `kind`: as400 → `host, port, database, driver, table`; mssql → `host,
  port, database, driver, encrypt, trust_server_certificate`.
- `validate_draft(draft, *, existing: set[str], editing: str | None) ->
  DraftErrors` (`dict[str, str]`, campo → mensaje; `{}` = válido).
  Reglas: alias con `_CONNECTION_ALIAS_RE` de schema (se importa, no se
  duplica), no `cmis`, no `as400`, único salvo que sea `editing`;
  `host` requerido; `port` entero 1..65535; `database` requerido para
  mssql; booleanos aceptan `true/false/sí/no/1/0` (case-insensitive);
  el resto opcional (vacío → se omite y aplica el default del modelo).
  La validación real la hace pydantic al hacer `load_config` sobre el
  tmp (137) — esta capa sólo da mensajes por campo ANTES de escribir.
- `draft_to_yaml(draft) -> dict[str, object]` — el mapping que va al
  YAML: `kind` + campos no vacíos ya tipados (int/bool). Sin defaults
  redundantes: si el operador dejó `port` vacío, no se escribe `port`.
- `Site(path: tuple[str|int,...], label: str, kind: str, current:
  str | None)` y `connection_sites(config) -> list[Site]` — los cuatro
  sitios que aceptan un alias: `("indexing","source","connection")` sólo
  si `config.indexing.source.kind == "as400"` (label `indexing.source`,
  kind as400); `("metadata","sources",i,"as400_connection")` por cada
  source as400 (label `metadata.sources[i] <name>`); `("metadata",
  "sources",i,"connection")` por cada source mssql; `("tracking",
  "as400_sync","connection")` si `as400_sync.enabled` (label
  `tracking.as400_sync`). `current` es el alias que hoy referencia el
  sitio, `None` si es inline o no está.
- `plan_write(draft, config, *, use_at: set[tuple]) -> list[Edit]` con
  `Edit(path, value)`: `("connections", alias)` → `draft_to_yaml`, y por
  cada sitio en `use_at` cuya `kind` coincida con la del draft:
  `Edit(site.path, alias)`. Un sitio de kind distinta → `ValueError`.
- `plan_delete(alias, config) -> list[Edit] | list[Site]`: si algún
  sitio lo referencia devuelve esos sitios (la UI rechaza y los lista);
  si no, `[Edit(("connections", alias), DELETE)]`.
- `plan_move_inline(site, alias, config) -> list[Edit]`: toma la
  conexión inline de `site` (as400 `As400ConnectionConfig`) y produce
  `Edit(("connections", alias), model_dump(...))` + `Edit(site.path,
  alias)`. `alias` validado como en `validate_draft`.
- `apply_edits(doc: YamlDocument, edits)`: `DELETE` → `doc.delete`,
  otro → `doc.set`.

**REQ-002 — `ConnectionEditScreen(ModalScreen[ConnectionDraft | None])`**
en `cli/console/connection_edit_screen.py`. Campos: `#alias` (Input,
`disabled=True` al editar), `#kind` (Select as400|mssql; al cambiar,
monta los inputs de esa kind), un `Input` por campo con id
`#f-<campo>` y placeholder con el default del modelo, un `Checkbox`
`#site-<n>` por cada `Site` de la kind elegida (label `Site.label`,
marcado si `current == alias`; **desmarcar un sitio que hoy usa este
alias está bloqueado**: el checkbox se re-marca y se avisa "para
soltarlo, apuntá el sitio a otra conexión desde su editor"), botones
`#save` / `#cancel`. `Enter` en cualquier Input = guardar; `Escape` =
cancelar. Al guardar: `validate_draft` → si hay errores, se pintan en
`#err-<campo>` (Static) y NO se cierra; si no, `dismiss(draft)` y la
lista `use_at` queda en `screen.use_at`. El `Select` de kind está
deshabilitado al editar (cambiar de kind = borrar y crear).

**REQ-003 — Controles en `[2]`.** Arriba de las tarjetas: botón
`#new-conn` "nueva conexión (n)". En cada tarjeta de alias del registro
(no CMIS, no `as400` inline): botones `#edit-<alias>` "editar" y
`#del-<alias>` "quitar". En cada tarjeta inline (`as400`): botón
`#move-<alias>` "mover al registro…" que abre un `ConnectionEditScreen`
con la kind y campos precargados del inline y el alias vacío
(editable). Tecla `n` en `[2]` = `#new-conn`. Todos los botones quedan
`disabled` mientras `run_active` (y `n` avisa "hay una corrida
activa"). CMIS no tiene botones (no vive en `connections`).

**REQ-004 — Escritura.** `CredsPane.apply_connection_edits(edits,
notice)`: `YamlDocument.load(app.config_path)`, `apply_edits`,
`doc.write(verify=load_config)`. Éxito → `app.config = result.value`;
`state.mark_doctor_stale("cambió connections")`; `state.creds.
prefill_missing(...)` para el alias nuevo; `await rebuild_cards()`;
`config_pane.refresh_yaml_values()`; `app.refresh_status()`; notify
`"{notice} — backup en {backup}"`. Error (`YamlWriteError`,
`YamlDocumentError`) → notify `severity="error"` con el mensaje
pydantic, YAML intacto. "Quitar" pide `app.confirm(danger=True)` con
el alias; si `plan_delete` devuelve sitios, no confirma: avisa
`"clientes_sql está en uso por: metadata.sources[0] (clientes)"`.

**REQ-005 — Los overrides de sesión no se pierden.** `app.config` se
reemplaza pero `state.overrides` se conserva (siguen aplicando encima
del nuevo config); `state.rebuild_conn(config)` mantiene el estado de
prueba de los alias que siguen existiendo (ya lo hace).

## Escenarios

**E1 —** Config válido con `connections: {rvi: as400...}` y una source
csv. Operador: `2`, `n`, alias `clientes_sql`,
kind mssql, host `sql01`, database `clientes`, guardar → el YAML tiene
`connections.clientes_sql: {kind: mssql, host: sql01, database:
clientes}` (sin `port`/`driver`/`encrypt` porque quedaron vacíos), hay
`.bak-*`, aparece la tarjeta `#card-clientes_sql` con chip "sin probar"
y el resto del YAML byte-idéntico.

**E2 —** Alias `Clientes` → error "alias: sólo minúsculas, dígitos y
`_`"; alias `cmis` → "reservado"; alias `rvi` existente → "ya existe";
puerto `70000` → "port: 1..65535". El modal no se cierra.

**E3 —** Editar `rvi`: `#alias` deshabilitado, `#kind` deshabilitado,
inputs precargados con los valores del YAML (`port` muestra `446` sólo si
está en el YAML; si no, placeholder `446`). Cambiar host a `as400b` y
guardar → una sola línea del YAML cambia. `state.conn["rvi"]` vuelve a
"sin probar" (`invalidate_conn`).

**E4 —** Config con `indexing.source` as400 inline y `metadata.sources
[0]` as400 con `as400_connection: rvi`. Crear `rvi2` as400 y marcar el
checkbox `indexing.source` → el YAML reemplaza el mapping inline de
`indexing.source.connection` por `rvi2`; la tarjeta inline `as400`
desaparece y aparece `rvi2` con sitio `indexing.source`. Intentar
desmarcar `metadata.sources[0]` en el editor de `rvi` → se re-marca y
avisa.

**E5 —** "quitar" `rvi` cuando `metadata.sources[0].as400_connection ==
"rvi"` → sin confirmación, notify error listando el sitio, YAML intacto.
"quitar" `rvi2` sin usos → confirm; `#yes` → desaparece del YAML y de
las tarjetas.

**E6 —** Tarjeta inline `as400` (indexing.source inline) → "mover al
registro…" con alias `rvi_main` → el YAML gana `connections.rvi_main`
con host/port/database/driver del inline y `indexing.source.connection:
rvi_main`; `load_config` valida; las credenciales `AS400_*` de sesión se
copian al nuevo alias (`creds.set(alias, user, pass)` si el inline las
tenía) para no re-pedirlas.

**E7 —** Con `run_active`, los botones están `disabled` y `n` notifica
sin abrir modal.

**E8 —** El verify falla (p. ej. el alias nuevo es mssql y se marcó un
sitio as400 — no debería poder pasar por la UI, pero `plan_write`
levanta `ValueError` antes de tocar el disco): notify error, sin backup.

## Notas de implementación

- `CredsPane._card` gana los botones; `on_button_pressed` despacha por
  prefijo `new-conn` / `edit-` / `del-` / `move-`. El id de botón usa el
  alias tal cual (regex `[a-z][a-z0-9_]*` es id válido de Textual).
- `ConnectionEditScreen.compose` con `Vertical` + `Grid` de
  `Label`/`Input`/`Static` de error; los inputs de kind se montan en un
  `#fields` `Vertical` que se vacía/remonta en `on_select_changed`.
- Precarga al editar: leer el YAML crudo (`YamlDocument.get(("connections",
  alias))`) y no el modelo, para no escribir defaults que el operador no
  puso. Si `get` devuelve MISSING (alias implícito) precargar desde el
  modelo.
- Tests: `tests/unit/cli/console/test_connection_edit.py` (puro:
  validate/draft_to_yaml/sites/plan_*), `test_connection_edit_screen.py`
  (Textual pilot: E1–E7 con `tmp_path` y `load_config`). `goto(pilot,
  app, "2")` desde conftest; `app.set_focus(None)` antes de teclas si un
  Input tiene foco.
- 50 líneas por función: separar `_compose_fields(kind)`,
  `_read_draft()`, `_show_errors(errors)`.
