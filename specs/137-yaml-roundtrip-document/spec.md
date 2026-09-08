# 137 — `YamlDocument`: escritura round-trip del YAML con `ruamel.yaml`

## Por qué

135 escribe siete escalares al YAML con un parche textual porque no
queríamos una dependencia nueva. Ese parche NO puede agregar bloques
anidados (`connections.clientes_sql: {...}`), borrar claves ni tocar
elementos de listas — y eso es exactamente lo que necesitan el editor de
conexiones (138) y el editor de configuración completo (139), el Plan C
que el operador pidió. Un parser YAML casero para eso es la clase de
código que se rompe en producción. `ruamel.yaml` en modo round-trip
conserva comentarios, comillas, orden y anchors; la verificación
semántica de 135 (recargar y comparar) sigue siendo la red de seguridad.

## Qué

**REQ-001 — Dependencia.** `ruamel.yaml>=0.18,<0.19` en `pyproject.toml`
(ya agregada con `uv add`; `uv.lock` actualizado — el instalador offline
lee el lock, así que el wheel viaja solo). Trae `ruamel.yaml.clib` como
extra opcional con wheels para Windows/Linux x86_64. Ship `py.typed`:
mypy strict sin `ignore_missing_imports`.

**REQ-002 — `cmcourier.config.yaml_doc.YamlDocument`.** Servicio puro,
sin Textual:

- `YamlDocument.load(path: Path) -> YamlDocument`: resuelve symlinks
  (`path.resolve()`, B2 de 135), lee bytes UTF-8, detecta el EOL
  (`\r\n` si aparece, si no `\n`), **normaliza a `\n` antes de parsear**
  (ruamel mezcla EOLs si no — verificado en spike), detecta la
  indentación del archivo (indent de mapping = primer hijo indentado;
  indent y offset de secuencias = primera línea `- ` bajo una clave;
  defaults 2 / 4 / 2) y carga con `YAML(typ="rt")`,
  `preserve_quotes=True`, `width=4096`, `indent(mapping, sequence,
  offset)` detectados. Clave duplicada → `YamlDocumentError` (ruamel
  levanta `DuplicateKeyError`; se traduce). Raíz que no es mapping →
  `YamlDocumentError`.
- `get(path) -> object | MISSING`, `has(path)`, `set(path, value)`,
  `delete(path)`. `path: tuple[str | int, ...]` — `str` indexa
  mappings, `int` indexa secuencias. `set` crea los mappings intermedios
  que falten (block style); `value` acepta escalares, `None` (→ `null`),
  `dict` y `list` anidados (se convierten recursivamente a
  `CommentedMap` / `CommentedSeq` en block style) y modelos pydantic
  (`model_dump(mode="json", exclude_defaults=False)` es responsabilidad
  del llamador — `set` NO conoce pydantic). `delete` de una clave
  inexistente es no-op; `delete` de un índice fuera de rango →
  `YamlDocumentError`. Al agregar una clave NUEVA de primer nivel se
  inserta una línea en blanco antes (legibilidad; los bloques del
  proyecto van separados por línea en blanco).
- `append(path, value)` agrega al final de una secuencia (la crea si no
  existe).
- `text() -> str`: dump con el EOL original.
- `write(*, verify: Callable[[Path], T]) -> WriteResult[T]`: escribe
  `.<name>.<pid>.tmp` en el MISMO directorio, llama `verify(tmp)` (si
  levanta cualquier excepción → `YamlWriteError(str(exc))`, tmp
  borrado, original intacto, SIN backup), backup
  `<name>.bak-YYYYmmdd-HHMMSS` (copia con `copy2`), `shutil.copymode`
  (B3), `tmp.replace(path)` atómico. `WriteResult(value: T,
  backup_path: Path, path: Path)`. `finally: tmp.unlink(missing_ok=True)`.
- Un documento escrito una vez puede seguir editándose y escribirse de
  nuevo (el backup es por escritura).

**REQ-003 — `persist_overrides` migra a `YamlDocument`.** Un solo
escritor en el proyecto. `persist.py` conserva su API pública
(`persist_overrides`, `PersistError`, `PersistResult`) y sus garantías
(sólo escalares, trigger no se persiste, verificación `loaded ==
apply_overrides(config, ov sin trigger)`, backup, symlink, modo, CRLF),
pero implementa con `doc.set(path, value)` + `doc.write(verify=...)`.
Se borran `_patch`, `_find_key`, `_block_end`, `_child_indent`,
`_KEY_RE`, `_render`. `PersistError` envuelve `YamlDocumentError` /
`YamlWriteError`.

**REQ-004 — Contratos de 135 que CAMBIAN.** Flow style
(`cmis: {base_url: x, repo_id: y}`) ya no es motivo de rechazo: ruamel
setea la clave dentro del mapping flow y el resultado valida — el test
`test_flow_style_block_refuses_to_write` pasa a
`test_flow_style_block_is_written_in_place` (el archivo cambia, valida,
el resto byte-idéntico). Clave duplicada sigue fallando cerrado (ahora
al CARGAR, con `PersistError` que menciona la clave). La verificación
semántica queda.

## Escenarios

**E1 —** `# top\r\ncmis:\r\n  workers: 4  # ojo\r\n` + `set(("cmis",
"workers"), 8)` → `text()` es `# top\r\ncmis:\r\n  workers: 8  # ojo\r\n`
— byte-idéntico salvo el `8`, EOL CRLF en TODAS las líneas.

**E2 —** YAML con `metadata:\n  sources:\n    - name: a\n      kind: csv\n`
(listas con offset 2) + `set(("x","y"), 1)` → las líneas de la lista
quedan byte-idénticas (`    - name: a`), y aparece `\nx:\n  y: 1\n` al
final con línea en blanco antes.

**E3 —** `set(("connections","clientes_sql"), {"kind":"mssql","host":
"h","port":1433,"database":"m","encrypt":True})` sobre un YAML sin
`connections:` → bloque block-style al final; `load_config` lo lee como
`MssqlConnectionConfig`. `delete(("connections","clientes_sql"))` →
desaparece el bloque completo, el resto byte-idéntico.

**E4 —** `set(("metadata","sources",0,"as400_connection"), "rvi")` sobre
una lista existente → sólo cambia ese elemento. `append(("metadata",
"sources"), {...})` → nuevo elemento al final con la misma indentación.

**E5 —** `write(verify=f)` con `f` que levanta `ValueError("x")` →
`YamlWriteError` con "x", el archivo original byte-idéntico, ningún
`.bak-*`, ningún `.tmp`. Con `f` que devuelve → `WriteResult.value` es
eso, hay backup, el modo `0600` se conserva, y a través de un symlink el
link sigue siendo symlink y el archivo real cambió.

**E6 —** Clave duplicada (`workers: 4\n  workers: 5`) → `load` levanta
`YamlDocumentError` que menciona `workers`.

**E7 —** Todos los tests de `test_persist.py` y `test_persist_flow.py`
verdes con el contrato de REQ-004; `sample/*.yaml` y
`docs/reference/config-reference.yaml` (mayormente comentarios)
sobreviven un `load` + `set` de un escalar existente + `text()` con
diff de UNA línea (test paramétrico sobre los samples).

## Notas de implementación

- Módulo: `src/cmcourier/config/yaml_doc.py` (capa config, no consola:
  139 lo usa desde la consola pero es un servicio de configuración).
  Tests: `tests/unit/config/test_yaml_doc.py`.
- Detección de indentación: `_detect_indent(text) -> tuple[int, int,
  int]`. Mapping: primera línea no vacía/no comentario con indent > 0
  cuyo padre es una clave sin `- `. Secuencia: primera línea que empieza
  (tras espacios) con `- ` y su clave padre: `sequence = indent_dash -
  indent_parent + 2`, `offset = indent_dash - indent_parent`. Si no hay
  listas: `sequence = mapping * 2`, `offset = mapping`... NO — ruamel
  exige `sequence >= offset + 2`; con mapping 2 y sin listas usar
  (2, 4, 2), que es el estilo de todos los YAML del proyecto.
- Conversión dict/list → `CommentedMap` / `CommentedSeq` recursiva con
  `fa.set_block_style()` en cada contenedor nuevo.
- Línea en blanco antes de una clave nueva de primer nivel:
  `doc.yaml_set_comment_before_after_key(key, before="\n")` — verificar
  en el spike que no duplique líneas en re-dumps.
- `ruamel` emite `1.0` para floats y `true`/`false` para bools, iguales
  a lo que 135 renderizaba. Los `str` que parecen números o booleanos
  (`"8"`, `"true"`) se emiten con comillas automáticamente — bien: el
  llamador pasa tipos ya parseados, nunca strings crudos.
