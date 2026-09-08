# 139 — `[9] YAML`: editor de configuración completo generado del schema

## Por qué

`[3] CONFIG` edita siete escalares como overrides de sesión (127/135).
El operador pidió poder configurar TODOS los parámetros desde la
consola. Escribir un formulario a mano para ~120 campos es inmantenible
— cada cambio de `schema.py` lo dejaría desincronizado. La alternativa
es GENERAR el formulario desde `PipelineConfig` (pydantic conoce tipos,
defaults, `Literal`s, descripciones y restricciones) y escribir el
resultado con `YamlDocument` (137), que preserva comentarios y valida
antes de pisar el archivo. `[3]` sigue siendo overrides de sesión;
`[9]` es el archivo.

## Qué

**REQ-001 — Walker puro `cli/console/schema_form.py`.** Sin Textual.
Tipos:

- `FieldSpec(path, label, kind, *, choices=(), default=None,
  required=False, description="", nullable=False, constraints="")` con
  `kind ∈ {"str","int","float","bool","choice","path","str_list",
  "str_map","connection"}`. `constraints` es texto para el placeholder
  (`"1..65535"`, `">= 1"`) derivado de `ge/gt/le/lt/min_length`.
- `Section(path, label, description, children: list[Node])`.
- `ListSection(path, label, item_model, discriminator, choices, items:
  list[Section])` — `list[BaseModel]`; `discriminator="kind"` cuando
  el item es unión discriminada, con `choices` = los kinds.
- `DictSection(path, label, value_model, items: list[Section])` —
  `dict[str, BaseModel]`; cada item lleva la clave en `path[-1]`.
- `Node = FieldSpec | Section | ListSection | DictSection`.
- `build_form(model: type[BaseModel], data: Mapping, *, path=()) ->
  list[Node]`: itera `model.model_fields`; resuelve `Annotated`,
  `Optional`, `Literal`, `list[...]`, `dict[str, ...]`, uniones
  discriminadas (`FieldInfo.discriminator` o `Field(discriminator=)` en
  el `Annotated`): para una unión el walker elige la variante según
  `data[...]["kind"]` (o la primera si falta) y emite un `FieldSpec`
  `kind="choice"` para `kind` seguido de los campos de ESA variante.
  `As400ConnectionConfig | str | None` y `As400ConnectionConfig | str`
  → `FieldSpec(kind="connection")`; `str` que se llama `connection` en
  una source mssql → `kind="connection"` también. `connections` (dict
  de uniones) → `Section` marcado `readonly=True` con `description="Se
  edita en [2] CREDENCIALES"` y sin hijos. Path/DirectoryPath/FilePath
  → `"path"`. `list[str]` → `"str_list"`; `dict[str,str]` →
  `"str_map"`. Desconocido → `"str"` (nunca falla; todo campo del
  schema aparece — test que recorre `PipelineConfig` y afirma que no
  hay campo sin nodo).
- `coerce(spec, raw: str) -> object`: `""` → `MISSING` (se borra la
  clave y aplica el default del modelo) salvo `nullable` (→ `None`); int/
  float → `int(raw)`/`float(raw)` y si no parsea devuelve el `raw`
  (pydantic reporta el error en su `loc`); bool → `sí/no/true/false/1/0`;
  `str_list` → split por coma, strip, sin vacíos; `str_map` → líneas
  `clave: valor`; `choice`/`connection` → raw; path → raw.
- `diff_edits(original: Mapping, working: Mapping) -> list[Edit]`:
  recorre ambos árboles; escalares distintos → `Edit(path, value)`;
  claves nuevas → `Edit(path, value)`; claves ausentes en `working` →
  `Edit(path, DELETE)`; listas: índices comunes se recorren, sobrantes
  en `working` → `Edit((…, i), value)` (YamlDocument.set con índice ==
  len → append), sobrantes en `original` → `DELETE` de mayor a menor
  índice; listas de escalares → un solo `Edit` con la lista completa.
  Reusa `Edit`/`DELETE`/`apply_edits` de 138 — moverlos a
  `config/yaml_doc.py` para que ambos importen de ahí.

**REQ-002 — `YamlPane` en `cli/console/yaml_pane.py`, tab `9·YAML`
(`id="yaml"`, teclas `9`/`F9`).** Estado: `original: dict` (el YAML crudo
convertido a `dict/list` planos vía `YamlDocument.get(())` +
`to_plain()`), `working: dict` (deepcopy), `rows: dict[str, tuple]` id
de fila → path. `compose`: cabecera `#yaml-head` (ruta del archivo,
"sin cambios" | "N cambios", último resultado de validación) +
`VerticalScroll #form`. `render()` vacía `#form` y monta desde
`build_form(PipelineConfig, working)`: `Section` → `Collapsible(title,
collapsed=True salvo primer nivel)`; `FieldSpec` → fila `Horizontal`
con `Label(label)`, widget (`Input` para str/int/float/path/str_list;
`Select` para bool (sí/no), choice, connection — opciones: alias del
registro de la kind adecuada + `"(inline — editar en [2])"` si el valor
actual es un mapping + `"(ninguna)"` si nullable; `TextArea` para
str_map) y `Static.err` vacío; `ListSection` → `Collapsible` con un
sub-`Collapsible` por item (título `[i] name|kind`) + botón "quitar" y
al final `Select` de kind (si discriminado) + botón "+ agregar";
`DictSection` → igual con `Input` de clave + "+ agregar". Cambiar un
Input/Select/TextArea → `coerce` → `working[path] = valor` (o borrar la
clave si MISSING) y actualiza el contador. Cambiar el `kind` de un item
discriminado → el item queda `{kind: nuevo, name: <si tenía>}` y se
re-renderiza ese `ListSection`. "+ agregar" → nuevo item `{kind: <select>}`
(o `{}`) y re-render; "quitar" → `del` y re-render. Se preservan los
`Collapsible` abiertos entre re-renders (set de paths abiertos).

**REQ-003 — Validar (`v`) y escribir (`w`) en `[9]`.**
`action_validate_yaml` (nueva tecla `v`, activa sólo con tab `yaml`):
`data = deepcopy(working)`, `_inject_default_kinds(data)`,
`PipelineConfig.model_validate(data)`; OK → cabecera "válido ✓"; error →
por cada `err["loc"]` buscar la fila cuyo path es prefijo más largo y
pintar `err["msg"]` en su `.err`, abrir sus `Collapsible` ancestros,
cabecera "N errores". `action_persist_overrides` (tecla `w`) despacha
por tab: `config` → 135; `yaml` → `YamlPane.write()`: si `working ==
original` → notify "sin cambios"; valida primero (mismo camino) y si
falla notify error y NO escribe; si `run_active` → notify "hay una
corrida activa" y NO escribe; `confirm("Escribir YAML", "N cambios
→ <path> (se guarda backup)")` → `YamlDocument.load`, `apply_edits(doc,
diff_edits(original, working))`, `doc.write(verify=load_config)`; éxito
→ `app.config = value`, `original = working` (deepcopy), `mark_doctor_
stale("cambió el YAML")`, `creds.prefill_missing`, `await creds_pane.
rebuild_cards()`, `config_pane.refresh_yaml_values()`,
`on_pii_override(None)`, `refresh_status()`, notify con backup;
`YamlWriteError` → notify error, nada cambia. `state.overrides` de
sesión se conservan.

**REQ-004 — Recarga.** Al entrar a `[9]` (`TabbedContent.TabActivated`)
si `working == original` se relee el YAML del disco (para reflejar
escrituras de `[2]`/`[3]`); si hay cambios pendientes no se pisa y la
cabecera avisa "el archivo cambió en disco" cuando el `mtime` difiere.
`CredsPane.apply_connection_edits` y `persist_overrides` de `[3]`
llaman `yaml_pane.reload_if_clean()`.

**REQ-005 — Ayuda y estado.** `HelpScreen.HELP` gana `[9] YAML` con
`v`/`w`; `F1–F8` pasa a `F1–F9`. La barra de estado no cambia.

## Escenarios

**E1 —** `build_form(PipelineConfig, data_sample)`: para cada campo de
cada modelo alcanzable (walk recursivo de `model_fields` en test) existe
un nodo con ese path; `cmis.workers` es `FieldSpec(kind="int",
constraints=">= 1")` (o el rango real del schema); `processing.mode` es
`choice` con `("batched","streaming")`; `trigger` es `Section` cuyo
primer hijo es `FieldSpec(("trigger","kind"), kind="choice",
choices=("csv","rvabrep","local_scan","single_doc"))` y el resto son
los campos de la variante `data["trigger"]["kind"]`; `metadata.sources`
es `ListSection(discriminator="kind", choices=("csv","as400","mssql"))`;
`metadata.field_sources` es `DictSection`; `metadata.field_aliases` es
`str_map`; `connections` es `Section(readonly=True)` sin hijos;
`tracking.as400_sync.connection` es `connection` nullable.

**E2 —** `coerce(int, "abc")` → `"abc"`; `coerce(int, "")` → MISSING;
`coerce(nullable str, "")` → None; `coerce(bool, "Sí")` → True;
`coerce(str_list, "a, b,,c")` → `["a","b","c"]`; `coerce(str_map,
"k: v\nk2: v2")` → `{"k":"v","k2":"v2"}`.

**E3 —** `diff_edits`: cambiar `cmis.workers` 4→8 → `[Edit(("cmis",
"workers"), 8)]`; borrar `cmis.workers` → `DELETE`; agregar un tercer
source → `Edit(("metadata","sources",2), {...})`; quitar el ÚLTIMO
source → `Edit(("metadata","sources",1), DELETE)`; quitar el source 0
de dos deja el 1 en la posición 0 → el diff por índice emite el item 0
como reemplazo completo y `DELETE` del índice 1. Aceptado: reemplazar
un item pierde sus comentarios; el test lo documenta. Listas de
escalares → un `Edit` con la lista.

**E4 —** Pilot: `9`, editar `#row` de `cmis.workers` a `8`, `v` →
"válido ✓"; `w`, `#yes` → el YAML tiene `workers: 8` con su comentario
inline intacto, hay backup, `app.config.cmis.workers == 8`, `[3]`
muestra 8 como valor del YAML, doctor stale.

**E5 —** Pilot: poner `cmis.workers` en `abc`, `v` → cabecera "1
errores", la fila muestra el mensaje de pydantic, el Collapsible `cmis`
está abierto; `w` → notify error, sin backup, YAML intacto.

**E6 —** Pilot: en `metadata.sources`, `+ agregar` con kind `mssql`,
completar `name`, `connection` (Select con los alias mssql del
registro), `table`, `prefix`; `v` válido; `w` → el YAML gana el item con
`kind: mssql` y `load_config` lo lee. "quitar" el item → desaparece.

**E7 —** Pilot: cambiar `trigger.kind` de csv a `local_scan` → el
Section trigger re-renderiza con los campos de `LocalScanTriggerConfig`
(`root`, etc.) y `csv_path` desaparece; `v` reporta los requeridos que
faltan.

**E8 —** Con `run_active`, `w` en `[9]` notifica y no escribe (el
formulario sí se puede editar).

**E9 —** Escribir desde `[2]` (138) y volver a `[9]` sin cambios
pendientes → el formulario muestra la conexión nueva en los Select de
`connection`.

## Notas de implementación

- `to_plain(node)` convierte `CommentedMap/Seq` a `dict/list`
  recursivamente (vive en `yaml_doc.py`).
- Rows: id `row-<n>` con contador; `rows[id] = (path, spec)`. Los
  handlers `on_input_changed` / `on_select_changed` /
  `on_text_area_changed` buscan la fila por `event.control.id`.
- `Select` de connection: opciones desde `working["connections"]` (no
  desde `app.config`, para reflejar lo que hay en el formulario) filtradas
  por `kind`.
- Textual `Collapsible` re-render: guardar `{path for c in
  query(Collapsible) if not c.collapsed}` antes de vaciar.
- Descripciones: `FieldInfo.description` → tooltip del `Input`
  (`widget.tooltip`) y `Label` con `?` si hay descripción.
- Funciones ≤50 líneas: `build_form` delega en `_node_for_field`,
  `_resolve_type`, `_union_variant`, `_list_node`, `_dict_node`.
- Tests: `tests/unit/cli/console/test_schema_form.py` (walker/coerce/
  diff, incluyendo el test de cobertura total del schema) y
  `test_yaml_pane.py` (pilot E4–E9 con `tmp_path`).
