"""Modelos de dominio — `dataclasses` `frozen`, fuente de verdad de las entidades
del core.

Solo Python standard library. Principio I de la Constitución: dominio tiene
cero dependencias externas. Cada `dataclass` es ``frozen=True, slots=True``
para que la mutación sea imposible y la memoria por instancia sea mínima a
escala.

Este módulo es dueño de los helpers ``parse_cymmdd``, ``is_pdf_filename``,
``compute_cm_folder`` y ``compute_cm_object_type`` porque están muy
acoplados a la semántica de los modelos.
"""

from __future__ import annotations

__all__ = [
    "RawResponse",
    "BatchDetails",
    "BatchInfo",
    "CMMapping",
    "ClientTrigger",
    "DocDetail",
    "FailedRecord",
    "LocalScanTrigger",
    "MigrationRecord",
    "ResolvedMetadata",
    "RVABREPDocument",
    "RvabrepRowTrigger",
    "StageStatus",
    "StagedFile",
    "Trigger",
    "TriggerRecord",
    "compute_cm_folder",
    "compute_cm_object_type",
    "is_pdf_filename",
    "parse_cymmdd",
]

import json
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def parse_cymmdd(date_str: str) -> datetime:
    """Parsea el formato de fecha de 7 dígitos ``CYYMMDD`` de AS400.

    ``C`` es el flag de siglo: ``0`` = 1900s, ``1`` = 2000s. ``YY`` es el
    año dentro del siglo, ``MM`` el mes y ``DD`` el día.

    >>> parse_cymmdd("1251117")
    datetime.datetime(2025, 11, 17, 0, 0)

    Lanza ``ValueError`` ante cualquier input que no matchee el formato o
    represente una fecha calendario inválida.
    """
    if not isinstance(date_str, str) or len(date_str) != 7 or not date_str.isdigit():
        raise ValueError(f"CYYMMDD requires exactly 7 digits, got {date_str!r}")
    century = int(date_str[0])
    year = (1900 + century * 100) + int(date_str[1:3])
    month = int(date_str[3:5])
    day = int(date_str[5:7])
    return datetime(year, month, day)  # lanza ValueError ante mes/día inválido


def is_pdf_filename(name: str) -> bool:
    """Devuelve ``True`` cuando *name* es un PDF nativo."""
    return name.upper().endswith(".PDF")


def compute_cm_folder(clase_id: str) -> str:
    """Calcula el path de carpeta `cmis` a partir de un ``clase_id`` del Modelo Documental."""
    return f"/$type/BAC_{clase_id.replace('.', '_')}"


def compute_cm_object_type(clase_id: str) -> str:
    """Calcula el ``cmis:objectTypeId`` a partir de un ``clase_id``."""
    return f"$t!-2_BAC_{clase_id.replace('.', '_')}v-1"


# ---------------------------------------------------------------------------
# Máquina de estados por etapa
# ---------------------------------------------------------------------------


class StageStatus(StrEnum):
    """Valores de la máquina de estados por etapa para el tracking store.

    Heredar de :class:`enum.StrEnum` (Python 3.11+) implica que cada
    miembro es su propio literal de string — ``StageStatus.S1_DONE ==
    "S1_DONE"`` y ``str(StageStatus.S1_DONE) == "S1_DONE"``. Las capas
    de persistencia guardan directamente el valor string del miembro en
    la columna SQL.
    """

    S1_PENDING = "S1_PENDING"
    S1_DONE = "S1_DONE"
    S1_FAILED = "S1_FAILED"
    # 062: estados terminales que exponen los resultados "no avanzó más
    # allá de S1, por un motivo no-falla". Se persisten en
    # ``migration_log`` para que la tab DETAIL, ``analyze batch`` y
    # ``cmcourier batch show`` puedan responder qué docs específicos
    # cayeron en cada `bucket` y por qué.
    S1_FILTERED = "S1_FILTERED"  # con código de borrado en la fuente (spec 051)
    S1_SKIPPED = "S1_SKIPPED"  # ya S5_DONE en un `batch` previo

    S2_PENDING = "S2_PENDING"
    S2_DONE = "S2_DONE"
    S2_FAILED = "S2_FAILED"

    S3_PENDING = "S3_PENDING"
    S3_DONE = "S3_DONE"
    S3_FAILED = "S3_FAILED"

    S4_PENDING = "S4_PENDING"
    S4_DONE = "S4_DONE"
    S4_FAILED = "S4_FAILED"

    S5_PENDING = "S5_PENDING"
    S5_DONE = "S5_DONE"
    S5_FAILED = "S5_FAILED"

    SKIPPED = "SKIPPED"

    @classmethod
    def terminal_for_stage(cls, stage: int) -> tuple[StageStatus, StageStatus]:
        """Devuelve ``(Sn_DONE, Sn_FAILED)`` para el número de etapa dado."""
        if not 1 <= stage <= 5:
            raise ValueError(f"stage must be in [1, 5], got {stage!r}")
        return (cls[f"S{stage}_DONE"], cls[f"S{stage}_FAILED"])


# ---------------------------------------------------------------------------
# Modelos
# ---------------------------------------------------------------------------


class Trigger(ABC):
    """Base polimórfica de todo lo que dispara un doc a través del `pipeline` (046).

    La forma de un `trigger` depende de qué dispara al doc, lo cual
    depende del tipo de `pipeline`:

    * ``ClientTrigger`` — una tupla de cliente (shortname, cif,
      system_id). S1 la expande a cada doc RVABREP del cliente. La usan
      los `pipeline`s csv-trigger y single-doc (modo csv).
    * ``RvabrepRowTrigger`` — una fila RVABREP única, ya conocida. S1 la
      envuelve en un solo ``RVABREPDocument`` sin re-querear. La usan
      los `pipeline`s rvabrep-direct y as400-trigger (el SQL/scan ya
      entregó la fila).
    * ``LocalScanTrigger`` — un archivo en disco + la fila RVABREP que
      lo describe. S1 emite un único ``RVABREPDocument`` para ese
      archivo exacto. La usa el `pipeline` local-scan (modo local_scan).

    Cada subtipo concreto implementa ``audit_row()`` para producir, en
    base a `best-effort`, strings ``{shortname, cif, system_id}`` para
    las columnas trigger_* del migration_log — esas columnas son audit
    legible solo por el operador; la identidad canónica por documento
    es ``rvabrep_txn_num`` sobre el ``RVABREPDocument`` resultante.
    """

    __slots__ = ()

    @abstractmethod
    def audit_row(self) -> dict[str, str | None]:
        """Proyección `best-effort` a {shortname, cif, system_id} para tracking."""


# Nombres físicos de columnas RVABREP — se usan como defaults de
# ``RvabrepRowTrigger`` / ``LocalScanTrigger`` para que el camino AS400 de
# producción "simplemente funcione". Las estrategias que leen CSVs con
# nombres de columna amigables (típico en tests + fixtures de integración)
# pisan los defaults en construcción. Matchea los defaults de
# ``RvabrepColumnsConfig``. Vive en dominio (no en servicios) porque la
# proyección de audit-row es preocupación de dominio; las estrategias
# pasan sus nombres de columna pero no son dueñas del lookup.
_DEFAULT_COL_SHORTNAME = "ABABCD"
_DEFAULT_COL_CIF = "ABACCD"
_DEFAULT_COL_SYSTEM_ID = "ABAACD"


def _read_normalized(row: Mapping[str, Any], key: str) -> str | None:
    v = row.get(key)
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _project_audit_from_row(
    row: Mapping[str, Any],
    *,
    col_shortname: str,
    col_cif: str,
    col_system_id: str,
) -> dict[str, str | None]:
    """Extrae la terna de audit de una fila, normalizando blancos a None."""
    return {
        "shortname": _read_normalized(row, col_shortname),
        "cif": _read_normalized(row, col_cif),
        "system_id": _read_normalized(row, col_system_id),
    }


@dataclass(frozen=True, slots=True)
class ClientTrigger(Trigger):
    """Una fila de una lista de `trigger`s de cliente (pre-046 ``TriggerRecord``).

    ``cif`` puede ser ``None`` para soportar la regla de self-healing
    del CIF: en modos donde la fuente de `trigger` no provee un CIF, la
    capa de metadata lo resuelve desde RVABREP y escribe de vuelta el
    valor resuelto.
    """

    shortname: str
    cif: str | None
    system_id: str

    def __post_init__(self) -> None:
        if not self.shortname:
            raise ValueError("ClientTrigger.shortname must be non-empty")
        if not self.system_id:
            raise ValueError("ClientTrigger.system_id must be non-empty")

    def audit_row(self) -> dict[str, str | None]:
        return {"shortname": self.shortname, "cif": self.cif, "system_id": self.system_id}


@dataclass(frozen=True, slots=True)
class RvabrepRowTrigger(Trigger):
    """Una fila RVABREP ya conocida (rvabrep-direct + as400-trigger).

    La fila lleva cada columna que las etapas downstream necesitan; S1
    la envuelve en un único ``RVABREPDocument`` sin re-querear RVABREP.
    Los campos ``col_*`` le dicen a ``audit_row()`` qué claves de fila
    tienen la terna de audit — los defaults son los nombres físicos
    AS400, las estrategias que leen CSVs con nombres de columna
    amigables los pisan en construcción.
    """

    row: Mapping[str, Any]
    col_shortname: str = _DEFAULT_COL_SHORTNAME
    col_cif: str = _DEFAULT_COL_CIF
    col_system_id: str = _DEFAULT_COL_SYSTEM_ID

    def audit_row(self) -> dict[str, str | None]:
        return _project_audit_from_row(
            self.row,
            col_shortname=self.col_shortname,
            col_cif=self.col_cif,
            col_system_id=self.col_system_id,
        )


@dataclass(frozen=True, slots=True)
class LocalScanTrigger(Trigger):
    """Un archivo escaneado + la fila RVABREP que lo describe (local_scan).

    Crucial: ``row`` es la entrada RVABREP cuyo ``ABAJCD`` matcheó el
    nombre del archivo escaneado. S1 produce exactamente un
    ``RVABREPDocument`` para este archivo, sin importar cuántos otros
    docs tenga el mismo cliente — el operador que dropeó el archivo en
    ``scan_path`` quería migrar ESE archivo, no cada doc de su cliente.
    Los campos ``col_*`` cargan el mapa de nombres de columna para
    ``audit_row()`` (ver ``RvabrepRowTrigger``).
    """

    file_path: Path
    row: Mapping[str, Any]
    col_shortname: str = _DEFAULT_COL_SHORTNAME
    col_cif: str = _DEFAULT_COL_CIF
    col_system_id: str = _DEFAULT_COL_SYSTEM_ID

    def audit_row(self) -> dict[str, str | None]:
        return _project_audit_from_row(
            self.row,
            col_shortname=self.col_shortname,
            col_cif=self.col_cif,
            col_system_id=self.col_system_id,
        )


# 046 — alias de backward-compat. Cada import pre-046 de ``TriggerRecord``
# (estrategia csv-trigger, CLI single-doc, tests, `adapter` de tracking, …)
# sigue resolviendo al mismo `dataclass` concreto — ahora llamado
# ``ClientTrigger`` para reflejar su forma semántica, pero idéntico en
# tipo para chequeos ``isinstance``.
TriggerRecord = ClientTrigger


@dataclass(frozen=True, slots=True)
class RVABREPDocument:
    """Una fila del índice maestro RVABREP de AS400."""

    system_code: str
    txn_num: str
    index1: str
    index2: str
    index3: str
    index4: str
    index5: str
    index6: str
    index7: str
    image_type: str
    image_path: str
    file_name: str
    creation_date: datetime
    last_view_date: datetime | None
    total_pages: int
    delete_code: str

    @property
    def is_pdf(self) -> bool:
        """Devuelve ``True`` si este documento es un PDF nativo."""
        return is_pdf_filename(self.file_name)

    @property
    def is_deleted(self) -> bool:
        """Devuelve ``True`` si la fila fue marcada como borrada."""
        return bool(self.delete_code)


@dataclass(frozen=True, slots=True)
class CMMapping:
    """Una fila del Modelo Documental — tipo RVI a clase CM.

    ``cmis_type`` (034) es el código de Tipo `cmis` que mapea a
    ``NIARVILOG.TIPIDN`` de AS400. Default a ``""`` hasta que el cambio
    035 divide el CSV de mapping en ``MapeoRVI_CM.csv`` +
    ``MetadatosCM.csv`` con una columna ``CMISType`` explícita.

    ``cmis_folder`` (038) es el path explícito de carpeta `cmis`.
    Cuando está seteado, pisa la property derivada ``cm_folder`` al
    momento del upload. Cuando es ``None`` (columna en blanco o
    ausente), los `pipeline`s caen al fallback ``cm_folder``.

    ``cmis_property_ids`` (038) mapea nombres amigables de campos de
    metadata (como aparecen en ``MetadatosCM.Metadato``) a sus
    identificadores `cmis` a nivel de wire (``MetadatosCM.CMISPropertyId``).
    ``None`` significa "sin catálogo" — el servicio de metadata mantiene
    los nombres amigables / canónicos como claves de property,
    preservando el comportamiento pre-038.
    """

    clase_id: str
    id_rvi: str
    id_corto: str
    clase_name: str
    required_metadata_fields: tuple[str, ...]
    cmis_type: str = ""
    cmis_folder: str | None = None
    cmis_property_ids: Mapping[str, str] | None = None

    @property
    def cm_folder(self) -> str:
        """La carpeta `cmis` donde se suben los documentos de esta clase."""
        return compute_cm_folder(self.clase_id)

    @property
    def cm_object_type(self) -> str:
        """El ``cmis:objectTypeId`` para los documentos de esta clase."""
        return compute_cm_object_type(self.clase_id)


@dataclass(frozen=True, slots=True)
class ResolvedMetadata:
    """`Snapshot` solo lectura de propiedades BAC_* resueltas para un documento.

    Construir vía :meth:`from_dict`. El storage interno es un
    ``MappingProxyType`` sobre una copia, así que mutar el dict fuente
    no puede corromper el `snapshot` y la vista ``properties`` lanza
    ``TypeError`` ante asignación por item.
    """

    properties: Mapping[str, str]

    @classmethod
    def from_dict(cls, d: Mapping[str, str]) -> ResolvedMetadata:
        return cls(properties=MappingProxyType(dict(d)))

    def __getitem__(self, key: str) -> str:
        return self.properties[key]

    def __contains__(self, key: object) -> bool:
        return key in self.properties

    def __iter__(self) -> Iterator[str]:
        return iter(self.properties)

    def __len__(self) -> int:
        return len(self.properties)


def _document_object_id(object_id: object, base_type_id: object) -> str | None:
    """141 B3: sólo strings no vacíos, y sólo si el ``baseTypeId`` (cuando
    viene) es ``cmis:document`` — nunca un id de carpeta."""
    if base_type_id is not None and base_type_id != "cmis:document":
        return None
    if isinstance(object_id, bool) or not object_id:
        return None
    return str(object_id)


@dataclass(frozen=True, slots=True)
class RawResponse:
    """La respuesta del servidor SIN maquillaje.

    ``body`` viaja COMPLETO (el pipeline lo trunca a 1024 caracteres;
    acá el punto es justamente leerlo entero) y ``curl`` es el comando
    equivalente con la contraseña enmascarada, para pegar en una
    terminal y seguir investigando.
    """

    status_code: int
    reason: str
    headers: Mapping[str, str]
    body: str
    elapsed_ms: int
    curl: str

    @property
    def ok(self) -> bool:
        """``True`` sólo para 2xx — un 302 no es un upload exitoso."""
        return 200 <= self.status_code < 300

    @property
    def object_id(self) -> str | None:
        """El ``cmis:objectId`` parseado del cuerpo, o ``None``.

        141 antagonista B3: sin ``ok`` no hay documento que borrar — un
        4xx/5xx nunca parsea un id, aunque el cuerpo traiga uno (Alfresco
        devuelve el id de la CARPETA destino en varios errores, no el del
        documento). Solo dos rutas válidas: ``succinctProperties`` o
        ``properties.cmis:objectId.value`` — el fallback a ``data["id"]``
        del parser del `pipeline` se eliminó acá porque ese campo es
        genérico y no garantiza ser un ``cmis:objectId``. Cuando el cuerpo
        trae ``cmis:baseTypeId``, tiene que valer ``"cmis:document"`` — un
        id de carpeta no sirve para borrar el tiro de prueba.
        """
        if not self.ok:
            return None
        try:
            data = json.loads(self.body)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        succinct = data.get("succinctProperties")
        if isinstance(succinct, dict):
            return _document_object_id(
                succinct.get("cmis:objectId"), succinct.get("cmis:baseTypeId")
            )
        properties = data.get("properties")
        if isinstance(properties, dict):
            obj = properties.get("cmis:objectId")
            obj_id = obj.get("value") if isinstance(obj, dict) else None
            base = properties.get("cmis:baseTypeId")
            base_value = base.get("value") if isinstance(base, dict) else base
            return _document_object_id(obj_id, base_value)
        return None


@dataclass(frozen=True, slots=True)
class StagedFile:
    """La salida de la etapa S4 — un archivo ensamblado listo para el upload."""

    path: Path
    size_bytes: int
    page_count: int

    def __post_init__(self) -> None:
        if self.size_bytes < 0:
            raise ValueError("StagedFile.size_bytes must be >= 0")
        if self.page_count < 0:
            raise ValueError("StagedFile.page_count must be >= 0")


@dataclass(frozen=True, slots=True)
class MigrationRecord:
    """Una fila del tracking store.

    Los campos requeridos capturan la identidad del intento de
    migración y su estado actual. Los campos opcionales se completan a
    medida que el documento atraviesa etapas.

    ``created_at`` es REQUERIDO y lo provee la capa de persistencia
    (sabe cuándo se insertó la fila). Deliberadamente no se usa
    ``default_factory=datetime.now`` para que los tests puedan pasar
    valores determinísticos.
    """

    trigger_shortname: str
    trigger_cif: str
    trigger_system_id: str
    rvabrep_txn_num: str
    rvabrep_file_name: str
    batch_id: str
    status: StageStatus
    created_at: datetime

    cm_object_id: str | None = None
    cm_folder: str | None = None
    cm_object_type: str | None = None
    error_message: str | None = None
    source_file_path: str | None = None
    page_count: int | None = None
    file_size_bytes: int | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    retry_count: int = 0


# ---------------------------------------------------------------------------
# Resúmenes de `batch` (superficie de CLI para el operador, cambio 021)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BatchInfo:
    """Una fila de ``migration_batch`` más un estado derivado.

    ``status`` es ``'completed'`` una vez que ``completed_at`` no es
    null, ``'in_progress'`` en caso contrario. Calculado, no
    almacenado — mantiene el schema SQLite sin cambios.
    """

    batch_id: str
    started_at: datetime
    completed_at: datetime | None
    total_records: int

    @property
    def status(self) -> str:
        return "completed" if self.completed_at is not None else "in_progress"


@dataclass(frozen=True, slots=True)
class FailedRecord:
    """Una fila ``*_FAILED`` de ``migration_log``.

    La usa ``batch show`` para exponer qué bloqueó la progresión.
    """

    txn_num: str
    status: str
    error_message: str


@dataclass(frozen=True, slots=True)
class BatchDetails:
    """Estado agregado de un único `batch`.

    ``stage_counts`` siempre contiene las claves ``S0..S5``; el dict
    interno tiene las claves ``DONE / FAILED / PENDING`` con conteos
    enteros (cero para combos faltantes). La forma predecible le
    permite a la CLI renderizar una tabla estable sin importar el
    progreso del `batch`.
    """

    info: BatchInfo
    stage_counts: Mapping[str, Mapping[str, int]]
    failed_records: tuple[FailedRecord, ...]


@dataclass(frozen=True, slots=True)
class DocDetail:
    """Una fila de ``migration_log``, proyectada para el `drill-down`
    por `chunk` del TUI (052).

    ``status`` es el valor crudo ``Sn_DONE / Sn_FAILED / Sn_PENDING``.
    ``error_message`` es la razón de fallo/skip ("" cuando no hay).
    Leído del tracking store bajo demanda — nunca se mantiene en
    memoria para cada `chunk` (eso desharía la garantía de memoria
    acotada de la spec 050).
    """

    txn_num: str
    file_name: str
    status: str
    error_message: str
    file_size_bytes: int
