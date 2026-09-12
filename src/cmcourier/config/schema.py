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
La traducción a objetos de dominio ocurre en :mod:`cmcourier.config.wiring`;
los orchestrators, servicios y adapters que reciben sub-modelos de
config (``ProcessingConfig``, ``As400SyncConfig``, ``MetadataConfigModel``)
los tratan como valores inmutables, nunca como fuente de wiring.
"""

from __future__ import annotations

__all__ = [
    "AnyConnectionConfig",
    "As400ConnectionConfig",
    "As400MetadataSourceConfig",
    "As400RvabrepSource",
    "AssemblyConfig",
    "AutoTuneConfig",
    "CmisConfigModel",
    "ConnectionConfig",
    "ConnectionKind",
    "ConnectionRef",
    "CsvMetadataSourceConfig",
    "CsvRvabrepSource",
    "CsvTriggerConfig",
    "EligibilityConfigModel",
    "EligibilityMatchModel",
    "FieldConfig",
    "FieldSourceItem",
    "HeavyLightLanesConfig",
    "INLINE_CONNECTION_ALIAS",
    "IdentityConfigModel",
    "IdentitySlotModel",
    "IndexingColumnsModel",
    "IndexingConfig",
    "IndexingSourceConfig",
    "LOOKUP_SOURCE_KINDS",
    "LocalScanTriggerConfig",
    "MappingConfig",
    "MetadataCacheConfig",
    "MetadataConfigModel",
    "MetadataSourceConfig",
    "MssqlConnectionConfig",
    "MssqlMetadataSourceConfig",
    "NiarvilogColumnsModel",
    "ObservabilityConfig",
    "PadLeftModel",
    "PadRightModel",
    "PipelineConfig",
    "RESERVED_CONNECTION_ALIASES",
    "RvabrepFiltersModel",
    "RvabrepSourceUnion",
    "RvabrepTriggerConfig",
    "SingleDocTriggerConfig",
    "StreamingConfig",
    "SyntheticBandConfig",
    "SyntheticContentConfig",
    "TrackingConfig",
    "TriggerConfigUnion",
    "TriggerCsvConfig",
    "ValidationModel",
    "ValueFormatModel",
    "as400_probe_sql",
    "credential_env_vars",
    "mssql_probe_sql",
    "split_field_reference",
    "split_lookup_source_type",
]

import re
from dataclasses import dataclass
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


def _validate_qualified_table(value: str | None) -> str | None:
    """``table`` de una fuente de metadata: 1 a 3 identificadores separados
    por punto (``CLIENTES``, ``RVILIB.CLIENTES``, ``db.dbo.clientes``).
    Se interpola crudo en el ``SELECT ... FROM`` del prefetch, así que
    aplica la misma regla que el resto de los identificadores (049)."""
    if value is None:
        return None
    parts = value.split(".")
    if len(parts) > 3:
        raise ValueError(f"{value!r} is not a valid table identifier (at most db.schema.table)")
    for part in parts:
        _validate_sql_identifier(part)
    return value


# ---------------------------------------------------------------------------
# Conexión AS400 (compartida: variante `source` RVABREP + sync NIARVILOG)
# ---------------------------------------------------------------------------


class As400ConnectionConfig(BaseModel):
    """Parámetros de conexión ODBC a AS400. Las credenciales viven en env vars."""

    model_config = _STRICT
    kind: Literal["as400"] = "as400"
    host: str
    port: int = Field(default=446, ge=1, le=65535)
    database: str = "RVILIB"
    driver: str = "iSeries Access ODBC Driver"
    table: str | None = None
    # 143: consulta con la que el doctor / "probar conexión" verifica la
    # conexión. Sin ella se deriva de la tabla del sitio que la usa (con
    # SafeNet/i sólo hay permiso sobre objetos whitelisteados por perfil).
    probe_query: str | None = None


class MssqlConnectionConfig(BaseModel):
    """129 — parámetros de conexión ODBC a SQL Server (msodbcsql18).

    ``encrypt`` / ``trust_server_certificate`` mapean a ``Encrypt=`` /
    ``TrustServerCertificate=`` de la connection string. El driver 18
    cifra por defecto y rechaza certificados autofirmados — en un banco
    local se setea ``trust_server_certificate: true``.
    """

    model_config = _STRICT
    kind: Literal["mssql"] = "mssql"
    host: str
    port: int = Field(default=1433, ge=1, le=65535)
    database: str
    driver: str = "ODBC Driver 18 for SQL Server"
    encrypt: bool = True
    trust_server_certificate: bool = False
    probe_query: str | None = None  # 143: ver As400ConnectionConfig


AnyConnectionConfig = As400ConnectionConfig | MssqlConnectionConfig
ConnectionKind = Literal["as400", "mssql"]

ConnectionConfig = Annotated[
    As400ConnectionConfig | MssqlConnectionConfig,
    Field(discriminator="kind"),
]

# Alias del registro `connections:`: minúscula + dígitos + underscore, así
# `<ALIAS>_USERNAME` es un nombre de env var válido en cualquier shell.
_CONNECTION_ALIAS_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")
# Alias implícito de toda conexión inline (compat: `AS400_USERNAME/PASSWORD`).
INLINE_CONNECTION_ALIAS = "as400"
# `cmis` es el destino, con su propio bloque `cmis:` y sus env vars `CMIS_*`.
RESERVED_CONNECTION_ALIASES = frozenset({"cmis"})


@dataclass(frozen=True, slots=True)
class ConnectionRef:
    """129 — una conexión que un sitio de la config necesita, ya resuelta.

    ``site`` es ``indexing`` / ``metadata:<alias de fuente>`` /
    ``tracking.as400_sync``. ``alias`` es la clave del registro o
    :data:`INLINE_CONNECTION_ALIAS` para conexiones inline; de él salen las
    env vars de credenciales (``<ALIAS>_USERNAME`` / ``<ALIAS>_PASSWORD``).
    """

    alias: str
    kind: ConnectionKind
    spec: AnyConnectionConfig
    site: str
    # 143: consulta de prueba DERIVADA de la tabla/query del sitio (None
    # si el sitio no la conoce). `spec.probe_query` explícita tiene prioridad.
    probe_sql: str | None = None

    @property
    def env_vars(self) -> tuple[str, str]:
        return credential_env_vars(self.alias)


def credential_env_vars(alias: str) -> tuple[str, str]:
    """Nombres de las env vars de credenciales del alias (``X_USERNAME``, ``X_PASSWORD``)."""
    prefix = alias.upper()
    return (f"{prefix}_USERNAME", f"{prefix}_PASSWORD")


def as400_probe_sql(table: str | None, query: str | None) -> str | None:
    """143 — prueba mínima sobre la tabla/query real del sitio (DB2 for i).

    Una sola fila, sin columnas de la tabla: barata y sólo exige el
    permiso de lectura que el pipeline necesita de todas formas.
    """
    if table:
        return f"SELECT 1 FROM {table} FETCH FIRST 1 ROW ONLY"
    if query:
        return f"SELECT 1 FROM ({query.strip().rstrip(';')}) AS T FETCH FIRST 1 ROW ONLY"
    return None


def mssql_probe_sql(table: str | None, query: str | None) -> str | None:
    """143 — ídem para SQL Server (``TOP`` en vez de ``FETCH FIRST``)."""
    if table:
        return f"SELECT TOP 1 1 FROM {table}"
    if query:
        return f"SELECT TOP 1 1 FROM ({query.strip().rstrip(';')}) AS T"
    return None


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
    """Filtros del escaneo ``kind: rvabrep``. Los dos NO son simétricos (148).

    ``systems`` recorta de verdad: va al ``WHERE ... IN`` del SQL y una
    fila de otro sistema nunca vuelve del origen.

    ``document_types`` **cambió de significado en la 148**: de *"traeme
    sólo estos"* pasó a *"de todo lo que traigas, migrá estos y contame
    el resto"*. Ya no toca el SQL — una fila cuyo código no está en la
    lista vuelve igual y queda registrada con ``EXCLUDED_BY_FILTER``, que
    es lo único que le permite al operador ver cuántos documentos dejó
    afuera su propio filtro y de qué códigos eran.
    """

    model_config = _STRICT
    systems: list[str] = Field(
        default_factory=list,
        description="Filtra por `ABAACD`. Va al SQL: lo que no matchea no vuelve del origen.",
    )
    document_types: list[str] = Field(
        default_factory=list,
        description=(
            "Códigos RVI (`ABAHCD`) a MIGRAR. 148: no toca el SQL — el resto vuelve igual "
            "y se reporta como `EXCLUDED_BY_FILTER` en el censo del batch."
        ),
    )


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
    # 129: objeto inline (alias implícito `as400`) o alias del registro.
    connection: As400ConnectionConfig | str
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

    Split (035, DEPRECADO por 145): dos CSVs unidos por
    ``IDCM ↔ IDCorto`` — ``MapeoRVI_CM.csv`` (una fila por IDRVI) más
    ``MetadatosCM.csv`` (varias filas por IDCorto). Setear tanto
    ``rvi_cm_csv_path`` como ``metadatos_csv_path`` y dejar
    ``csv_path`` en ``None``.

    Manifest (145 REQ-001, el modo nuevo): ``MapeoRVI_CM.csv`` reducido a
    ``IDSistema,IDRVI,IDCM`` más el manifest JSON de tipos CM que bajó
    ``cmcourier types discover``. Todo lo que el servidor ya sabe (tipo,
    carpeta, propiedades) sale del manifest; nadie mantiene
    ``MetadatosCM.csv`` a mano. Setear ``rvi_cm_csv_path`` y
    ``type_manifest_path``.

    El validador exige ``csv_path`` XOR (``rvi_cm_csv_path`` + EXACTAMENTE
    uno de ``metadatos_csv_path`` / ``type_manifest_path``).
    """

    model_config = _STRICT
    csv_path: FilePath | None = None
    rvi_cm_csv_path: FilePath | None = None
    metadatos_csv_path: FilePath | None = None
    type_manifest_path: FilePath | None = None
    id_rvi_column: str = "ID RVI"
    clase_id_column: str = "ID CLASE DOCUMENTAL"
    id_corto_column: str = "ID Corto"
    clase_name_column: str = "CLASE DOCUMENTAL"
    metadata_list_column: str = "METADATOS"
    cmis_type_column: str = "CMISType"
    rvi_cm_id_rvi_column: str = "IDRVI"
    rvi_cm_id_cm_column: str = "IDCM"
    # 145 REQ-001: en modo manifest es opcional (columna ausente ≡ todas
    # las filas al comodín).
    rvi_cm_id_sistema_column: str = "IDSistema"
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
        has_manifest = self.type_manifest_path is not None
        if has_consolidated and (has_rvi or has_meta or has_manifest):
            raise ValueError(
                "MappingConfig: pick either consolidated `csv_path` "
                "OR `rvi_cm_csv_path` + one of `metadatos_csv_path` / "
                "`type_manifest_path`, not both"
            )
        if not has_consolidated and not (has_rvi or has_meta or has_manifest):
            raise ValueError(
                "MappingConfig: must provide consolidated `csv_path` "
                "OR `rvi_cm_csv_path` + `type_manifest_path`"
            )
        if has_consolidated:
            return self
        if not has_rvi:
            raise ValueError("MappingConfig: split/manifest mode requires `rvi_cm_csv_path`")
        if has_meta == has_manifest:
            raise ValueError(
                "MappingConfig: `rvi_cm_csv_path` needs EXACTLY one of "
                "`metadatos_csv_path` (split, deprecated) / "
                "`type_manifest_path` (manifest, 145)"
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
    # 129: objeto inline (alias implícito `as400`) o alias del registro.
    as400_connection: As400ConnectionConfig | str
    table: str | None = Field(default=None, min_length=1)
    query: str | None = Field(default=None, min_length=1)

    _validate_table = field_validator("table")(_validate_qualified_table)

    @model_validator(mode="after")
    def _exactly_one_table_or_query(self) -> As400MetadataSourceConfig:
        if bool(self.table) == bool(self.query):
            raise ValueError("as400 metadata source requires exactly one of `table` or `query`")
        return self


class MssqlMetadataSourceConfig(BaseModel):
    """130: un `source` SQL Server con nombre, disponible para la resolución
    de metadata.

    La conexión es SIEMPRE un alias del registro ``connections:`` (kind
    ``mssql``): no hay forma inline — es nuevo, no hay compat que respetar.
    ``table`` admite esquema (``dbo.clientes``); igual que AS400, DEBE
    setearse exactamente uno de ``table`` / ``query``.
    """

    model_config = _STRICT
    kind: Literal["mssql"]
    alias: str
    connection: str
    table: str | None = Field(default=None, min_length=1)
    query: str | None = Field(default=None, min_length=1)

    _validate_table = field_validator("table")(_validate_qualified_table)

    @model_validator(mode="after")
    def _exactly_one_table_or_query(self) -> MssqlMetadataSourceConfig:
        if bool(self.table) == bool(self.query):
            raise ValueError("mssql metadata source requires exactly one of `table` or `query`")
        return self


# Nombre retrocompatible para la forma legacy solo-CSV.
MetadataSourceConfig = Annotated[
    CsvMetadataSourceConfig | As400MetadataSourceConfig | MssqlMetadataSourceConfig,
    Field(discriminator="kind"),
]

# 130: prefijos de `source_type` que resuelven contra `metadata.sources[]`
# por alias. Un único helper reemplaza los `startswith` repartidos.
LOOKUP_SOURCE_KINDS: tuple[str, ...] = ("csv", "as400", "mssql")


def split_lookup_source_type(source_type: str) -> tuple[str, str] | None:
    """``"csv:clients"`` → ``("csv", "clients")``; ``None`` si no es un lookup."""
    kind, sep, alias = source_type.partition(":")
    if not sep or kind not in LOOKUP_SOURCE_KINDS:
        return None
    return kind, alias


# 147: scopes admitidos por ``lookup_value_source``. ``field`` es el tercero
# (REQ-001): la clave de búsqueda es el valor YA RESUELTO de otro campo.
LOOKUP_VALUE_SCOPES: tuple[str, ...] = ("trigger", "rvabrep", "field")


def split_field_reference(lookup_value_source: str) -> str | None:
    """``"field.BAC_CIF"`` → ``"BAC_CIF"``; ``None`` si no es scope ``field``.

    Único parser de la arista campo → campo del grafo de dependencias (147
    REQ-001). Lo consumen el validador de :class:`MetadataConfigModel` (al
    cargar el YAML) y el resolver de metadata (al ordenar topológicamente).
    """
    scope, sep, attr = lookup_value_source.partition(".")
    if not sep or scope != "field":
        return None
    return attr


def _find_dependency_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    """El PRIMER ciclo del grafo, completo y en orden (``A -> B -> C -> A``).

    Devolver el ciclo entero y no un "cycle detected" pelado es el punto:
    el operador tiene que poder ir al YAML y cortar la arista concreta.
    """
    done: set[str] = set()
    stack: list[str] = []
    on_stack: set[str] = set()

    def visit(node: str) -> list[str] | None:
        if node in done:
            return None
        if node in on_stack:
            return [*stack[stack.index(node) :], node]
        on_stack.add(node)
        stack.append(node)
        for dep in graph.get(node, ()):
            cycle = visit(dep)
            if cycle is not None:
                return cycle
        stack.pop()
        on_stack.discard(node)
        done.add(node)
        return None

    for root in graph:
        cycle = visit(root)
        if cycle is not None:
            return cycle
    return None


class ValidationModel(BaseModel):
    model_config = _STRICT
    allowed_pattern: str | None = None


class PadLeftModel(BaseModel):
    """146: relleno a la IZQUIERDA hasta ``width`` (default ``"0"``, que es
    el caso real: cuentas de largo fijo con ceros adelante)."""

    model_config = _STRICT
    width: int = Field(gt=0)
    char: str = Field(default="0", min_length=1, max_length=1)


class PadRightModel(BaseModel):
    """146: relleno a la DERECHA hasta ``width`` (default ``" "``, que es
    cómo DB2 entrega una columna ``CHAR(n)``)."""

    model_config = _STRICT
    width: int = Field(gt=0)
    char: str = Field(default=" ", min_length=1, max_length=1)


class ValueFormatModel(BaseModel):
    """146: normalización declarativa del valor de un metadato.

    Vive en dos lugares con el mismo modelo y dos momentos distintos:
    ``FieldSourceItem.format`` corre entre buscar el valor y validarlo
    (normaliza la vestimenta ANTES del patrón, que es lo que permite
    escribir patrones estrictos sin perder valores buenos), y
    ``FieldConfig.format`` corre sobre el valor ganador y sobre
    ``default_value``, justo antes de devolverlo (la garantía de salida).

    Las transformaciones se aplican SIEMPRE en el mismo orden fijo —
    ``trim`` → ``case`` → ``strip_leading_zeros`` → ``pad_left`` →
    ``pad_right`` → ``truncate``. No es una lista de pasos configurable:
    un orden libre vuelve el YAML imposible de leer y de auditar.

    Todos los campos son opcionales; un ``format:`` sin ninguna clave
    seteada es válido y es un no-op.
    """

    model_config = _STRICT
    trim: bool = False
    case: Literal["upper", "lower"] | None = None
    strip_leading_zeros: bool = False
    pad_left: PadLeftModel | None = None
    pad_right: PadRightModel | None = None
    truncate: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _truncate_not_below_pad(self) -> ValueFormatModel:
        """Rellenar hasta ``width`` para después cortar a menos que eso no
        tiene lectura sensata: es un error de config, y explota al cargar
        el YAML en vez de recortar valores en silencio en runtime."""
        if self.truncate is None:
            return self
        for label, pad in (("pad_left", self.pad_left), ("pad_right", self.pad_right)):
            if pad is not None and self.truncate < pad.width:
                raise ValueError(
                    f"truncate ({self.truncate}) is smaller than {label}.width ({pad.width}): "
                    "padding and then cutting back has no sensible reading"
                )
        return self


class FieldSourceItem(BaseModel):
    """084: ``lookup_value_source`` permite especificar de dónde sale
    el valor de búsqueda para sources con prefijo ``csv:`` / ``as400:``.

    Default ``"trigger.cif"`` preserva el contrato pre-084 (lookups
    indexados por CIF del trigger). Sintaxis admitida:
    ``"trigger.<attr>"`` (cif, shortname, system_id) o
    ``"rvabrep.<col>"`` (txn_num, index1..index7, etc.).

    Para sources `trigger` y `rvabrep` puros, ``lookup_value_source``
    se ignora — esos paths leen directamente sin lookup.

    147 agrega el tercer scope ``"field.<CANONICAL_NAME>"``: la clave de
    búsqueda es el valor YA RESUELTO de otro campo, que es lo que permite
    encadenar saltos (hijo → padre → shortname → CIF). El grafo que arman
    esas referencias se valida al CARGAR el YAML — ver
    :class:`MetadataConfigModel`.
    """

    model_config = _STRICT
    source_type: str
    lookup_value_column: str
    lookup_key_column: str | None = None
    validation: ValidationModel | None = None
    lookup_value_source: str = "trigger.cif"
    # 146: corre ENTRE el fetch y `validation` — normaliza la vestimenta
    # antes de que el patrón la juzgue.
    format: ValueFormatModel | None = None

    @field_validator("source_type")
    @classmethod
    def _validate_source_type(cls, value: str) -> str:
        if value in ("trigger", "rvabrep"):
            return value
        if split_lookup_source_type(value) is not None:
            return value
        raise ValueError(f"unknown source_type: {value!r}")

    @field_validator("lookup_value_source")
    @classmethod
    def _validate_lookup_value_source(cls, value: str) -> str:
        if "." not in value:
            raise ValueError(f"lookup_value_source must be '<scope>.<attr>' (got {value!r})")
        scope = value.split(".", 1)[0]
        if scope not in LOOKUP_VALUE_SCOPES:
            raise ValueError(
                f"lookup_value_source scope must be 'trigger', 'rvabrep' or 'field' (got {scope!r})"
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
    # 146: corre sobre el valor GANADOR y sobre `default_value`, justo
    # antes de devolverlo. Gane la fuente que gane, a CM llega el mismo
    # formato.
    format: ValueFormatModel | None = None

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

    @model_validator(mode="after")
    def _lookup_prefix_matches_source_kind(self) -> MetadataConfigModel:
        """130: ``"<kind>:<alias>"`` debe apuntar a una fuente declarada con
        ESE kind — un prefijo que miente (``mssql:`` sobre un CSV) no
        avisa en runtime porque la resolución es por alias. Un alias no
        declarado acá no es asunto del schema: lo reporta el resolver."""
        kinds = {source.alias: source.kind for source in self.sources}
        for field_name, field_config in self.field_sources.items():
            for item in field_config.sources:
                lookup = split_lookup_source_type(item.source_type)
                if lookup is None:
                    continue
                prefix, alias = lookup
                declared = kinds.get(alias)
                if declared is not None and declared != prefix:
                    raise ValueError(
                        f"field_sources[{field_name}]: source_type {item.source_type!r} "
                        f"but metadata.sources[{alias}] is kind {declared!r}"
                    )
        return self

    def field_dependency_graph(self) -> dict[str, list[str]]:
        """147 REQ-001: campo → campos de los que depende, en orden de config.

        Una arista por cada ``lookup_value_source: "field.<NAME>"``. Referirse
        a un campo que no existe es un error de config y explota acá, al
        cargar el YAML — nunca en runtime, donde ya sería tarde.
        """
        graph: dict[str, list[str]] = {}
        for field_name, field_config in self.field_sources.items():
            deps: list[str] = []
            for item in field_config.sources:
                dep = split_field_reference(item.lookup_value_source)
                if dep is None:
                    continue
                if dep not in self.field_sources:
                    raise ValueError(
                        f"field_sources[{field_name}]: lookup_value_source "
                        f"{item.lookup_value_source!r} references field {dep!r}, "
                        "which is not a key of metadata.field_sources"
                    )
                if dep not in deps:
                    deps.append(dep)
            graph[field_name] = deps
        return graph

    @model_validator(mode="after")
    def _field_dependencies_are_acyclic(self) -> MetadataConfigModel:
        """147 REQ-001: el grafo de ``field.<NAME>`` no puede tener ciclos.

        Un ciclo se reporta COMPLETO y en orden (``A -> B -> C -> A``): un
        "cycle detected" pelado obliga al operador a reconstruir el grafo a
        mano sobre un YAML de cientos de líneas.
        """
        cycle = _find_dependency_cycle(self.field_dependency_graph())
        if cycle is not None:
            raise ValueError(
                "metadata.field_sources has a dependency cycle through "
                f"lookup_value_source 'field.<NAME>': {' -> '.join(cycle)}"
            )
        return self


class IdentitySlotModel(BaseModel):
    """147 REQ-002: un slot del bloque ``identity:``.

    ``field`` es el nombre canónico de ``metadata.field_sources`` que resuelve
    ese pedazo de identidad — el mismo motor de cadenas, así que puede llegar
    después de tres saltos. ``on_missing`` decide qué pasa cuando la cadena no
    dio nada: ``fail`` (default) rompe el documento con un error que nombra la
    cadena entera, ``warn`` deja el valor vacío y sigue, ``default`` usa
    ``default_value``.

    ``max_digits`` sólo tiene lectura sobre el CIF (lo valida
    :class:`IdentityConfigModel`): es la precisión REAL de ``CTENUM``, y un
    valor más largo rompe con ``22003`` del lado del banco.
    """

    model_config = _STRICT
    field: str
    on_missing: Literal["fail", "warn", "default"] = "fail"
    max_digits: int | None = Field(default=None, gt=0)
    default_value: str | None = None

    @model_validator(mode="after")
    def _default_requires_a_default_value(self) -> IdentitySlotModel:
        if self.on_missing == "default" and self.default_value is None:
            raise ValueError(
                "on_missing: 'default' requires a non-null default_value "
                "(there is nothing to fall back to otherwise)"
            )
        return self


class IdentityConfigModel(BaseModel):
    """147 REQ-002: bloque top-level ``identity:``.

    Reemplaza el hardcode de ``"BAC_CIF"`` que vivía en el resolver de
    metadata. Los tres slots son OPCIONALES: un slot ausente deja el
    comportamiento pre-147 (el valor se lee del trigger, sin cadena).
    """

    model_config = _STRICT
    shortname: IdentitySlotModel | None = None
    cif: IdentitySlotModel | None = None
    system_id: IdentitySlotModel | None = None

    def slots(self) -> tuple[tuple[str, IdentitySlotModel | None], ...]:
        """Los tres slots con su nombre, en orden fijo. Única fuente de verdad
        del recorrido para validadores y wiring."""
        return (
            ("shortname", self.shortname),
            ("cif", self.cif),
            ("system_id", self.system_id),
        )

    @model_validator(mode="after")
    def _max_digits_only_on_cif(self) -> IdentityConfigModel:
        for name, slot in self.slots():
            if name != "cif" and slot is not None and slot.max_digits is not None:
                raise ValueError(
                    f"identity.{name}: max_digits is only valid on `cif` "
                    "(it models the real precision of the CTENUM column)"
                )
        return self


class EligibilityMatchModel(BaseModel):
    """150 REQ-001: un criterio de ``match_any``.

    ``field`` es el nombre canónico de ``metadata.field_sources`` cuyo valor
    YA RESUELTO se busca; ``column``, la columna de la lista de activos donde
    se lo busca. Basta con que UNO de los criterios matchee.
    """

    model_config = _STRICT
    field: str
    column: str


class EligibilityConfigModel(BaseModel):
    """150 REQ-001: bloque top-level ``eligibility:``.

    Directiva de negocio: Content Manager no tiene espacio para todo RVABREP,
    así que sólo se migran los documentos de clientes **con producto activo**.
    El banco produce un CSV con el ``Shortname`` y el ``CIF`` de esos
    clientes; este bloque declara contra qué fuente se lo consulta.

    ``enabled: false`` (default) es **byte-equivalente a que el bloque no
    exista**: no se valida nada, no se evalúa nada y no se emite ninguna
    razón. Por eso la validación entera está detrás de la perilla — un bloque
    a medio escribir no puede romper una corrida que ni lo va a mirar.

    Prendida, en cambio, el YAML no puede mentir: sin ``source``, con
    ``match_any`` vacío, con un alias no declarado en ``metadata.sources`` o
    con un ``field`` que no existe en ``metadata.field_sources``, el error
    sale al CARGAR (las dos últimas las valida :class:`PipelineConfig`, que
    es quien ve los dos bloques).
    """

    model_config = _STRICT
    enabled: bool = False
    source: str | None = None
    match_any: tuple[EligibilityMatchModel, ...] = ()

    @model_validator(mode="after")
    def _enabled_requires_a_usable_list(self) -> EligibilityConfigModel:
        if not self.enabled:
            return self
        if self.source is None:
            raise ValueError(
                "eligibility.enabled is true but there is no `source`: "
                "there is no list to check the client against"
            )
        if split_lookup_source_type(self.source) is None:
            raise ValueError(
                f"eligibility.source {self.source!r} must be '<kind>:<alias>' "
                f"(one of {', '.join(LOOKUP_SOURCE_KINDS)}) against an alias "
                "declared in metadata.sources"
            )
        if not self.match_any:
            raise ValueError(
                "eligibility.enabled is true but `match_any` is empty: "
                "no criterion could ever mark a client as active"
            )
        return self


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
    # 129: objeto inline (alias implícito `as400`) o alias del registro.
    connection: As400ConnectionConfig | str | None = None
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
    # 129: registro de conexiones con alias. Las tres inserciones
    # (`indexing.source.connection`, `metadata.sources[].as400_connection`,
    # `tracking.as400_sync.connection`) pueden apuntar acá por alias.
    connections: dict[str, ConnectionConfig] = Field(default_factory=dict)
    trigger: TriggerConfigUnion
    indexing: IndexingConfig
    mapping: MappingConfig
    metadata: MetadataConfigModel
    # 147 REQ-002: bloque opcional. Ausente ⇒ comportamiento pre-147 (la
    # identidad se lee del trigger, sin cadena).
    identity: IdentityConfigModel = Field(default_factory=IdentityConfigModel)
    # 150 REQ-001: bloque opcional con perilla. Ausente —o con ``enabled:
    # false``— ⇒ comportamiento pre-150: no se evalúa elegibilidad y no se
    # emite ninguna razón.
    eligibility: EligibilityConfigModel = Field(default_factory=EligibilityConfigModel)
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

    @field_validator("connections")
    @classmethod
    def _check_aliases(
        cls, value: dict[str, AnyConnectionConfig]
    ) -> dict[str, AnyConnectionConfig]:
        for alias in value:
            if alias in RESERVED_CONNECTION_ALIASES:
                raise ValueError(f"connection alias {alias!r} is reserved")
            if not _CONNECTION_ALIAS_RE.fullmatch(alias):
                raise ValueError(
                    f"connection alias {alias!r} is invalid "
                    "(lowercase letter, then lowercase letters / digits / _, 32 chars max)"
                )
        return value

    @model_validator(mode="after")
    def _identity_fields_exist(self) -> PipelineConfig:
        """147 REQ-002: cada ``identity.<slot>.field`` tiene que ser una clave
        de ``metadata.field_sources``. El root model ve los dos bloques, así
        que el typo del operador muere al cargar el YAML."""
        known = self.metadata.field_sources
        for name, slot in self.identity.slots():
            if slot is not None and slot.field not in known:
                raise ValueError(
                    f"identity.{name}.field {slot.field!r} is not a key of "
                    f"metadata.field_sources (declared: {sorted(known) or 'none'})"
                )
        return self

    @model_validator(mode="after")
    def _eligibility_references_exist(self) -> PipelineConfig:
        """150 REQ-001: la lista de activos tiene que existir en el YAML.

        Un alias no declarado en ``metadata.sources``, un prefijo de kind que
        miente sobre esa fuente (mismo criterio que 130) o un ``field`` que no
        es clave de ``metadata.field_sources`` son errores de CONFIG, y el
        root model es el único que ve los dos bloques a la vez.

        Todo detrás de la perilla: con ``enabled: false`` el bloque es
        byte-equivalente a no existir (REQ-001).
        """
        cfg = self.eligibility
        if not cfg.enabled or cfg.source is None:
            return self
        lookup = split_lookup_source_type(cfg.source)
        if lookup is None:  # pragma: no cover — EligibilityConfigModel ya lo rechazó
            return self
        prefix, alias = lookup
        kinds = {source.alias: source.kind for source in self.metadata.sources}
        declared = kinds.get(alias)
        if declared is None:
            raise ValueError(
                f"eligibility.source {cfg.source!r} references alias {alias!r}, "
                f"which is not declared in metadata.sources "
                f"(declared: {sorted(kinds) or 'none'})"
            )
        if declared != prefix:
            raise ValueError(
                f"eligibility.source {cfg.source!r} but metadata.sources[{alias}] "
                f"is kind {declared!r}"
            )
        known = self.metadata.field_sources
        for index, match in enumerate(cfg.match_any):
            if match.field not in known:
                raise ValueError(
                    f"eligibility.match_any[{index}].field {match.field!r} is not a key "
                    f"of metadata.field_sources (declared: {sorted(known) or 'none'})"
                )
        return self

    @model_validator(mode="after")
    def _check_connection_references(self) -> PipelineConfig:
        # Resolver cada sitio valida existencia + kind; el resultado se tira.
        # `include_disabled`: un sync apagado con alias roto se rechaza igual.
        self.connection_refs(include_disabled=True)
        return self

    def connection_refs(self, *, include_disabled: bool = False) -> tuple[ConnectionRef, ...]:
        """129 — una entrada por sitio que USA una conexión, en orden de config.

        Única fuente de verdad de "qué conexiones necesita esta config":
        la consola, el doctor y `sync_ops` derivan de acá. Un
        ``tracking.as400_sync`` con ``enabled: false`` no cuenta (el
        pipeline jamás abre esa conexión) salvo ``include_disabled``, que
        existe sólo para validar el alias.
        """
        sites: list[tuple[str, AnyConnectionConfig | str, ConnectionKind, str, str | None]] = []
        source = self.indexing.source
        if isinstance(source, As400RvabrepSource):
            probe = as400_probe_sql(None, source.query)
            path = "indexing.source.connection"
            sites.append(("indexing", source.connection, "as400", path, probe))
        for meta_source in self.metadata.sources:
            if isinstance(meta_source, As400MetadataSourceConfig):
                sites.append(
                    (
                        f"metadata:{meta_source.alias}",
                        meta_source.as400_connection,
                        "as400",
                        f"metadata.sources[{meta_source.alias}].as400_connection",
                        as400_probe_sql(meta_source.table, meta_source.query),
                    )
                )
            elif isinstance(meta_source, MssqlMetadataSourceConfig):
                sites.append(
                    (
                        f"metadata:{meta_source.alias}",
                        meta_source.connection,
                        "mssql",
                        f"metadata.sources[{meta_source.alias}].connection",
                        mssql_probe_sql(meta_source.table, meta_source.query),
                    )
                )
        sync = self.tracking.as400_sync
        if sync.connection is not None and (sync.enabled or include_disabled):
            probe = as400_probe_sql(f"{sync.library}.{sync.table}", None)
            path = "tracking.as400_sync.connection"
            sites.append(("tracking.as400_sync", sync.connection, "as400", path, probe))
        return tuple(self._resolve_site(*site) for site in sites)

    def _resolve_site(
        self,
        site: str,
        value: AnyConnectionConfig | str,
        expected: ConnectionKind,
        path: str,
        probe_sql: str | None,
    ) -> ConnectionRef:
        if not isinstance(value, str):
            return ConnectionRef(INLINE_CONNECTION_ALIAS, value.kind, value, site, probe_sql)
        spec = self.connections.get(value)
        if spec is None:
            raise ValueError(
                f"{path}: unknown connection alias {value!r} "
                f"(declared: {sorted(self.connections) or 'none'})"
            )
        if spec.kind != expected:
            raise ValueError(
                f"{path}: connection {value!r} is kind {spec.kind!r}, "
                f"this site requires kind {expected!r}"
            )
        return ConnectionRef(value, spec.kind, spec, site, probe_sql)

    def required_aliases(self) -> tuple[str, ...]:
        """Aliases de conexión que la config usa, deduplicados en orden de aparición."""
        return tuple(dict.fromkeys(ref.alias for ref in self.connection_refs()))

    def connection_ref(self, site: str) -> ConnectionRef | None:
        """La conexión de un sitio concreto (``indexing`` / ``metadata:<alias>`` / ...)."""
        return next((ref for ref in self.connection_refs() if ref.site == site), None)
