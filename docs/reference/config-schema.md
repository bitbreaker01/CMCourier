> [← Volver al índice](../INDEX.md) · [Reference](README.md)

# Configuration schema

Schema completo del YAML que consumen los pipelines. Todos los modelos son Pydantic v2 `frozen=True, extra="forbid"` — claves desconocidas explotan al cargar. Defaults y rangos salen directo de `src/cmcourier/config/schema.py`.

Convenciones de los rangos:
- `≥ N` = `Field(ge=N)`
- `> N` = `Field(gt=N)`
- `A..B` = inclusive ambos extremos.

## Top-level `PipelineConfig`

| Field | Type | Default | Constraint | Description |
|-------|------|---------|------------|-------------|
| `connections` | `dict[str, ConnectionConfig]` | `{}` | alias `^[a-z][a-z0-9_]{0,31}$`, `cmis` reservado | Registro de conexiones por alias (129). |
| `trigger` | `TriggerConfigUnion` (required) | — | discriminated by `kind` | Strategy de S0. |
| `indexing` | `IndexingConfig` (required) | — | — | Config de S1 + source RVABREP. |
| `mapping` | `MappingConfig` (required) | — | — | Modelo Documental (S2). |
| `metadata` | `MetadataConfigModel` (required) | — | — | Resolución de propiedades (S3). |
| `identity` | `IdentityConfigModel` | factory | — | (147) Qué campo alimenta el shortname / CIF / sistema del cliente. Todos los slots opcionales; sin declarar nada, comportamiento pre-147. |
| `assembly` | `AssemblyConfig` (required) | — | — | Fuentes + temp dir para S4. |
| `cmis` | `CmisConfigModel` (required) | — | — | Conexión + retries de S5. |
| `tracking` | `TrackingConfig` (required) | — | — | SQLite + AS400 sync. |
| `observability` | `ObservabilityConfig` | factory | — | Logs + métricas. |
| `processing` | `ProcessingConfig` | factory | — | Modo + lanes + workers. |
| `batch_size` | int | `1000` | `≥ 1` | Tamaño del chunk lógico. |

---

## Connections (`connections`, 129)

Registro opcional de conexiones con **alias**. Cada sitio que hoy acepta una
conexión inline (`indexing.source.connection`,
`metadata.sources[].as400_connection`, `tracking.as400_sync.connection`)
acepta también un `str` con el alias de este registro. Un validador de
`PipelineConfig` verifica que el alias exista y que su `kind` sea el que el
sitio espera (una fuente `as400` no puede apuntar a una conexión `mssql`).

```yaml
connections:
  rvi:                       # alias → env vars RVI_USERNAME / RVI_PASSWORD
    kind: as400
    host: as400.example.com
  clientes_sql:              # alias → CLIENTES_SQL_USERNAME / CLIENTES_SQL_PASSWORD
    kind: mssql
    host: 127.0.0.1
    database: cmcourier
    trust_server_certificate: true

indexing:
  source:
    kind: as400
    connection: rvi          # referencia por alias
    query: "SELECT ..."
```

### `ConnectionConfig` (discriminated by `kind`)

| kind | Modelo | Notas |
|------|--------|-------|
| `as400` | [`As400ConnectionConfig`](#as400connectionconfig) | El mismo objeto que se usa inline. |
| `mssql` | [`MssqlConnectionConfig`](#mssqlconnectionconfig) | SQL Server vía ODBC (130). |

### `MssqlConnectionConfig`

| Field | Type | Default | Constraint | Description |
|-------|------|---------|------------|-------------|
| `kind` | Literal `"mssql"` | `"mssql"` | — | — |
| `host` | str (required) | — | — | Hostname / IP del SQL Server. |
| `port` | int | `1433` | `1..65535` | — |
| `database` | str (required) | — | — | Base a la que se conecta. |
| `driver` | str | `"ODBC Driver 18 for SQL Server"` | — | Nombre del driver ODBC (msodbcsql18). |
| `encrypt` | bool | `True` | — | `Encrypt=yes/no` en la connection string. |
| `trust_server_certificate` | bool | `False` | — | `TrustServerCertificate=yes` para certificados self-signed (entornos locales). |

### `ConnectionRef` — cómo se resuelve

`PipelineConfig.connection_refs()` devuelve UNA entrada por sitio que use
una conexión, ya resuelta (`alias`, `kind`, `spec`, `site`). Las conexiones
inline tienen alias implícito **`as400`** — por eso el YAML de siempre sigue
leyendo `AS400_USERNAME` / `AS400_PASSWORD` sin cambios.
`required_aliases()` deduplica los alias en orden y `connection_ref(site)`
devuelve la entrada de un sitio (`indexing`, `metadata:<alias de fuente>`,
`tracking.as400_sync`). Doctor, consola y `sync_ops` derivan de ahí.

Un `tracking.as400_sync` con `enabled: false` **no** cuenta aunque tenga
`connection` seteada: el pipeline nunca abre esa conexión, así que ni el
doctor la prueba ni la consola pide sus credenciales. El alias sí se valida
igual al cargar (un alias inexistente falla aunque el sync esté apagado).

---

## Trigger (`trigger`)

Unión discriminada por `kind`. Pickeá EXACTAMENTE uno.

### `CsvTriggerConfig` — `kind: csv`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `kind` | Literal `"csv"` | `"csv"` | — |
| `csv_path` | `FilePath` (required) | — | CSV de triggers. Debe existir. |
| `shortname_column` | str | `"ShortName"` | — |
| `cif_column` | str | `"CIF"` | — |
| `system_id_column` | str | `"SystemID"` | — |

### `RvabrepTriggerConfig` — `kind: rvabrep`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `kind` | Literal `"rvabrep"` (required) | — | — |
| `filters` | `RvabrepFiltersModel` | factory | Filtros opcionales. |

### `RvabrepFiltersModel`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `systems` | `list[str]` | `[]` | Filtra por `ABAACD`. 148: es el **único** filtro que llega al SQL, y ese camino lee en stream (`stream_by_fields_in`, chunks del `IN` de 1000) porque un sistema entero no entra en una lista de Python. |
| `document_types` | `list[str]` | `[]` | Códigos RVI (`ABAHCD`) a **migrar**. 148: no toca el SQL — el resto vuelve igual del origen y se reporta como `EXCLUDED_BY_FILTER` en el censo del batch (ver [`../how-to/operator/read-the-batch-census.md`](../how-to/operator/read-the-batch-census.md)). |

### `LocalScanTriggerConfig` — `kind: local_scan`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `kind` | Literal `"local_scan"` (required) | — | — |
| `scan_path` | `DirectoryPath` (required) | — | Carpeta a escanear. |
| `recursive` | bool | `False` | `True` → desciende por todos los subdirectorios de `scan_path` (088). |

### `SingleDocTriggerConfig` — `kind: single_doc`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `kind` | Literal `"single_doc"` (required) | — | — |

Sin campos extra — los parámetros (`shortname`, `system`, `cif`) vienen del CLI.

---

## Indexing (`indexing`)

### `IndexingConfig`

| Field | Type | Default | Constraint | Description |
|-------|------|---------|------------|-------------|
| `source` | `RvabrepSourceUnion` (required) | — | discriminated by `kind` | CSV o AS400. |
| `columns` | `IndexingColumnsModel` | factory | — | Mapeo de columnas RVABREP. |
| `batch_size` | int | `50` | `≥ 1` | Tamaño del fetch de RVABREP. |

### `CsvRvabrepSource` — `source.kind: csv`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `kind` | Literal `"csv"` | `"csv"` | — |
| `csv_path` | `FilePath` (required) | — | Tabla RVABREP simulada como archivo CSV. |

### `As400RvabrepSource` — `source.kind: as400`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `kind` | Literal `"as400"` (required) | — | — |
| `connection` | `As400ConnectionConfig \| str` (required) | — | Conexión ODBC inline o alias de `connections` (129). |
| `query` | str (required) | — | SQL que devuelve columnas con shape RVABREP. |

### `As400ConnectionConfig`

| Field | Type | Default | Constraint | Description |
|-------|------|---------|------------|-------------|
| `host` | str (required) | — | — | Hostname AS400. |
| `port` | int | `446` | `1..65535` | Puerto ODBC. |
| `database` | str | `"RVILIB"` | — | Library default. |
| `driver` | str | `"iSeries Access ODBC Driver"` | — | Driver ODBC. |
| `table` | `str \| None` | `None` | — | Tabla por default para mock generators. |

### `IndexingColumnsModel`

Mapeo lógico → físico para RVABREP. Todos `str`. Defaults coinciden con la nomenclatura canónica del banco.

| Field | Default |
|-------|---------|
| `shortname_column` | `"ABABCD"` |
| `system_id_column` | `"ABAACD"` |
| `delete_code_column` | `"ABACST"` |
| `txn_num_column` | `"ABAANB"` |
| `index2_column` | `"ABACCD"` |
| `index3_column` | `"ABADCD"` |
| `index4_column` | `"ABAECD"` |
| `index5_column` | `"ABAFCD"` |
| `index6_column` | `"ABAGCD"` |
| `index7_column` | `"ABAHCD"` |
| `image_type_column` | `"ABABST"` |
| `image_path_column` | `"ABAICD"` |
| `file_name_column` | `"ABAJCD"` |
| `creation_date_column` | `"ABAADT"` |
| `last_view_date_column` | `"ABABDT"` |
| `total_pages_column` | `"ABABUN"` |

---

## Mapping (`mapping`)

Tres modos mutuamente excluyentes. Validator `_exactly_one_mode` lo hace explotar si se mezclan.

- **Consolidado** (`csv_path`): un único CSV, formato legacy usado en fixtures de test.
- **Split** (`rvi_cm_csv_path` + `metadatos_csv_path`, 035): **DEPRECADO por 145**. Sigue funcionando, pero el modo recomendado para instalaciones nuevas es manifest.
- **Manifest** (`rvi_cm_csv_path` + `type_manifest_path`, 145, el modo nuevo): `MapeoRVI_CM.csv` reducido a `IDSistema,IDRVI,IDCM` más el manifest JSON de tipos CM que bajó `cmcourier types discover`. Todo lo que el servidor ya sabe (tipo, carpeta, propiedades) sale del manifest — nadie mantiene `MetadatosCM.csv` a mano. Ver la guía completa: [`how-to/cm-type-manifest.md`](../how-to/cm-type-manifest.md).

### `MappingConfig`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `csv_path` | `FilePath \| None` | `None` | Modo consolidado (legacy / fixtures). |
| `rvi_cm_csv_path` | `FilePath \| None` | `None` | Modos split y manifest — `MapeoRVI_CM.csv`. |
| `metadatos_csv_path` | `FilePath \| None` | `None` | Modo split (**deprecado**, 145) — `MetadatosCM.csv`. |
| `type_manifest_path` | `FilePath \| None` | `None` | Modo manifest (145) — JSON de `cmcourier types discover`. |
| `id_rvi_column` | str | `"ID RVI"` | Consolidado. |
| `clase_id_column` | str | `"ID CLASE DOCUMENTAL"` | Consolidado. |
| `id_corto_column` | str | `"ID Corto"` | Consolidado. |
| `clase_name_column` | str | `"CLASE DOCUMENTAL"` | Consolidado. |
| `metadata_list_column` | str | `"METADATOS"` | Consolidado. |
| `cmis_type_column` | str | `"CMISType"` | Consolidado. |
| `rvi_cm_id_rvi_column` | str | `"IDRVI"` | Split y manifest — MapeoRVI_CM. |
| `rvi_cm_id_cm_column` | str | `"IDCM"` | Split y manifest — MapeoRVI_CM. |
| `rvi_cm_id_sistema_column` | str | `"IDSistema"` | Manifest (145). Sistema de origen del código RVI. **Opcional** en modo manifest: columna ausente ≡ todas las filas al comodín (`""`). No aplica al modo split. |
| `rvi_cm_clase_id_column` | str | `"IDClaseDocumental"` | Split (deprecado) — MapeoRVI_CM. No se lee en modo manifest. |
| `rvi_cm_cmis_type_column` | str | `"CMISType"` | Split (deprecado) — MapeoRVI_CM. No se lee en modo manifest (el tipo sale del manifest). |
| `rvi_cm_cmis_folder_column` | str | `"CMISFolder"` | Split (deprecado) — MapeoRVI_CM. No se lee en modo manifest (la carpeta sale del manifest). |
| `metadatos_id_corto_column` | str | `"IDCorto"` | Split (deprecado) — MetadatosCM. |
| `metadatos_metadata_column` | str | `"Metadato"` | Split (deprecado) — MetadatosCM. |
| `metadatos_required_column` | str | `"Requerido"` | Split (deprecado) — MetadatosCM. |
| `metadatos_cmis_property_id_column` | str | `"CMISPropertyId"` | Split (deprecado) — MetadatosCM. |
| `required_marker` | str | `"Yes"` | Valor que marca campo obligatorio (consolidado y split). |

Reglas (validator `_exactly_one_mode`):
- Consolidado: setear `csv_path`, dejar los demás en `None`.
- Split (**deprecado**): setear `rvi_cm_csv_path` Y `metadatos_csv_path`, dejar `csv_path` y `type_manifest_path` en `None`.
- Manifest (145, recomendado): setear `rvi_cm_csv_path` Y `type_manifest_path`, dejar `csv_path` y `metadatos_csv_path` en `None`.
- `rvi_cm_csv_path` exige EXACTAMENTE uno de `metadatos_csv_path` / `type_manifest_path` — nunca los dos, nunca ninguno.

En modo manifest, las columnas requeridas de `MapeoRVI_CM.csv` son sólo `IDRVI` e `IDCM` (`rvi_cm_id_rvi_column` / `rvi_cm_id_cm_column`); `IDSistema` es opcional. La clave del mapeo pasa a ser `(sistema, IDRVI)` — ver [`MappingService.get_mapping`](../how-to/cm-type-manifest.md) — mientras que los modos consolidado y split siguen indexando sólo por `IDRVI` (todo bajo el comodín).

---

## Metadata (`metadata`)

### `MetadataConfigModel`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `field_aliases` | `dict[str, str]` | `{}` | Lógico → físico. |
| `field_sources` | `dict[str, FieldConfig]` (required) | — | Resolver por campo. |
| `sources` | `list[MetadataSourceConfig]` | `[]` | CSV/AS400 con nombre. |
| `prefetch_enabled` | bool | `True` | Carga eager en memoria. |
| `cache` | `MetadataCacheConfig` | factory | Cache cross-batch (037). |

### `FieldConfig`

| Field | Type | Default | Constraint | Description |
|-------|------|---------|------------|-------------|
| `sources` | `list[FieldSourceItem]` (required) | — | `min_length=1` | Cadena de fallback. |
| `default_value` | `str \| None` | `None` | — | Si todas las sources fallan. Se FORMATEA y **nunca se valida** (149). Sin él, toda la cadena fallando es `SourceFailedError`. |
| `format` | `ValueFormatModel \| None` | `None` | — | (146) Formato POR CAMPO: corre sobre el valor ganador **y sobre `default_value`**, justo antes del wire. |

### `FieldSourceItem`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `source_type` | str (required) | — | `"trigger"`, `"rvabrep"`, `"csv:{alias}"`, `"as400:{alias}"` o `"mssql:{alias}"` (130). El prefijo se parsea con `split_lookup_source_type()`; `LOOKUP_SOURCE_KINDS = ("csv", "as400", "mssql")`. |
| `lookup_value_column` | str (required) | — | Columna a leer. |
| `lookup_key_column` | `str \| None` | `None` | Columna pivot. |
| `lookup_value_source` | str | `"trigger.cif"` | (084/147) De dónde sale el VALOR que se busca. Tres scopes: `trigger.<attr>`, `rvabrep.<col>` y `field.<NOMBRE_CANONICO>` (147). Sólo aplica a fuentes de lookup (`csv:` / `as400:` / `mssql:`). |
| `validation` | `ValidationModel \| None` | `None` | — |
| `format` | `ValueFormatModel \| None` | `None` | (146) Formato POR FUENTE: corre **entre el fetch y `validation`**. |

#### `lookup_value_source: "field.<NOMBRE>"` (147)

El tercer scope. La clave de búsqueda deja de salir del trigger o de la fila
RVABREP y pasa a ser **el valor YA RESUELTO de otro campo** — que es lo que
permite encadenar saltos (afiliado hijo → padre → shortname → CIF → nombre):

```yaml
BAC_Shortname:
  sources:
    - source_type: trigger                  # 1º: si ya vino, es gratis
      lookup_value_column: shortname
    - source_type: "as400:clientes"         # 2º: si no, se busca
      lookup_value_source: "field.BAC_Afiliado_Padre"
      lookup_key_column:   CUSAFI
      lookup_value_column: CUSSHN
```

Reglas:

- El resolver arma el **grafo de dependencias** entre campos, lo ordena
  topológicamente y resuelve en ese orden. **El orden de declaración en el
  YAML no importa**.
- Sólo se resuelven los campos pedidos **más sus dependencias transitivas**.
  Un campo que nadie pide ni nadie depende de él ni se toca.
- **Los ciclos y las referencias a campos inexistentes fallan al CARGAR el
  YAML**, con el ciclo completo en el mensaje (`A -> B -> C -> A`). Nunca en
  runtime, nunca a mitad de un batch de producción.
- Una dependencia que **no resolvió** deja su dependiente sin esa fuente: se
  saltea SÓLO esa fuente (igual que un valor vacío) y la cadena sigue con la
  próxima. No aborta el documento.
- Cada salto se memoiza por corrida (REQ-005), con clave
  `(campo, source_type, columna clave, columna valor, valor de clave)`.

### `ValidationModel`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `allowed_pattern` | `str \| None` | `None` | Regex que el valor debe matchear. |

### `ValueFormatModel` (146)

El mismo modelo en dos ubicaciones — `FieldSourceItem.format` (por fuente, antes de validar) y `FieldConfig.format` (por campo, antes del wire) —, las dos opcionales e independientes. Ausente en las dos ⇒ comportamiento byte-idéntico al pre-146. Un `format:` sin ninguna clave seteada es válido y es un no-op.

Las transformaciones se aplican **siempre en este orden fijo**. No es una lista de pasos configurable: un orden libre vuelve el YAML imposible de leer y de auditar.

| # | Field | Type | Default | Constraint | Qué hace |
|---|-------|------|---------|------------|----------|
| 1 | `trim` | bool | `False` | — | `value.strip()`. |
| 2 | `case` | `Literal["upper","lower"] \| None` | `None` | — | `upper()` / `lower()`. |
| 3 | `strip_leading_zeros` | bool | `False` | — | Saca los ceros de la izquierda. |
| 4 | `pad_left` | `PadLeftModel \| None` | `None` | — | Rellena a la izquierda hasta `width`. |
| 5 | `pad_right` | `PadRightModel \| None` | `None` | — | Rellena a la derecha hasta `width`. |
| 6 | `truncate` | `int \| None` | `None` | `> 0` | Se queda con los PRIMEROS `N` caracteres. |

Reglas de borde:

- **`strip_leading_zeros` nunca devuelve vacío**: `"00000"` → `"0"`. Un vacío significa "esta fuente no dio" y borraría un valor legítimo.
- **`pad_*` nunca recorta**: un valor ya más largo que `width` vuelve intacto. Para recortar está `truncate`, que es explícito.
- **El resultado vacío corta la fuente**: si después de formatear el valor queda `""` (el `CHAR(n)` de AS400 todo espacios con `trim: true`), la fuente se trata como "no dio" y se pasa a la siguiente.
- **El `default_value` se formatea y NUNCA se valida** (149). El `format` por campo se le aplica igual que al valor ganador —un `default_value: "0"` con `format: {pad_left: {width: 9}}` llega como `"000000000"`—, pero no se lo juzga contra ningún patrón. Pre-149 se validaba contra el `allowed_pattern` de la PRIMERA fuente: magia que el YAML no declaraba, acoplamiento POSICIONAL (reordenar las fuentes cambiaba en silencio contra qué se validaba) y documentos muertos en producción por un error de config. La red de seguridad está en `types check`, que emite un **INFO** cuando el default ya formateado no matchea ningún `allowed_pattern` de sus fuentes. Ver [`../how-to/metadata-format.md`](../how-to/metadata-format.md).
- El `format` por campo corre DESPUÉS de la validación de la fuente: si rompe el patrón, es decisión del operador y `types check` lo avisa (nunca el resolver).
- **Validador de schema** (`_truncate_not_below_pad`): `truncate` menor que `pad_left.width` o que `pad_right.width` falla al CARGAR el YAML — rellenar para después cortar no tiene lectura sensata.

`types check` cruza este bloque con el manifest: el largo que declara el `format` (`truncate`, si no el mayor de los `pad_*`) contra el `max_length` de CM — y ese largo REEMPLAZA al deducido del `allowed_pattern`, nunca los dos warnings para la misma propiedad. Un `case: upper` contra `choices` donde ninguna opción está en mayúsculas también avisa. Y (149) un `default_value` ya formateado que no matchea ningún `allowed_pattern` de sus fuentes sale como INFO —una vez por campo, liste los patrones en orden de config— porque un default deliberadamente distinto de los datos reales es una técnica legítima: el check informa, no juzga.

#### `PadLeftModel` / `PadRightModel`

| Field | Type | Default | Constraint | Description |
|-------|------|---------|------------|-------------|
| `width` | int (required) | — | `> 0` | Largo objetivo. |
| `char` | str | `"0"` (left) / `" "` (right) | exactamente 1 carácter | Relleno. Los defaults son los casos reales: ceros a la izquierda en cuentas, espacios a la derecha en `CHAR(n)` de DB2. |

### `MetadataSourceConfig` (discriminated by `kind`)

#### `CsvMetadataSourceConfig` — `kind: csv`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `kind` | Literal `"csv"` | `"csv"` | — |
| `alias` | str (required) | — | Nombre con el que se referencia desde `source_type: csv:{alias}`. |
| `csv_path` | `FilePath` (required) | — | — |

#### `As400MetadataSourceConfig` — `kind: as400`

| Field | Type | Default | Constraint | Description |
|-------|------|---------|------------|-------------|
| `kind` | Literal `"as400"` (required) | — | — | — |
| `alias` | str (required) | — | — | — |
| `as400_connection` | `As400ConnectionConfig \| str` (required) | — | — | Inline o alias de `connections` (129). |
| `table` | `str \| None` | `None` | 1–3 identificadores DB2 separados por punto | Modo table (`CLIENTES`, `RVILIB.CLIENTES`). Se interpola crudo en el `SELECT`, por eso se valida (049). |
| `query` | `str \| None` | `None` | `min_length=1` | Modo query. |

Exactamente uno de `table` / `query` (validator `_exactly_one_table_or_query`).

#### `MssqlMetadataSourceConfig` — `kind: mssql` (130)

| Field | Type | Default | Constraint | Description |
|-------|------|---------|------------|-------------|
| `kind` | Literal `"mssql"` (required) | — | — | — |
| `alias` | str (required) | — | — | Nombre usado en `source_type: mssql:{alias}`. |
| `connection` | str (required) | — | — | Alias de `connections` con `kind: mssql`. NO admite forma inline: SQL Server sólo existe a través del registro (129). |
| `table` | `str \| None` | `None` | 1–3 identificadores separados por punto | Modo table (p. ej. `dbo.clientes`, `db.dbo.clientes`). Se interpola crudo en el `SELECT`, por eso se valida (049). |
| `query` | `str \| None` | `None` | `min_length=1` | Modo query. |

Exactamente uno de `table` / `query`. El prefijo de `source_type` debe coincidir con el `kind` de la fuente declarada: `mssql:clientes` sobre una fuente `csv` falla al cargar (`field_sources[...]: source_type 'mssql:clientes' but metadata.sources[clientes] is kind 'csv'`). Si `connection` apunta a una conexión de otro kind, `load_config` falla nombrando `metadata.sources[{alias}].connection` y "requires kind 'mssql'". En runtime el registro de fuentes recibe un `MssqlDataSource` (subclase de `OdbcDataSource`, misma base que AS400) bajo `alias`.

### `MetadataCacheConfig`

| Field | Type | Default | Constraint | Description |
|-------|------|---------|------------|-------------|
| `enabled` | bool | `False` | — | Activa cache cross-batch en `document_cache`. |
| `ttl_minutes` | int | `60` | `1..43200` | Tope 30 días. |

---

## Identity (`identity`, 147)

Quién es el cliente de ESTE documento. Reemplaza al hardcode de `"BAC_CIF"`
que vivía adentro del resolver de metadata: ahora el YAML declara qué campo
alimenta cada slot, y ese campo puede llegar por una cadena de tres saltos
(`field.<NOMBRE>`, arriba).

Lo que resuelve este bloque es **un solo origen para las tres escrituras**
que antes podían discrepar: la fila de `migration_log`, las columnas
`CTECIF`/`CTENUM` del log de AS400 (`RVIMGLOG`/`NIARVILOG`) y la clave del
mapeo de S2. Ver [`how-to/identity-chain.md`](../how-to/identity-chain.md).

```yaml
identity:
  shortname:
    field: BAC_Shortname
    on_missing: fail
  cif:
    field: BAC_CIF
    on_missing: fail
    max_digits: 9          # la precisión REAL de CTENUM
  system_id:
    field: BAC_Sistema
    on_missing: warn
```

### `IdentityConfigModel`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `shortname` | `IdentitySlotModel \| None` | `None` | Alimenta `CTECIF` y `migration_log.trigger_shortname`. |
| `cif` | `IdentitySlotModel \| None` | `None` | Alimenta `CTENUM` y `migration_log.trigger_cif`. |
| `system_id` | `IdentitySlotModel \| None` | `None` | Alimenta la clave `(sistema, IDRVI)` del mapeo y `migration_log.trigger_system_id`. |

**Los tres slots son opcionales.** Un slot ausente ⇒ comportamiento
pre-147: el valor se lee de `trigger.audit_row()` sin cadena, y la clave del
mapeo sigue saliendo de `trigger_system_id(trigger)`.

Validadores de schema (fallan al CARGAR, no en runtime):

- `identity.<slot>.field` tiene que ser una clave de `metadata.field_sources`.
- `max_digits` **sólo** es válido en `cif` — modela la precisión real de
  `CTENUM`, y no tiene lectura sobre el shortname ni sobre el sistema.

### `IdentitySlotModel`

| Field | Type | Default | Constraint | Description |
|-------|------|---------|------------|-------------|
| `field` | str (required) | — | clave de `metadata.field_sources` | Qué campo canónico resuelve este pedazo de identidad. |
| `on_missing` | `Literal["fail","warn","default"]` | `"fail"` | — | Qué hacer cuando la cadena no dio nada. |
| `max_digits` | `int \| None` | `None` | `> 0`, sólo en `cif` | Largo máximo aceptable. Compara el LARGO — no convierte nada. |
| `default_value` | `str \| None` | `None` | required si `on_missing: default` | El valor de fallback. |

`on_missing` en detalle:

| Valor | Qué pasa |
|-------|----------|
| `fail` (default) | El documento falla como **`S2_FAILED`** con un error que nombra el slot **y la cadena completa que se intentó**, fuente por fuente. No hay estado nuevo: `IdentityResolutionError` desciende de `MappingError`. |
| `warn` | Se loguea un WARNING con la cadena, el slot queda en `""` y el documento sigue. |
| `default` | Se usa `default_value`. |

`max_digits` es lo que mata al viejo `int(cif) if cif.isdigit() else 0`:

- Validaba el **tipo** y nunca la **magnitud**. Un CIF más largo que la
  precisión de `CTENUM` rompía del lado del banco con `22003` (1194 filas en
  una corrida real del operador).
- Un CIF **no numérico** se escribía como **cliente `0`** en el log del banco,
  en silencio. Post-147 el valor nunca se convierte ni se reemplaza solo: o
  pasa tal cual, o se aplica `on_missing`. Cuando aun así no hay número
  válido para `CTENUM`, va **`NULL`** — nunca un `0` inventado. Un operador
  que quiera el `0` lo declara con `on_missing: default` + `default_value: "0"`.

El check `as400_column_widths` del `doctor` (147 REQ-006) cruza `max_digits`
contra la precisión REAL de la columna antes de que arranque el batch.

---

## Assembly (`assembly`)

### `AssemblyConfig`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `source_root` | `DirectoryPath` (required) | — | Raíz del archivo de imágenes. Debe existir. |
| `temp_dir` | `Path` (required) | — | Directorio temporal. Se crea en runtime. |
| `image_type_map` | `dict[str, str]` | `{"B": "image/tiff", "O": "application/pdf", "C": "image/jpeg"}` | Códigos de tipo de imagen → MIME. |
| `keep_staged_files` | bool | `False` | `True` preserva el PDF ensamblado en `temp_dir` tras un S5_DONE — debug/inspección (085). |

---

## CMIS (`cmis`)

### `CmisConfigModel`

| Field | Type | Default | Constraint | Description |
|-------|------|---------|------------|-------------|
| `base_url` | str (required) | — | — | URL del Browser Binding. |
| `repo_id` | str (required) | — | — | Repository ID. |
| `timeout_seconds` | float | `300.0` | `> 0` | Timeout por request. |
| `verify_ssl` | bool | `False` | — | TLS verification. |
| `max_bandwidth_mbps` | float | `0.0` | `≥ 0` | `0` = unlimited. |
| `retry_max_attempts` | int | `3` | `≥ 1` | Reintentos por upload. |
| `retry_base_delay_s` | float | `2.0` | `≥ 0` | Backoff base. |
| `workers` | int | `4` | `≥ 1` | Tamaño base del pool S5 (AIMD-resizable). |
| `http2` | bool | `True` | — | `False` fuerza HTTP/1.1 (089). |
| `upload_chunk_bytes` | int | `1048576` | `4096..67108864` | Chunk de lectura del multipart encoder; 1 MiB default (090). |
| `auto_tune` | `AutoTuneConfig` | factory | — | AIMD. |

### `AutoTuneConfig` (recalibrado en 068)

| Field | Type | Default | Constraint | Description |
|-------|------|---------|------------|-------------|
| `enabled` | bool | `False` | — | Activa el controlador AIMD. |
| `min_threads` | int | `2` | `≥ 1` | Piso del pool. |
| `max_threads` | int | `50` | `≥ 1` | Techo del pool. |
| `target_p95_ms` | float | `5000.0` | `> 0` | p95 objetivo para S5. |
| `adjustment_interval_s` | int | `30` | `≥ 1` | Tick del controlador. |
| `warmup_seconds` | int | `60` | `≥ 0` | No actuar antes de N s. |
| `min_samples` | int | `20` | `≥ 1` | No actuar con menos muestras. |
| `timeout_auto_adjust` | bool | `True` | — | También ajustar `timeout_seconds`. |
| `min_timeout_s` | int | `30` | `≥ 1` | Piso del timeout dinámico. |
| `max_timeout_s` | int | `600` | `≥ 1` | Techo del timeout dinámico. |
| `growth_factor` | float | `1.25` | `1.0..4.0` | Multiplicador en growth. |
| `halve_factor` | float | `0.75` | `0.05..1.0` | Multiplicador en halve. |
| `halve_threshold_ratio` | float | `1.5` | `1.05..10.0` | Múltiplo de `target_p95_ms` que dispara halve. |

Validators:
- `min_threads <= max_threads`.
- `min_timeout_s <= max_timeout_s`.

---

## Tracking (`tracking`)

### `TrackingConfig`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `db_path` | `Path` (required) | — | SQLite (WAL mode). Se crea si no existe. |
| `as400_sync` | `As400SyncConfig` | factory | Sync distribuido con NIARVILOG. |

### `As400SyncConfig`

| Field | Type | Default | Constraint | Description |
|-------|------|---------|------------|-------------|
| `enabled` | bool | `False` | — | Activa sync (034). |
| `connection` | `As400ConnectionConfig \| str \| None` | `None` | required if `enabled=true` | Inline o alias de `connections` (129). Validator lo verifica. |
| `library` | str | `"RVILIB"` | DB2 identifier | Library de NIARVILOG. |
| `table` | str | `"NIARVILOG"` | DB2 identifier | — |
| `columns` | `NiarvilogColumnsModel` | factory | — | Mapeo lógico → físico. |
| `stale_in_progress_minutes` | int | `30` | `1..1440` | Threshold para reclamar filas stale. |
| `retry_attempts` | int | `3` | `1..10` | — |
| `retry_base_delay_s` | float | `5.0` | `> 0` | — |
| `mode` | `Literal["claim", "periodic"]` | `"claim"` | — | `claim` = sync atómico por-doc (034); `periodic` = reconciliador de fondo (096). |
| `periodic` | `PeriodicSyncConfig \| None` | `None` | required if `mode="periodic"` | Validator lo verifica. |

Validator: `mode="periodic"` exige el bloque `periodic`.

### `PeriodicSyncConfig` (096)

| Field | Type | Default | Constraint | Description |
|-------|------|---------|------------|-------------|
| `interval_minutes` | int | `5` | `1..1440` | Cada cuánto corre el reconciliador de fondo. |

### `NiarvilogColumnsModel`

Todas las columnas se interpolan en SQL (un nombre de columna nunca puede ser bind-param) — cada valor se valida como identificador DB2.

| Field | Default |
|-------|---------|
| `system_id_column` | `"SISCOD"` |
| `txn_num_column` | `"TRNNUM"` |
| `doc_format_column` | `"DOCFRM"` |
| `image_archive_column` | `"IMGARC"` |
| `image_type_column` | `"IMGTIP"` |
| `client_cif_column` | `"CTECIF"` |
| `client_num_column` | `"CTENUM"` |
| `status_column` | `"STSCOD"` |
| `idcm_column` | `"IDNBAC"` |
| `cm_type_column` | `"TIPIDN"` |
| `cm_object_id_column` | `"OBJIDN"` |
| `retry_count_column` | `"NUMREI"` |
| `started_at_column` | `"PMRREI"` |
| `finished_at_column` | `"FINREI"` |
| `error_message_column` | `"EERRMSG"` |

---

## Processing (`processing`)

### `ProcessingConfig`

| Field | Type | Default | Constraint | Description |
|-------|------|---------|------------|-------------|
| `mode` | `Literal["batched", "streaming"]` | `"batched"` | — | Orquestador. |
| `streaming` | `StreamingConfig` | factory | — | Knobs del modo streaming. |
| `batches_in_flight` | int | `2` | `1..2` | Solape multi-batch (ignorado en streaming). |
| `prep_workers` | int | `1` | `≥ 1` | Pool S2/S3/S4 (056). |
| `heavy_light_lanes` | `HeavyLightLanesConfig` | factory | — | Lanes (036). |
| `s4_use_processes` | bool | `True` | — | `ProcessPoolExecutor` para S4 (066). |
| `s4_max_processes` | `int \| None` | `None` | `≥ 1` | `None` → `os.cpu_count()`. |
| `s4_smart_routing` | bool | `True` | — | Con el pool activo, rutea PDFs nativos inline y paginados al pool (094; default on desde 114). |

### `StreamingConfig` (063)

| Field | Type | Default | Constraint | Description |
|-------|------|---------|------------|-------------|
| `bucket_size` | int | `100` | `≥ 1` | Capacidad del bounded queue entre prep y upload. |

### `HeavyLightLanesConfig` (036)

| Field | Type | Default | Constraint | Description |
|-------|------|---------|------------|-------------|
| `enabled` | bool | `False` | — | — |
| `heavy_threshold_bytes` | int | `10485760` (10 MiB) | `> 0` | Threshold para mandar a heavy lane. |
| `heavy_lane_min_batch` | int | `50` | `≥ 1` | Mínimo de docs para activar lanes. |
| `heavy_initial_ratio` | float | `0.2` | `0.0..1.0` | Fracción inicial del budget para heavy. |
| `rebalance_interval_s` | float | `10.0` | `0.0 < x ≤ 600.0` | Tick del daemon de rebalance. |
| `idle_threshold_s` | float | `15.0` | `0.0 < x ≤ 3600.0` | Tiempo vacío antes de migrar 1 worker. |

---

## Observability (`observability`)

### `ObservabilityConfig`

| Field | Type | Default | Constraint | Description |
|-------|------|---------|------------|-------------|
| `enabled` | bool | `True` | — | Master toggle. |
| `pipeline_metrics` | bool | `True` | — | Tier 2: `batch_summary` JSON. |
| `network_metrics` | bool | `True` | — | Tier 3: `NetworkEvent`. |
| `system_metrics` | `SystemMetricsConfig` | factory | — | Tier 5 (psutil). |
| `log_dir` | `Path` | `Path("./logs")` | — | Destino de los JSONL. |
| `log_format` | `Literal["json", "text"]` | `"json"` | — | Formato de log. |
| `rotation_mb` | int | `100` | `≥ 1` | Tamaño de rotación. |
| `retention_days` | int | `30` | `≥ 1` | Retención. |
| `slow_op_threshold_ms` | int | `5000` | `≥ 0` | Umbral para slow-ops (tier 4). |
| `slow_op_top_n` | int | `20` | `≥ 1` | Cuántos slow ops emitir por batch. |
| `unmask_pii` | bool | `False` | — | Si `true`, eventos de upload llevan valores crudos. Doctor emite WARNING al arrancar. |

Coerción legacy: `system_metrics: false` (bool en YAML pre-026) se promueve a `{enabled: false}`.

### `SystemMetricsConfig`

| Field | Type | Default | Constraint | Description |
|-------|------|---------|------------|-------------|
| `enabled` | bool | `True` | — | Daemon de sampling psutil. |
| `sample_interval_s` | float | `5.0` | `1.0..60.0` | Período de sampling. |

---

## Secrets (env vars, `config/loader.py:Secrets`)

Las credenciales NUNCA viven en el YAML. Desde 129 `Secrets` es un mapa
`alias → Credential(username, password)` y el esquema de env vars es
uniforme: la credencial del alias `X` se lee de **`X_USERNAME` /
`X_PASSWORD`** con el alias en MAYÚSCULAS.

| Alias | Env vars | Obligatoria | Description |
|-------|----------|-------------|-------------|
| `cmis` | `CMIS_USERNAME` / `CMIS_PASSWORD` | sí (`load_secrets` falla si faltan) | Usuario Browser Binding del destino. |
| `as400` (implícito) | `AS400_USERNAME` / `AS400_PASSWORD` | cuando algún sitio usa una conexión inline | Alias implícito de toda conexión escrita inline. |
| `<alias>` del registro | `<ALIAS>_USERNAME` / `<ALIAS>_PASSWORD` | cuando algún sitio lo referencia | Ej. `clientes_sql` → `CLIENTES_SQL_USERNAME` / `CLIENTES_SQL_PASSWORD`. |

`load_secrets(config)` lee `cmis` más cada alias de
`config.required_aliases()`. Las credenciales de conexión son opcionales al
cargar: la ausencia se detecta al construir el pipeline, con
`secrets.require(alias)`, que levanta `ConfigurationError` con
`missing_vars=[<ALIAS>_USERNAME, <ALIAS>_PASSWORD]` y el `site` que la
necesitaba. `secrets.get(alias)` devuelve `None` si falta cualquiera de las
dos mitades (un usuario sin password NO cuenta como credencial).

---

## Ver también

- [`cli.md`](cli.md) — flags que sobrescriben campos del YAML (`--batch-size`, `--batches-in-flight`).
- [`error-codes.md`](error-codes.md) — `ConfigurationError` es lo que te tira el loader.
- [Explanation: architecture overview](../explanation/architecture-overview.md) — el por qué detrás de los defaults de `AutoTuneConfig`.
- [How-to: heavy/light lanes](../how-to/heavy-light-lanes.md) — tunear `HeavyLightLanesConfig`.
- [How-to: document cache](../how-to/document-cache.md) — habilitar `metadata.cache`.
- [How-to: manifest de tipos CM](../how-to/cm-type-manifest.md) — modo `mapping` recomendado (145), reemplaza `metadatos_csv_path`.
