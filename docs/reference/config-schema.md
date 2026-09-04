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
| `trigger` | `TriggerConfigUnion` (required) | — | discriminated by `kind` | Strategy de S0. |
| `indexing` | `IndexingConfig` (required) | — | — | Config de S1 + source RVABREP. |
| `mapping` | `MappingConfig` (required) | — | — | Modelo Documental (S2). |
| `metadata` | `MetadataConfigModel` (required) | — | — | Resolución de propiedades (S3). |
| `assembly` | `AssemblyConfig` (required) | — | — | Fuentes + temp dir para S4. |
| `cmis` | `CmisConfigModel` (required) | — | — | Conexión + retries de S5. |
| `tracking` | `TrackingConfig` (required) | — | — | SQLite + AS400 sync. |
| `observability` | `ObservabilityConfig` | factory | — | Logs + métricas. |
| `processing` | `ProcessingConfig` | factory | — | Modo + lanes + workers. |
| `batch_size` | int | `1000` | `≥ 1` | Tamaño del chunk lógico. |

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
| `systems` | `list[str]` | `[]` | Filtra por `ABAACD`. |
| `document_types` | `list[str]` | `[]` | Filtra por id_rvi (`ABAHCD`). |

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
| `connection` | `As400ConnectionConfig` (required) | — | Conexión ODBC. |
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

Dos modos mutuamente excluyentes. Validator `_exactly_one_mode` lo hace explotar si se mezclan.

### `MappingConfig`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `csv_path` | `FilePath \| None` | `None` | Modo consolidado (legacy / fixtures). |
| `rvi_cm_csv_path` | `FilePath \| None` | `None` | Modo split — `MapeoRVI_CM.csv`. |
| `metadatos_csv_path` | `FilePath \| None` | `None` | Modo split — `MetadatosCM.csv`. |
| `id_rvi_column` | str | `"ID RVI"` | Consolidado. |
| `clase_id_column` | str | `"ID CLASE DOCUMENTAL"` | Consolidado. |
| `id_corto_column` | str | `"ID Corto"` | Consolidado. |
| `clase_name_column` | str | `"CLASE DOCUMENTAL"` | Consolidado. |
| `metadata_list_column` | str | `"METADATOS"` | Consolidado. |
| `cmis_type_column` | str | `"CMISType"` | Consolidado. |
| `rvi_cm_id_rvi_column` | str | `"IDRVI"` | Split — MapeoRVI_CM. |
| `rvi_cm_id_cm_column` | str | `"IDCM"` | Split — MapeoRVI_CM. |
| `rvi_cm_clase_id_column` | str | `"IDClaseDocumental"` | Split — MapeoRVI_CM. |
| `rvi_cm_cmis_type_column` | str | `"CMISType"` | Split — MapeoRVI_CM. |
| `rvi_cm_cmis_folder_column` | str | `"CMISFolder"` | Split — MapeoRVI_CM. |
| `metadatos_id_corto_column` | str | `"IDCorto"` | Split — MetadatosCM. |
| `metadatos_metadata_column` | str | `"Metadato"` | Split — MetadatosCM. |
| `metadatos_required_column` | str | `"Requerido"` | Split — MetadatosCM. |
| `metadatos_cmis_property_id_column` | str | `"CMISPropertyId"` | Split — MetadatosCM. |
| `required_marker` | str | `"Yes"` | Valor que marca campo obligatorio. |

Reglas:
- Consolidado: setear `csv_path`, dejar los del modo split en `None`.
- Split: setear `rvi_cm_csv_path` Y `metadatos_csv_path`, dejar `csv_path` en `None`.

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
| `default_value` | `str \| None` | `None` | — | Si todas las sources fallan. |

### `FieldSourceItem`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `source_type` | str (required) | — | `"trigger"`, `"rvabrep"`, `"csv:{alias}"` o `"as400:{alias}"`. |
| `lookup_value_column` | str (required) | — | Columna a leer. |
| `lookup_key_column` | `str \| None` | `None` | Columna pivot. |
| `validation` | `ValidationModel \| None` | `None` | — |

### `ValidationModel`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `allowed_pattern` | `str \| None` | `None` | Regex que el valor debe matchear. |

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
| `as400_connection` | `As400ConnectionConfig` (required) | — | — | — |
| `table` | `str \| None` | `None` | `min_length=1` | Modo table. |
| `query` | `str \| None` | `None` | `min_length=1` | Modo query. |

Exactamente uno de `table` / `query` (validator `_exactly_one_table_or_query`).

### `MetadataCacheConfig`

| Field | Type | Default | Constraint | Description |
|-------|------|---------|------------|-------------|
| `enabled` | bool | `False` | — | Activa cache cross-batch en `document_cache`. |
| `ttl_minutes` | int | `60` | `1..43200` | Tope 30 días. |

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
| `connection` | `As400ConnectionConfig \| None` | `None` | required if `enabled=true` | Validator lo verifica. |
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

## Secrets (env vars, `config/env.py:Secrets`)

Las credenciales NUNCA viven en el YAML.

| Env var | Fallback | Default | Description |
|---------|----------|---------|-------------|
| `CMIS_USERNAME` | `CMIS_USER` | required for CMIS | Usuario Browser Binding. |
| `CMIS_PASSWORD` | `CMIS_PASS` | required for CMIS | Password. |
| `AS400_USERNAME` | — | `""` | Usuario ODBC. Required cuando hay source AS400. |
| `AS400_PASSWORD` | — | `""` | Password ODBC. Idem. |

---

## Ver también

- [`cli.md`](cli.md) — flags que sobrescriben campos del YAML (`--batch-size`, `--batches-in-flight`).
- [`error-codes.md`](error-codes.md) — `ConfigurationError` es lo que te tira el loader.
- [Explanation: architecture overview](../explanation/architecture-overview.md) — el por qué detrás de los defaults de `AutoTuneConfig`.
- [How-to: heavy/light lanes](../how-to/heavy-light-lanes.md) — tunear `HeavyLightLanesConfig`.
- [How-to: document cache](../how-to/document-cache.md) — habilitar `metadata.cache`.
