# 145 — Manifest de tipos CM: adiós `MetadatosCM.csv`

## Por qué

`MetadatosCM.csv` es una copia hecha a mano de lo que Content Manager ya
publica en `getTypeDefinition`. Medido contra el dump real de PRD
(`reference-data/cmis-responses/EjemploRespuestaCMIS.txt`, 368 tipos) el
CSV ya está desincronizado en las DOS direcciones:

- el servidor marca `required:true` en 234–363 tipos para `BAC`,
  `BAC_Nombre_Documento`, `BAC_Expediente_Digital`,
  `BAC_Expedientes_Clientes`, `BAC_Nombre_Carpeta`, `BAC_ID_Corto` y el
  CSV no los lista nunca;
- el CSV marca `Fvenc_Inicio`/`Fvenc_Fin` como `Requerido=Yes` y el
  servidor dice `required:false`;
- `CMISPropertyId` está poblado en 5 de 1231 filas: las otras 1226 clases
  irían al wire como `BAC_CIF` a secas, sin el prefijo `clbNonGroup.` que
  PRD exige.

Además `MapeoRVI_CM.csv` arrastra columnas que o están muertas
(`IDSistema`: cero lecturas en `src/`) o duplican al servidor
(`IDClaseDocumental`, `CMISType`, `CMISFolder`).

El operador no quiere mantener nada a mano que el servidor ya sepa. Lo
único que el servidor NO sabe es (a) qué código RVI (por sistema) va a qué
clase CM y (b) de dónde sale el VALOR de cada propiedad — eso ya vive en
`metadata.field_sources` del YAML y no cambia.

## Qué

### REQ-001 — `MapeoRVI_CM.csv` reducido a `IDSistema,IDRVI,IDCM`

- Nuevo modo de `MappingConfig`: **manifest** = `rvi_cm_csv_path` +
  `type_manifest_path` (JSON, REQ-002). El modo split (`rvi_cm_csv_path` +
  `metadatos_csv_path`) queda como legacy: sigue funcionando pero se marca
  deprecado en docs; el modo consolidado (`csv_path`) no cambia (fixtures).
  El validador exige `rvi_cm_csv_path` + exactamente uno de
  `metadatos_csv_path` / `type_manifest_path`.
- En modo manifest las columnas requeridas de `MapeoRVI_CM` son `IDRVI` e
  `IDCM`; `IDSistema` es opcional (columna ausente ≡ todas vacías). Nombres
  configurables: `rvi_cm_id_sistema_column` (default `IDSistema`).
- La clave del mapeo es `(sistema, id_rvi)`. `MappingService.get_mapping(id_rvi,
  system_id=None)`: primero busca `(system_id, id_rvi)`, después el comodín
  `("", id_rvi)`; si no hay ninguno → `IDRViNotMappedError`. Comparación de
  sistema case-insensitive y con `strip()`. Duplicados de la MISMA clave →
  gana la primera fila, WARNING.
- Los call sites pasan el sistema del trigger: S2 (`staged.py`),
  `recovery.py`, `doctor.py`, `inspect.py`. Helper en `domain/models.py`
  `trigger_system_id(trigger) -> str | None` (`ClientTrigger.system_id` o
  `audit_row()["system_id"]`). Los modos consolidado/split ignoran el
  sistema (siempre comodín).
- Cada fila resuelve su `IDCM` contra el manifest: `CMMapping` sale con
  `id_corto=IDCM`, `cmis_type=entry.type_id`, `cmis_folder=entry.folder`,
  `clase_id=entry.local_name`, `clase_name=entry.display_name`,
  `required_metadata_fields=` nombres canónicos de las propiedades con
  decisión `usar`, `cmis_property_ids={canónico: id de wire}`. Nombre
  canónico = id de wire sin prefijo hasta el último `.` o `:`
  (`clbNonGroup.BAC_CIF` → `BAC_CIF`, `cmcourier:BAC_CIF` → `BAC_CIF`), lo
  que engancha directo con las claves de `metadata.field_sources`.
- Un `IDCM` que no existe en el manifest: la fila se descarta con WARNING y
  el código queda en `MappingService.missing_cm_codes` (tupla ordenada) para
  que `types check` (REQ-005) y `doctor` lo muestren.
- Nada cambia aguas abajo: S3, `staged.py:1453-1454`, NIARVILOG
  (`IDNBAC=id_corto`, `TIPIDN=cmis_type`), `[0] PRUEBA` y doctor siguen
  leyendo `CMMapping`.

### REQ-002 — Manifest JSON de tipos, indexado por ID corto

Módulos: `domain/cm_types.py` (modelos frozen), `services/type_manifest.py`
(lógica pura: derivar, decidir, diff, merge), `adapters/manifest/json_store.py`
(leer/escribir JSON, escritura atómica: tmp + `os.replace`).

```
CmPropertyDef(id, display_name, property_type, cardinality, updatability,
              required, max_length: int|None, default_value: str|None,
              inherited: bool, choices: tuple[str, ...])
CmTypeEntry(id_corto, type_id, local_name, display_name, folder,
            folder_source: "derivada"|"manual", folder_ok: bool|None,
            properties: tuple[CmPropertyDef, ...],
            decisions: Mapping[prop_id, "usar"|"omitir"],
            reviewed: bool, changes: tuple[str, ...],
            missing_on_server: bool)
CmTypeManifest(service_url, repository_id, discovered_at (ISO),
               types: Mapping[id_corto, CmTypeEntry],
               without_code: tuple[(type_id, display_name), ...])
```

Reglas:

- **ID corto** = `defaultValue` de la propiedad cuyo id (sin prefijo) es
  `BAC_ID_Corto`; fallback: prefijo `^(\S+) - ` del `displayName`. Sin
  ninguno → va a `without_code`.
- **ID corto compartido** (visto en PRD: `BAC_01_01_01_03_07_01` y
  `..._02` con default `DC35`, 759 tipos): NO aborta. Se elige un
  ganador determinístico — primero el tipo cuyo `displayName` empieza
  con `"<id_corto> - "`; si ninguno o varios, el primero en el orden del
  servidor — y los demás van ENTEROS (como `CmTypeEntry`) a
  `CmTypeManifest.duplicates: tuple[CmTypeEntry, ...]`. El ganador queda
  `reviewed=False` con `changes=("ID corto compartido con <type_id>
  (<display_name>)", ...)`. `types discover` imprime un WARNING por cada
  duplicado; `types show IDCM` lista los candidatos; `types review IDCM
  --type-id TYPE_ID` promueve un duplicado a ganador (el ganador anterior
  pasa a `duplicates`, sin red: los duplicados ya están completos).
  `diff`/`update` alinean el manifest vivo con la elección local antes de
  comparar (si el `type_id` elegido localmente está entre los candidatos
  vivos, ese es el ganador vivo). `types check`: WARNING "ID corto
  compartido" por cada duplicado; CRITICAL si ese ID corto lo usa el
  mapeo. JSON: `"duplicates": [entry, ...]` (misma forma que `types`).
- Solo entran tipos `creatable` con `baseId == "cmis:document"`.
- **Carpeta derivada** = `/$type/<localName>` (mismo fallback histórico de
  `compute_cm_folder`). `folder_ok` se llena verificando con
  `IUploader.verify_folder_exists` (REQ-003); `None` = no verificada.
- **Propiedades**: se guardan todas las escribibles (`updatability` in
  `readwrite`/`oncreate`); las readonly NO se guardan. `cmis:name` y
  `cmis:objectTypeId` se excluyen (las pone el código).
- **Decisión sugerida** al descubrir o al aparecer una propiedad nueva:
  `required and default_value is None` → `usar`; el resto → `omitir`.
  `reviewed=False` hasta que el operador lo marque.
- `usable_properties(entry) -> tuple[CmPropertyDef, ...]` = decisión `usar`,
  en orden del servidor. `canonical_name(prop_id) -> str`.
- **Diff** `diff_manifest(local, live) -> ManifestDiff`: tipos nuevos, tipos
  ausentes en el servidor, y por tipo propiedades agregadas/quitadas y
  cambios de `required`/`default_value`/`max_length`/`property_type`/
  `updatability`/`cardinality`; también cambio de `local_name`/`type_id`
  (→ carpeta derivada cambia si `folder_source == "derivada"`).
  `ManifestDiff.is_empty`, `ManifestDiff.render() -> str` legible.
- **Merge** `apply_diff(local, live, only: set[str] | None) -> CmTypeManifest`:
  reemplaza propiedades del tipo con las del servidor, conserva las
  decisiones de las propiedades que siguen existiendo, sugiere decisión
  para las nuevas, conserva `folder` manual, recalcula la derivada,
  setea `reviewed=False` y `changes=[...]` descriptivos en los tipos
  tocados; tipos ausentes en el servidor → `missing_on_server=True` (no se
  borran); tipos nuevos entran con `reviewed=False`. Los tipos sin cambios
  no se tocan.
- `set_decision(manifest, id_corto, prop_id, decision)`,
  `set_folder(manifest, id_corto, folder)` (→ `folder_source="manual"`,
  `folder_ok=None`), `mark_reviewed(manifest, id_corto)` (limpia `changes`).
  Todo devuelve un manifest nuevo (inmutable).
- JSON: `{"version": 1, "service_url", "repository_id", "discovered_at",
  "types": {id_corto: {...}}, "without_code": [...]}`, claves ordenadas,
  `ensure_ascii=False`, indent 2 — diffeable en git.

### REQ-003 — Descubrimiento con progreso

- Port `IUploader.get_type_descendants(include_property_definitions: bool)
  -> Sequence[Mapping[str, Any]]` (`cmisselector=typeDescendants`,
  `depth=-1`) + adapter en `CmisUploader` (mismo estilo que
  `get_type_definition`, sin loop de retry, `_network_log`).
- `services/type_discovery.py` `TypeDiscoveryService(uploader, workers=8)`
  `.discover(*, verify_folders=True, on_progress=None) -> CmTypeManifest`:
  fases (`SyncProgress` de 144, reusado): `"descargando tipos" 0/0` →
  `"procesando tipos" k/N` → `"verificando carpetas" k/N` (ThreadPool,
  progreso cada tipo, un fallo de red en una carpeta → `folder_ok=None` +
  WARNING, no aborta). `.fetch_live()` = discover sin verificar carpetas
  (para `diff`).

### REQ-004 — Comandos CLI `cmcourier types …`

Grupo nuevo `types` (`cli/commands/types.py`), path del manifest por
default `mapping.type_manifest_path` del YAML, override `--manifest PATH`.

- `discover [--no-verify-folders] [--force]`: escribe el manifest. Si ya
  existe uno con tipos revisados, se niega salvo `--force` (mensaje: usá
  `update`). Progreso en stderr como `sync recover` (144).
- `show <IDCM> [--live]`: tabla de propiedades (id, tipo, card, escritura,
  límites, default, decisión, nombre) del manifest o del servidor.
- `diff`: descarga el árbol y muestra `ManifestDiff.render()`; exit 1 si
  hay diferencias, 0 si no.
- `update [--only IDCM ...]`: `apply_diff` y guarda; resume tipos tocados.
- `review <IDCM> [--use PROP ...] [--omit PROP ...] [--folder PATH]
  [--done]`: edita decisiones/carpeta y marca revisado con `--done`.
  PROP acepta id de wire o nombre canónico.
- `check`: REQ-005. Exit 1 si hay CRITICAL.

### REQ-005 — `types check`: manifest ↔ YAML ↔ CSV

Para cada `IDCM` referenciado por `MapeoRVI_CM.csv`:

- CRITICAL: código ausente en el manifest (`missing_cm_codes`); propiedad
  `usar` sin entrada en `metadata.field_sources`; tipo `missing_on_server`.
- WARNING: tipo con `reviewed=False`; `folder_ok is False`; propiedad
  requerida sin default marcada `omitir`; `field_sources` con
  `validation.max_length`/patrón que exceda `max_length` del CM (cuando la
  validación declare largo); propiedad `usar` cuyo `property_type` es
  `datetime`/`integer`/`boolean` y la fuente es un valor fijo no parseable.
- INFO: entradas de `field_sources` que ningún tipo mapeado usa.

Salida agrupada por severidad, `--json` opcional. `doctor` gana un check
`cm_manifest` que reutiliza el mismo servicio y reporta sólo CRITICAL.

### REQ-006 — Pestaña `M·MODELO` en la consola

- Nueva pestaña al FINAL de `_TABS` (id `modelo`, tecla `m`, F11), pane
  `cli/console/modelo_pane.py` (patrón de `sync_pane.py`/`practice_pane.py`).
- Layout: botonera `[Descubrir] [Comparar] [Actualizar] [Verificar YAML]`
  + línea de progreso `Static#md-progress` (como `#sy-progress`) +
  `DataTable#md-types` (IDCM, nombre, usar, omitir, revisado ✓/✗, carpeta
  ✓/✗/?) + `DataTable#md-props` del tipo seleccionado (propiedad, req,
  tipo, len, default, decisión) + `Input#md-folder` + `Log#md-log`.
- Teclas dentro del pane: `space` alterna usar/omitir en la propiedad
  seleccionada, `enter` marca el tipo como revisado, `f` enfoca la carpeta.
  Cada cambio se guarda al JSON de inmediato (store atómico).
- Las operaciones de red corren en hilo y marshalizan a UI con
  `_apply_on_ui` (144); el botón queda deshabilitado mientras corre; el
  progreso `SyncProgress` se refleja en `#md-progress`.
- Sin credenciales CMIS: Descubrir/Comparar/Actualizar avisan en el log y no
  rompen; Verificar YAML y la revisión funcionan offline.

### REQ-007 — Docs y CHANGELOG

- `docs/how-to/cm-type-manifest.md` (nuevo): flujo descubrir → revisar →
  check → update; formato del JSON; qué es "usar"/"omitir".
- `docs/reference/config-schema.md`, `docs/reference/config-reference.yaml`,
  `docs/tutorials/01-the-yaml-config.md`, `docs/how-to/cmis-target-preflight.md`,
  `docs/how-to/as400-sync.md` (IDNBAC/TIPIDN ahora del manifest),
  `docs/reference/cli.md` (grupo `types`), `docs/explanation/operations-console.md`
  (pestaña M), `docs/how-to/developer/add-a-new-cmis-property.md`.
- `CHANGELOG.md` `[Unreleased]`: Added (manifest, `types`, pestaña M),
  Changed (MapeoRVI_CM a 3 columnas, clave por sistema), Deprecated
  (`metadatos_csv_path`).

## Escenarios

1. `types discover` contra PRD → 362 tipos con ID corto, 6 en
   `without_code`; `PT55` y `PT55.2` son entradas distintas.
2. `MapeoRVI_CM.csv` con `("", "0001") → DC01` y `("RVI2", "0001") → DC02`;
   trigger con sistema `rvi2` → DC02; trigger con sistema `X` → DC01.
3. Fila con `IDCM=ZZ99` inexistente → WARNING, `missing_cm_codes == ("ZZ99",)`,
   `types check` CRITICAL.
4. Manifest con `BAC_CIF` `usar` y YAML sin `field_sources.BAC_CIF` →
   `types check` CRITICAL; con la entrada presente → OK.
5. Servidor agrega `BAC_Nueva` (required, sin default) a DC01 → `diff`
   lo lista, `update` la agrega como `usar`, `reviewed=False`,
   `changes=["+ clbNonGroup.BAC_Nueva (required)"]`; la decisión previa de
   `BAC_CIF` se conserva.
6. Operador pone carpeta manual → `update` no la pisa aunque cambie
   `local_name`.
7. S3 con manifest: propiedades al wire con id completo
   (`clbNonGroup.BAC_CIF`), valores desde `field_sources`, NIARVILOG
   `TIPIDN=$t!-2_…v-1`.

## Notas de implementación

- `services/*` importa sólo `cmcourier.domain.*` + stdlib. El JSON store va
  en `adapters/`.
- Reusar `SyncProgress`/`ProgressEmitter` (144) para todo el progreso.
- Funciones ≤ 50 líneas. TDD: test rojo primero.
- El modo split y `_build_metadatos_index` NO se borran en este cambio
  (deprecado); se borran en un cambio posterior cuando el operador migre.
