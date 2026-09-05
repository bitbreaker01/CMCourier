"""Schema de configuración Pydantic v2 para el pipeline de CMCourier.

Cada modelo es ``frozen=True, extra="forbid"`` para que:

* Mutar configs ya validadas explote (alineado con el patrón
  "frozen dataclasses en todos lados" del proyecto).
* Claves YAML desconocidas exploten al cargar — el operador recibe un
  error inmediato en vez de corridas mal configuradas en silencio.

Los campos de path usan :class:`pydantic.FilePath` para inputs que YA
DEBEN existir (CSVs, source_root) y :class:`pathlib.Path` para salidas
que se crean en tiempo de ejecución (temp_dir, base SQLite).

Principio V de la Constitución: este módulo es la única fuente
declarativa de verdad para la superficie configurable del pipeline.
El orchestrator y los adapters NO importan este módulo — la traducción
ocurre en :mod:`cmcourier.config.wiring`.
"""

from __future__ import annotations

__all__ = [
    "As400ConnectionConfig",
    "As400MetadataSourceConfig",
    "As400RvabrepSource",
    "AssemblyConfig",
    "SyntheticBandConfig",
    "SyntheticContentConfig",
    "AutoTuneConfig",
    "CmisConfigModel",
    "CsvMetadataSourceConfig",
    "CsvRvabrepSource",
    "CsvTriggerConfig",
    "FieldConfig",
    "FieldSourceItem",
    "HeavyLightLanesConfig",
    "IndexingColumnsModel",
    "IndexingConfig",
    "IndexingSourceConfig",
    "LocalScanTriggerConfig",
    "MappingConfig",
    "MetadataCacheConfig",
    "SingleDocTriggerConfig",
    "MetadataConfigModel",
    "MetadataSourceConfig",
    "NiarvilogColumnsModel",
    "ObservabilityConfig",
    "PipelineConfig",
    "RvabrepFiltersModel",
    "RvabrepSourceUnion",
    "RvabrepTriggerConfig",
    "StreamingConfig",
    "TrackingConfig",
    "TriggerConfigUnion",
    "TriggerCsvConfig",
    "ValidationModel",
]

import re
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    DirectoryPath,
    Field,
    FilePath,
    field_validator,
    model_validator,
)

_STRICT = ConfigDict(frozen=True, extra="forbid")

# Reglas de identificador ordinario de DB2 for i: una letra (``@``, ``#``,
# ``$`` cuentan como letras) seguida de letras / dígitos / underscore,
# máximo 128 caracteres. Los nombres de columna / library / table de
# NIARVILOG se interpolan como string dentro del SQL (un identificador
# nunca puede ser un `bind-param` ``?``), así que TODO identificador
# configurable DEBE validarse para cerrar la superficie de inyección —
# ver spec 049.
_SQL_IDENTIFIER_RE = re.compile(r"[A-Za-z@#$][A-Za-z0-9@#$_]{0,127}")


def _validate_sql_identifier(value: str) -> str:
    if not _SQL_IDENTIFIER_RE.fullmatch(value):
        msg = (
            f"{value!r} is not a valid DB2 SQL identifier "
            "(letter / @ / # / $ then letters / digits / _ / @ / # / $, "
            "128 chars max)"
        )
        raise ValueError(msg)
    return value


# ---------------------------------------------------------------------------
# Conexión AS400 (compartida: variante `source` RVABREP + sync NIARVILOG)
# ---------------------------------------------------------------------------


class As400ConnectionConfig(BaseModel):
    """Parámetros de conexión ODBC a AS400. Las credenciales viven en env vars."""

    model_config = _STRICT
    host: str
    port: int = Field(default=446, ge=1, le=65535)
    database: str = "RVILIB"
    driver: str = "iSeries Access ODBC Driver"
    table: str | None = None


# ---------------------------------------------------------------------------
# `kinds` de trigger (unión discriminada por `kind`)
# ---------------------------------------------------------------------------


class CsvTriggerConfig(BaseModel):
    model_config = _STRICT
    kind: Literal["csv"] = "csv"
    csv_path: FilePath
    shortname_column: str = "ShortName"
    cif_column: str = "CIF"
    system_id_column: str = "SystemID"


class RvabrepFiltersModel(BaseModel):
    model_config = _STRICT
    systems: list[str] = Field(default_factory=list)
    document_types: list[str] = Field(default_factory=list)


class RvabrepTriggerConfig(BaseModel):
    model_config = _STRICT
    kind: Literal["rvabrep"]
    filters: RvabrepFiltersModel = Field(default_factory=RvabrepFiltersModel)


class LocalScanTriggerConfig(BaseModel):
    """Modo ``local_scan``.

    088: ``recursive`` permite descender por todos los subdirectorios
    del ``scan_path``. Default ``False`` preserva el comportamiento
    pre-088 (solo el directorio raíz, ``Path.iterdir()``). Con
    ``True`` el motor usa ``Path.rglob("*")`` y mantiene los mismos
    filtros de filename (``*.PDF`` / ``*.001``).
    """

    model_config = _STRICT
    kind: Literal["local_scan"]
    scan_path: DirectoryPath
    recursive: bool = False


class SingleDocTriggerConfig(BaseModel):
    """Pipeline diagnóstico de un único documento.

    Sin campos extra — el trigger (shortname / cif / system_id) viene
    de los argumentos de CLI en tiempo de ejecución, no del YAML.
    """

    model_config = _STRICT
    kind: Literal["single_doc"]


TriggerConfigUnion = Annotated[
    CsvTriggerConfig | RvabrepTriggerConfig | LocalScanTriggerConfig | SingleDocTriggerConfig,
    Field(discriminator="kind"),
]
# 048: ``trigger.kind: as400`` fue removido. "AS400" ahora es una
# elección de *source* (``indexing.source.kind: as400``), no un `kind`
# de trigger — el pipeline RVABREP es el mismo pipeline independientemente
# de dónde viva su tabla RVABREP. Ver ``RvabrepSourceUnion`` más abajo.


# Alias retrocompatible: el código existente que importa TriggerCsvConfig
# sigue funcionando. La unión discriminada es la nueva forma.
TriggerCsvConfig = CsvTriggerConfig


class IndexingColumnsModel(BaseModel):
    """Mapeo columna lógica → física para el `source` RVABREP."""

    model_config = _STRICT
    shortname_column: str = "ABABCD"
    system_id_column: str = "ABAACD"
    delete_code_column: str = "ABACST"
    txn_num_column: str = "ABAANB"
    index2_column: str = "ABACCD"
    index3_column: str = "ABADCD"
    index4_column: str = "ABAECD"
    index5_column: str = "ABAFCD"
    index6_column: str = "ABAGCD"
    index7_column: str = "ABAHCD"
    image_type_column: str = "ABABST"
    image_path_column: str = "ABAICD"
    file_name_column: str = "ABAJCD"
    creation_date_column: str = "ABAADT"
    last_view_date_column: str = "ABABDT"
    total_pages_column: str = "ABABUN"


# ---------------------------------------------------------------------------
# `source` RVABREP (048 — unión discriminada por `kind`)
#
# La tabla RVABREP es la misma data, viva en un CSV (testing / staging /
# bancos chicos que exportan RVABREP a un archivo) o en DB2 sobre el
# AS400 (producción, accedido vía un SELECT que devuelve columnas con
# forma RVABREP). Ese único `source` alimenta TANTO a S0
# (DirectRvabrepTriggerStrategy) como a S1 (IndexingService) — se compone
# una sola vez en la capa de wiring.
# ---------------------------------------------------------------------------


class CsvRvabrepSource(BaseModel):
    """Tabla RVABREP simulada como archivo CSV."""

    model_config = _STRICT
    kind: Literal["csv"] = "csv"
    csv_path: FilePath


class As400RvabrepSource(BaseModel):
    """Tabla RVABREP en DB2/AS400, accedida vía un SELECT definido por el operador.

    El ``query`` puede llevar JOINs / filtros WHERE, pero sus **columnas
    de salida deben tener forma RVABREP** — el mapeo de
    ``IndexingColumnsModel`` se aplica al `result set` exactamente igual
    que se aplicaría a un CSV. Las credenciales vienen de las env vars
    ``AS400_USERNAME`` / ``AS400_PASSWORD`` (nunca del YAML).
    """

    model_config = _STRICT
    kind: Literal["as400"]
    connection: As400ConnectionConfig
    query: str


RvabrepSourceUnion = Annotated[
    CsvRvabrepSource | As400RvabrepSource,
    Field(discriminator="kind"),
]


class IndexingConfig(BaseModel):
    """Config de indexing de S1 + el `source` RVABREP del que lee (junto con S0).

    048 renombró esto desde ``IndexingSourceConfig`` y reemplazó el
    campo crudo ``csv_path`` por la unión discriminada ``source``.
    """

    model_config = _STRICT
    source: RvabrepSourceUnion
    columns: IndexingColumnsModel = Field(default_factory=IndexingColumnsModel)
    # 121: sin efecto — alimentaba al lookup batcheado eliminado.
    # Se conserva para no romper YAMLs existentes (extra="forbid").
    batch_size: int = Field(default=50, ge=1)


# 048: alias retrocompatible para que imports en vuelo del nombre viejo resuelvan.
IndexingSourceConfig = IndexingConfig


class MappingConfig(BaseModel):
    """Config del Modelo Documental en uno de dos modos mutuamente excluyentes.

    Consolidado (legacy / fixtures de test): un único CSV con todas las
    columnas inline y una celda ``METADATOS`` separada por comas. Setear
    ``csv_path`` y dejar los campos del modo `split` en ``None``.

    Split (producción / formato del banco, 035): dos CSVs unidos por
    ``IDCM ↔ IDCorto`` — ``MapeoRVI_CM.csv`` (una fila por IDRVI) más
    ``MetadatosCM.csv`` (varias filas por IDCorto). Setear tanto
    ``rvi_cm_csv_path`` como ``metadatos_csv_path`` y dejar
    ``csv_path`` en ``None``.
    """

    model_config = _STRICT
    csv_path: FilePath | None = None
    rvi_cm_csv_path: FilePath | None = None
    metadatos_csv_path: FilePath | None = None
    id_rvi_column: str = "ID RVI"
    clase_id_column: str = "ID CLASE DOCUMENTAL"
    id_corto_column: str = "ID Corto"
    clase_name_column: str = "CLASE DOCUMENTAL"
    metadata_list_column: str = "METADATOS"
    cmis_type_column: str = "CMISType"
    rvi_cm_id_rvi_column: str = "IDRVI"
    rvi_cm_id_cm_column: str = "IDCM"
    rvi_cm_clase_id_column: str = "IDClaseDocumental"
    rvi_cm_cmis_type_column: str = "CMISType"
    rvi_cm_cmis_folder_column: str = "CMISFolder"
    metadatos_id_corto_column: str = "IDCorto"
    metadatos_metadata_column: str = "Metadato"
    metadatos_required_column: str = "Requerido"
    metadatos_cmis_property_id_column: str = "CMISPropertyId"
    required_marker: str = "Yes"

    @model_validator(mode="after")
    def _exactly_one_mode(self) -> MappingConfig:
        has_consolidated = self.csv_path is not None
        has_rvi = self.rvi_cm_csv_path is not None
        has_meta = self.metadatos_csv_path is not None
        if has_consolidated and (has_rvi or has_meta):
            raise ValueError(
                "MappingConfig: pick either consolidated `csv_path` "
                "OR split (`rvi_cm_csv_path` + `metadatos_csv_path`), not both"
            )
        if not has_consolidated and not (has_rvi or has_meta):
            raise ValueError(
                "MappingConfig: must provide consolidated `csv_path` "
                "OR split (`rvi_cm_csv_path` + `metadatos_csv_path`)"
            )
        if (has_rvi and not has_meta) or (has_meta and not has_rvi):
            raise ValueError(
                "MappingConfig: split mode requires BOTH `rvi_cm_csv_path` and `metadatos_csv_path`"
            )
        return self


class CsvMetadataSourceConfig(BaseModel):
    """Un `source` CSV con nombre, disponible para la resolución de metadata."""

    model_config = _STRICT
    kind: Literal["csv"] = "csv"
    alias: str
    csv_path: FilePath


class As400MetadataSourceConfig(BaseModel):
    """Un `source` AS400 con nombre, disponible para la resolución de metadata.

    El `prefetch` ejecuta ``SELECT * FROM <table>`` (modo `table`) o
    ``SELECT * FROM (<query>) AS T`` (modo `query`) sobre la conexión
    configurada. DEBE setearse exactamente uno de ``table`` / ``query`` —
    el operador elige la forma que escala a su volumen de datos.
    """

    model_config = _STRICT
    kind: Literal["as400"]
    alias: str
    as400_connection: As400ConnectionConfig
    table: str | None = Field(default=None, min_length=1)
    query: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _exactly_one_table_or_query(self) -> As400MetadataSourceConfig:
        if bool(self.table) == bool(self.query):
            raise ValueError("as400 metadata source requires exactly one of `table` or `query`")
        return self


# Nombre retrocompatible para la forma legacy solo-CSV.
MetadataSourceConfig = Annotated[
    CsvMetadataSourceConfig | As400MetadataSourceConfig,
    Field(discriminator="kind"),
]


class ValidationModel(BaseModel):
    model_config = _STRICT
    allowed_pattern: str | None = None


class FieldSourceItem(BaseModel):
    """084: ``lookup_value_source`` permite especificar de dónde sale
    el valor de búsqueda para sources con prefijo ``csv:`` / ``as400:``.

    Default ``"trigger.cif"`` preserva el contrato pre-084 (lookups
    indexados por CIF del trigger). Sintaxis admitida:
    ``"trigger.<attr>"`` (cif, shortname, system_id) o
    ``"rvabrep.<col>"`` (txn_num, index1..index7, etc.).

    Para sources `trigger` y `rvabrep` puros, ``lookup_value_source``
    se ignora — esos paths leen directamente sin lookup.
    """

    model_config = _STRICT
    source_type: str
    lookup_value_column: str
    lookup_key_column: str | None = None
    validation: ValidationModel | None = None
    lookup_value_source: str = "trigger.cif"

    @field_validator("source_type")
    @classmethod
    def _validate_source_type(cls, value: str) -> str:
        if value in ("trigger", "rvabrep"):
            return value
        if value.startswith("csv:") or value.startswith("as400:"):
            return value
        raise ValueError(f"unknown source_type: {value!r}")

    @field_validator("lookup_value_source")
    @classmethod
    def _validate_lookup_value_source(cls, value: str) -> str:
        if "." not in value:
            raise ValueError(f"lookup_value_source must be '<scope>.<attr>' (got {value!r})")
        scope = value.split(".", 1)[0]
        if scope not in ("trigger", "rvabrep"):
            raise ValueError(
                f"lookup_value_source scope must be 'trigger' or 'rvabrep' (got {scope!r})"
            )
        return value


class FieldConfig(BaseModel):
    """083: ``sources`` puede ser vacío cuando ``default_value`` está
    seteado — útil para metadata constante (ej. clasificación
    hardcodeada, banco emisor). Si ambos están vacíos, falla la
    validación porque el campo no podría resolver nunca."""

    model_config = _STRICT
    sources: list[FieldSourceItem] = Field(default_factory=list)
    default_value: str | None = None

    @model_validator(mode="after")
    def _at_least_one_resolution_path(self) -> FieldConfig:
        if not self.sources and self.default_value is None:
            raise ValueError(
                "FieldConfig requires at least one of `sources` (non-empty) or `default_value`"
            )
        return self


class MetadataCacheConfig(BaseModel):
    """POST-MVP §9 — configuración del cache de metadata cross-`batch` (037).

    Cuando ``enabled`` es ``True``, ``StagedPipeline`` consulta la tabla
    ``document_cache`` respaldada por SQLite antes de invocar S3
    (Resolución de Metadata). Un hit cuyo ``cached_at`` esté dentro de
    ``ttl_minutes`` cortocircuita al resolver; un miss ejecuta el
    resolver y hace `upsert` del resultado. Default off — el
    comportamiento de un único `batch` es byte-idéntico al pre-037.
    """

    model_config = _STRICT
    enabled: bool = False
    ttl_minutes: int = Field(default=60, gt=0, le=43200)  # tope: 30 días


class MetadataConfigModel(BaseModel):
    model_config = _STRICT
    field_aliases: dict[str, str] = Field(default_factory=dict)
    field_sources: dict[str, FieldConfig]
    sources: list[MetadataSourceConfig] = Field(default_factory=list)
    prefetch_enabled: bool = True
    cache: MetadataCacheConfig = Field(default_factory=MetadataCacheConfig)


class SyntheticBandConfig(BaseModel):
    """102: una clase de tamaño del generador sintético.

    ``min`` / ``max`` aceptan sufijos binarios (``50kb``, ``1mb``,
    ``10mb``) — ver :func:`cmcourier.services.mock.sizing.parse_size`.
    """

    model_config = _STRICT
    name: str
    weight: float = Field(ge=0.0)
    min: str
    max: str


class SyntheticContentConfig(BaseModel):
    """102: generación de contenido sintético on-the-fly para stress tests.

    ``enabled=False`` (default) deja el comportamiento intacto. Con
    ``size_mix`` vacío se usa la distribución por defecto 60/30/10
    alineada al plan de pruebas §3.3.
    """

    model_config = _STRICT
    enabled: bool = False
    seed: int = 0
    size_mix: tuple[SyntheticBandConfig, ...] = ()


class AssemblyConfig(BaseModel):
    """085: ``keep_staged_files`` controla si los archivos ensamblados
    bajo ``temp_dir`` se preservan después de un S5_DONE exitoso.

    Default ``False`` — el orchestrator borra ``temp_dir/{txn_num}.pdf``
    post-upload. ``True`` los preserva para debug / inspección manual.
    Falla del unlink no aborta el upload (se loguea como warning).
    """

    model_config = _STRICT
    source_root: DirectoryPath
    temp_dir: Path
    image_type_map: dict[str, str] = Field(
        default_factory=lambda: {
            "B": "image/tiff",
            "O": "application/pdf",
            "C": "image/jpeg",
        }
    )
    keep_staged_files: bool = False
    # 102: contenido sintético on-the-fly para pruebas de stress.
    synthetic_content: SyntheticContentConfig = Field(default_factory=SyntheticContentConfig)


class AutoTuneConfig(BaseModel):
    """Auto-tune `AIMD` para el `worker pool` de S5.

    Cuando ``enabled=True``, un controlador en background ajusta la
    cantidad de threads y (opcionalmente) el timeout del request CMIS
    según el p95 observado de S5 vs ``target_p95_ms``.
    """

    model_config = _STRICT
    enabled: bool = False
    min_threads: int = Field(default=2, ge=1)
    max_threads: int = Field(default=50, ge=1)
    target_p95_ms: float = Field(default=5000.0, gt=0)
    adjustment_interval_s: int = Field(default=30, ge=1)
    warmup_seconds: int = Field(default=60, ge=0)
    # 061: no actuar sobre un tick que vio menos de esta cantidad de
    # muestras de S5 — el p95 por `nearest-rank` queda dominado por una
    # única muestra grande cuando N es chico, así que un outlier de
    # conexión fría en el primer `chunk` solía disparar un `halve`
    # espurio. 20 es el piso empírico donde un outlier de 30 s entre 19
    # muestras normales de 1.5 s no puede dominar.
    min_samples: int = Field(default=20, ge=1)
    timeout_auto_adjust: bool = True
    min_timeout_s: int = Field(default=30, ge=1)
    max_timeout_s: int = Field(default=600, ge=1)
    # 068: perillas de forma del crecimiento + `halve`. Pre-068 estaba
    # hardcodeado a crecimiento aditivo +1, `halve` dividir-por-2, y el
    # `halve` se disparaba en 1.2 × target_p95_ms. Eso oscilaba la
    # capacidad entre 4-8 para la carga de producción de archivos de
    # 30 MB (un outlier cada ~10 ticks halveaba 6 min de crecimiento).
    growth_factor: float = Field(default=1.25, ge=1.0, le=4.0)
    halve_factor: float = Field(default=0.75, ge=0.05, le=1.0)
    halve_threshold_ratio: float = Field(default=1.5, ge=1.05, le=10.0)

    @model_validator(mode="after")
    def _validate_ranges(self) -> AutoTuneConfig:
        if self.min_threads > self.max_threads:
            raise ValueError(
                "auto_tune.min_threads must be <= max_threads "
                f"(got {self.min_threads} > {self.max_threads})"
            )
        if self.min_timeout_s > self.max_timeout_s:
            raise ValueError(
                "auto_tune.min_timeout_s must be <= max_timeout_s "
                f"(got {self.min_timeout_s} > {self.max_timeout_s})"
            )
        return self


class CmisConfigModel(BaseModel):
    """Perillas de conexión CMIS. Las credenciales viven en env vars, no acá.

    089: ``http2`` permite optar fuera de HTTP/2. Default ``True``
    preserva el comportamiento de spec 060 (negociar h2 vía ALPN,
    fallback a 1.1 si el server no anuncia h2). Setearlo ``False``
    fuerza HTTP/1.1 — útil cuando muchos uploads paralelos
    comparten flow-control window del lado server y serializan
    el throughput agregado. Bajo HTTP/1.1 cada worker mantiene su
    propia conexión TCP (hasta ``max_keepalive_connections``).
    """

    model_config = _STRICT
    base_url: str
    repo_id: str
    timeout_seconds: float = Field(default=300.0, gt=0)
    verify_ssl: bool = False
    max_bandwidth_mbps: float = Field(default=0.0, ge=0)
    retry_max_attempts: int = Field(default=3, ge=1)
    retry_base_delay_s: float = Field(default=2.0, ge=0)
    workers: int = Field(default=4, ge=1)
    http2: bool = True
    # 090: chunk size de lectura del MultipartEncoder durante el upload.
    # Pre-090 estaba hardcoded a 8192 (8 KB) — con N workers
    # paralelos el GIL serializaba los reads (cada chunk dispara
    # work CPU en Python). Default 1 MiB reduce las GIL acquisitions
    # por un factor de 128, restaurando paralelismo real.
    upload_chunk_bytes: int = Field(default=1 << 20, ge=4096, le=64 << 20)
    auto_tune: AutoTuneConfig = Field(default_factory=AutoTuneConfig)


class NiarvilogColumnsModel(BaseModel):
    """Mapeo columna lógica → física para la tabla AS400 NIARVILOG.

    El banco corre CMCourier contra varios ambientes AS400 cuya tabla
    NIARVILOG tiene las mismas 15 columnas bajo nombres físicos
    distintos. Los defaults coinciden con los nombres canónicos para
    que una config que omita este bloque se comporte exactamente como
    pre-049.

    Cada valor se interpola dentro del SQL (un nombre de columna nunca
    puede ser un `bind-param`), así que cada campo se valida como
    identificador DB2.
    """

    model_config = _STRICT
    system_id_column: str = "SISCOD"
    txn_num_column: str = "TRNNUM"
    doc_format_column: str = "DOCFRM"
    image_archive_column: str = "IMGARC"
    image_type_column: str = "IMGTIP"
    client_cif_column: str = "CTECIF"
    client_num_column: str = "CTENUM"
    status_column: str = "STSCOD"
    idcm_column: str = "IDNBAC"
    cm_type_column: str = "TIPIDN"
    cm_object_id_column: str = "OBJIDN"
    retry_count_column: str = "NUMREI"
    started_at_column: str = "PMRREI"
    finished_at_column: str = "FINREI"
    error_message_column: str = "EERRMSG"

    @field_validator("*")
    @classmethod
    def _check_identifier(cls, v: str) -> str:
        return _validate_sql_identifier(v)


class PeriodicSyncConfig(BaseModel):
    """096 — perillas del modo de sincronización ``periodic``.

    ``interval_minutes`` controla cada cuánto el reconciliador de fondo
    sincroniza SQLite ↔ AS400. Solo aplica cuando
    ``tracking.as400_sync.mode == "periodic"``.
    """

    model_config = _STRICT
    interval_minutes: int = Field(default=5, ge=1, le=1440)


class As400SyncConfig(BaseModel):
    """POST-MVP §4 — coordinación distribuida de `idempotency` vía AS400 NIARVILOG.

    El default ``enabled=False`` preserva el comportamiento pre-034
    solo-SQLite. Cuando se habilita, el pipeline coordina con la tabla
    centralizada ``RVILIB.NIARVILOG`` para `idempotency` cross-`batch`,
    claim atómico contra procesos concurrentes, y estado de upload
    visible para el operador.

    ``mode`` (096) elige cómo se sincroniza con AS400:

    * ``"claim"`` (default): claim atómico por-documento en S5 — el
      comportamiento pre-096. Previene doble-upload contra procesos
      concurrentes a costa de 2-3 round-trips sincrónicos por doc.
    * ``"periodic"``: S5 escribe solo SQLite; un reconciliador de fondo
      sincroniza las dos bases cada ``periodic.interval_minutes``.
      **No previene** doble-upload — los conflictos se detectan
      post-hoc. Solo para entornos sin migrador competidor.
    """

    model_config = _STRICT
    enabled: bool = False
    connection: As400ConnectionConfig | None = None
    library: str = "RVILIB"
    table: str = "NIARVILOG"
    columns: NiarvilogColumnsModel = Field(default_factory=NiarvilogColumnsModel)
    stale_in_progress_minutes: int = Field(default=30, ge=1, le=1440)
    retry_attempts: int = Field(default=3, ge=1, le=10)
    retry_base_delay_s: float = Field(default=5.0, gt=0)
    mode: Literal["claim", "periodic"] = "claim"
    periodic: PeriodicSyncConfig | None = None

    @field_validator("library", "table")
    @classmethod
    def _check_identifier(cls, v: str) -> str:
        return _validate_sql_identifier(v)

    @model_validator(mode="after")
    def _connection_required_when_enabled(self) -> As400SyncConfig:
        if self.enabled and self.connection is None:
            msg = (
                "tracking.as400_sync.enabled=true requires tracking.as400_sync.connection to be set"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _periodic_required_when_mode_periodic(self) -> As400SyncConfig:
        if self.mode == "periodic" and self.periodic is None:
            msg = (
                "tracking.as400_sync.mode='periodic' requires "
                "tracking.as400_sync.periodic to be set"
            )
            raise ValueError(msg)
        return self


class TrackingConfig(BaseModel):
    model_config = _STRICT
    db_path: Path
    as400_sync: As400SyncConfig = Field(default_factory=As400SyncConfig)


class HeavyLightLanesConfig(BaseModel):
    """POST-MVP §1 — configuración adaptativa de `lanes` `heavy`/`light` de upload (036).

    Cuando ``enabled`` es ``True`` y un `batch` tiene al menos
    ``heavy_lane_min_batch`` ítems, S5 separa documentos por
    ``file_size_bytes >= heavy_threshold_bytes`` en dos `lanes` que
    comparten el presupuesto total de `workers`. El presupuesto total
    lo maneja `AIMD` (cuando está activo); ``heavy_initial_ratio`` más
    un demonio de rebalanceo basado en `drain` (``rebalance_interval_s``
    / ``idle_threshold_s``) controlan la división entre `lanes`.

    El default ``enabled = False`` preserva el comportamiento pre-036
    de un único `pool`, byte por byte.
    """

    model_config = _STRICT
    enabled: bool = False
    heavy_threshold_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    heavy_lane_min_batch: int = Field(default=50, ge=1)
    heavy_initial_ratio: float = Field(default=0.2, ge=0.0, le=1.0)
    rebalance_interval_s: float = Field(default=10.0, gt=0.0, le=600.0)
    idle_threshold_s: float = Field(default=15.0, gt=0.0, le=3600.0)


class StreamingConfig(BaseModel):
    """063 — perillas del modo `streaming`.

    ``bucket_size`` es la cantidad máxima de documentos totalmente
    preparados sentados entre PREP (productores S1–S4) y UPLOAD
    (consumidores S5). El `bucket` es una cola acotada: cuando está
    llena, los productores bloquean; cuando está vacía, los
    consumidores bloquean. Por eso el pico de memoria escala solo con
    ``bucket_size`` — independiente del total de triggers.
    """

    model_config = _STRICT
    bucket_size: int = Field(default=100, ge=1)


class ProcessingConfig(BaseModel):
    """POST-MVP §7 + §1 + 063 — modo de orquestación + perillas por modo.

    ``mode`` (063) elige el orchestrator del pipeline:

    * ``"batched"`` (default): el pipeline multi-`batch` histórico de
      N=2 — el `chunk` N+1 prepara mientras el `chunk` N sube. Respeta
      ``batches_in_flight`` y la semántica completa de `resume`.
    * ``"streaming"``: un pipeline productor-consumidor continuo
      manejado por un `bucket` acotado (``streaming.bucket_size``).
      ``batches_in_flight`` se **ignora** en este modo — hay un solo
      `batch` lógico por corrida. Los args de `resume`
      (``--from-stage``, ``--batch-id``) se rechazan; `resume` = una
      nueva corrida.

    ``batches_in_flight`` controla el solape productor-consumidor del
    modo `batched`: mientras el `batch` N sube (S5), los `batches`
    N+1..N+(K-1) preparan (S0–S4) concurrentemente. El default ``2``
    es el modelo canónico "uno preparando + uno subiendo".

    ``prep_workers`` (056) dimensiona un thread pool fijo para los
    `stages` de prep S2 (mapping), S3 (metadata) y S4 (assembly) —
    de lo contrario corren un documento por vez. El default ``1``
    mantiene el comportamiento serial byte-idéntico. S0/S1 quedan
    seriales por diseño (cargan la lógica de `idempotency`
    cross-`batch` + `resume`). Aplica a ambos modos.

    ``heavy_light_lanes`` lleva la config de doble `lane` (POST-MVP §1) —
    `default-off`; ver :class:`HeavyLightLanesConfig`. En modo
    `streaming` los `lanes` están *diferidos* (spec 065) — la capa de
    wiring emite un WARN claro al arrancar si el operador los combina.
    """

    model_config = _STRICT
    mode: Literal["batched", "streaming"] = "batched"
    streaming: StreamingConfig = Field(default_factory=StreamingConfig)
    batches_in_flight: int = Field(default=2, ge=1, le=2)
    prep_workers: int = Field(default=1, ge=1)
    heavy_light_lanes: HeavyLightLanesConfig = Field(default_factory=HeavyLightLanesConfig)
    # 066: paralelismo CPU-bound real para S4 (`PDF assembly`).
    # El paralelismo por threads queda serializado por el GIL para el
    # trabajo de img2pdf/PIL/PyPDF2; mover S4 a un `ProcessPoolExecutor`
    # da throughput de N cores. Default ``True`` porque todo `benchmark`
    # por encima de ~5 docs/s se beneficia. ``False`` corre S4 inline en
    # el thread productor (comportamiento pre-066).
    s4_use_processes: bool = True
    # ``None`` => ``os.cpu_count()``; un int explícito lo sobreescribe.
    s4_max_processes: int | None = Field(default=None, ge=1)
    # 094: ruteo por tipo de documento en S4. Cuando True Y el process
    # pool está activo, los PDF nativos (``document.is_pdf``) corren
    # inline en el thread del prep_workers (``shutil.copy2`` libera el
    # GIL durante I/O), mientras que los paginados TIFF/JPEG van al
    # process pool (``img2pdf`` es CPU bound). Evita el overhead de
    # pickle/IPC/spawn del process pool para docs cuyo trabajo útil
    # NO es CPU bound. Crítico en Windows donde ``spawn`` es caro.
    # 114: default ``True`` — con ``s4_use_processes`` on por default,
    # mandar los PDF nativos al pool era puro overhead. ``false`` en el
    # YAML restaura el comportamiento 094-off (todo al pool).
    s4_smart_routing: bool = True


class SystemMetricsConfig(BaseModel):
    """POST-MVP §2 — `sampling` de recursos de sistema (tier 5) vía ``psutil``.

    ``enabled`` por default está ON: cuando corre un pipeline, un thread
    daemon muestrea métricas a nivel host y proceso cada
    ``sample_interval_s`` segundos y escribe JSONL en
    ``observability.log_dir/system-{date}.jsonl``. Setear
    ``enabled: false`` (o la forma bool legacy ``system_metrics: false``)
    para opt-out en ambientes de bajo overhead.
    """

    model_config = _STRICT
    enabled: bool = True
    sample_interval_s: float = Field(default=5.0, ge=1.0, le=60.0)


class ObservabilityConfig(BaseModel):
    """Observabilidad — toggles por tier + directorio de logs + umbrales.

    Los tiers 1-4 (app log, métricas de pipeline, métricas de red,
    reporte de slow-ops) shippean desde 020. El tier 5 (métricas de
    sistema vía psutil) shippeó en 026 — ver ``SystemMetricsConfig``.
    """

    model_config = _STRICT
    enabled: bool = True
    pipeline_metrics: bool = True
    network_metrics: bool = True
    system_metrics: SystemMetricsConfig = Field(default_factory=SystemMetricsConfig)
    log_dir: Path = Path("./logs")
    log_format: Literal["json", "text"] = "json"
    rotation_mb: int = Field(default=100, ge=1)
    retention_days: int = Field(default=30, ge=1)
    slow_op_threshold_ms: int = Field(default=5000, ge=0)
    slow_op_top_n: int = Field(default=20, ge=1)
    # 038: cuando es True, los eventos de trace del payload de upload
    # (``s5_upload_attempt`` / ``s5_upload_failed``) emiten valores
    # crudos de propiedades en vez de los enmascarados por PII. NUNCA
    # default-true; expuesto solo vía archivo de config (sin flag de CLI)
    # para evitar habilitaciones accidentales en `batches` PRD. El doctor
    # emite un WARNING cuando esto está seteado para que el operador vea
    # la desviación al arrancar.
    unmask_pii: bool = False

    @field_validator("system_metrics", mode="before")
    @classmethod
    def _coerce_system_metrics(cls, value: object) -> object:
        # REQ-002: aceptar la forma bool legacy (`system_metrics: false`)
        # de YAMLs pre-026 y promoverla al modelo estructurado.
        if isinstance(value, bool):
            return {"enabled": value}
        return value


class PipelineConfig(BaseModel):
    """Config top-level que agrega cada bloque de configuración por `stage`."""

    model_config = _STRICT
    trigger: TriggerConfigUnion
    indexing: IndexingConfig
    mapping: MappingConfig
    metadata: MetadataConfigModel
    assembly: AssemblyConfig
    cmis: CmisConfigModel
    tracking: TrackingConfig
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    processing: ProcessingConfig = Field(default_factory=ProcessingConfig)
    # 123: etiqueta de entorno para la consola de operación. "prd" activa
    # el interlock de lanzamiento (confirmación tipeada) y el badge rojo
    # permanente. Los comandos headless existentes la ignoran.
    environment: Literal["staging", "prd"] = "staging"
    batch_size: int = Field(default=1000, ge=1)
