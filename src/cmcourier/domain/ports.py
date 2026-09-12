"""Interfaces abstractas (`port`s) implementadas por los `adapter`s desde el cambio 003 en adelante.

Principio I de la Constitución: aquí solo se pueden importar la standard
library y ``cmcourier.domain``. Nada de ``pydantic``, ``requests`` ni
``pyodbc``.

Las implementaciones concretas viven en ``cmcourier.adapters.*`` (fuentes
de datos, tracking, ensamblado, upload) y en las implementaciones de
estrategia para la etapa S0 (``cmcourier.adapters.sources`` para
CSV / AS400, más una estrategia de folder-scan en el `pipeline` de
local-scan).
"""

from __future__ import annotations

__all__ = [
    "PracticeUploadPort",
    "CacheEntry",
    "CacheKey",
    "CacheStats",
    "IAssembler",
    "IDataSource",
    "IDocumentCache",
    "ITrackingStore",
    "IUploader",
    "S0Strategy",
]

from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from cmcourier.domain.models import (
    BatchDetails,
    BatchInfo,
    DocDetail,
    MigrationRecord,
    RawResponse,
    ReasonCode,
    RVABREPDocument,
    StagedFile,
    StageStatus,
    Trigger,
)

# ---------------------------------------------------------------------------
# IDataSource — abstracción genérica de fuente de datos (CSV, AS400, …)
# ---------------------------------------------------------------------------


class IDataSource(ABC):
    """Fuente de datos genérica. Las subclases concretas envuelven archivos
    CSV, conexiones ODBC a AS400 u otras fuentes y exponen una API de query
    uniforme.

    Los valores de fila están tipados como ``Any`` porque las fuentes de
    datos devuelven primitivos heterogéneos (str, int, datetime, Decimal,
    bytes, None). Los callers convierten las filas en modelos de dominio
    tipados antes de pasarlas a los servicios.
    """

    @abstractmethod
    def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        """Ejecuta una query y devuelve todas las filas como dicts. Materializa el resultado."""

    @abstractmethod
    def query_stream(self, sql: str, params: list[Any] | None = None) -> Iterator[dict[str, Any]]:
        """Ejecuta una query y stremea las filas de forma lazy. Usar para `result set`s grandes."""

    @abstractmethod
    def get_by_fields(self, filters: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Trae filas que matcheen igualdad ``WHERE`` sobre los campos dados."""

    @abstractmethod
    def get_by_fields_in(
        self,
        field: str,
        values: list[Any],
        fixed_filters: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """Trae filas donde *field* IN *values* Y cada *fixed_filter* matchee.

        Separar la lista IN de los filtros de igualdad fijos le permite al
        `adapter` chunkear la cláusula IN de manera eficiente (`batch`es
        de 50).
        """

    @abstractmethod
    def stream_by_fields_in(
        self,
        field: str,
        values: list[Any],
        fixed_filters: Mapping[str, Any],
    ) -> Iterator[dict[str, Any]]:
        """148 REQ-001: igual que :meth:`get_by_fields_in`, pero LAZY.

        Mismo filtro, mismo chunkeo de la lista ``IN``; lo que cambia es
        que las filas salen de a poco en vez de materializarse enteras.

        No es una optimización opcional: con el censo siempre activo,
        ``filters.systems: ["1"]`` es la configuración NORMAL del modo
        ``direct_rvabrep`` y trae el sistema ENTERO. Una lista de Python
        con un sistema adentro no es aceptable, así que el escaneo de S0
        usa este método y no el materializado.

        Contrato: NO ejecuta nada hasta el primer ``next()`` (generator),
        y una lista de *values* vacía no emite filas ni toca la fuente.
        """

    @abstractmethod
    def get_all(self) -> Iterator[dict[str, Any]]:
        """Stremea cada fila de la fuente subyacente. Lo usa el pre-fetch de metadata."""

    @abstractmethod
    def count(self) -> int:
        """Devuelve el conteo total de filas de la fuente subyacente."""

    @abstractmethod
    def close(self) -> None:
        """Libera cualquier recurso (`cursor`s, conexiones, `file handle`s)."""


# ---------------------------------------------------------------------------
# ITrackingStore — `idempotency` + estado por etapa
# ---------------------------------------------------------------------------


class ITrackingStore(ABC):
    """Contrato del tracking store.

    Dos capas de estado:

    1. **`Idempotency` cross-`batch`**: ``is_uploaded(txn_num)`` responde
       "¿este documento ya fue subido con éxito alguna vez?". La respuesta
       gobierna el comportamiento de "saltar lo ya subido" al inicio de
       cualquier corrida del `pipeline`.
    2. **Máquina de estados por `batch` y por etapa**: ``Sn_PENDING /
       Sn_DONE / Sn_FAILED`` para el `batch` actual. Gobierna la semántica
       de resume / ejecución etapa-por-etapa para `batch`es resumibles.

    Las fallas de tracking (lanzadas por cualquiera de estos métodos) son
    no bloqueantes según el contrato de la etapa S6 — las
    implementaciones loguean y convierten a ``TrackingError`` pero el
    `pipeline` continúa.
    """

    @abstractmethod
    def is_uploaded(self, txn_num: str) -> bool:
        """Anchor de `idempotency` cross-`batch`. Devuelve True solo cuando
        el estado terminal del documento es ``S5_DONE``."""

    @abstractmethod
    def is_stage_done(self, txn_num: str, batch_id: str, stage: StageStatus) -> bool:
        """Chequeo por `batch` y por etapa. ``stage`` DEBE ser un valor ``Sn_DONE``."""

    @abstractmethod
    def mark_stage_pending(self, record: MigrationRecord, stage: StageStatus) -> None:
        """Inserta / actualiza la fila para *record* en ``Sn_PENDING``."""

    @abstractmethod
    def mark_stage_done(
        self,
        txn_num: str,
        batch_id: str,
        stage: StageStatus,
        *,
        cm_object_id: str | None = None,
    ) -> None:
        """Transiciona la fila de *txn_num* en *batch_id* a ``Sn_DONE``.

        047: ``cm_object_id`` es el objectId CMIS que devuelve el
        uploader. Solo la transición S5_DONE lo trae; los callers de
        S1..S4 no pasan nada y la columna queda intacta. Cuando es
        ``None``, la implementación NO DEBE escribir la columna (para
        que sobreviva un valor previo, si lo hubiera).
        """

    @abstractmethod
    def mark_stage_failed(
        self,
        txn_num: str,
        batch_id: str,
        stage: StageStatus,
        error: str,
        *,
        reason_code: ReasonCode | None = None,
    ) -> None:
        """Transiciona la fila de *txn_num* en *batch_id* a ``Sn_FAILED`` y
        guarda el mensaje de error legible.

        148 REQ-004: ``reason_code`` es el eje ORTOGONAL al status — el
        status dice dónde paró, el código dice por qué, y el balde
        (:class:`ReasonBucket`) dice quién lo arregla. Opcional: cuando
        es ``None`` la implementación NO DEBE tocar la columna, para que
        sobreviva una razón escrita antes.
        """

    @abstractmethod
    def mark_stage_terminal(
        self,
        txn_num: str,
        batch_id: str,
        stage: StageStatus,
        error_message: str,
        *,
        reason_code: ReasonCode | None = None,
    ) -> None:
        """062: transición terminal que NO es una falla.

        Se usa para los resultados sin error que antes no tenían fila en
        ``migration_log``: ``S1_FILTERED`` (con código de borrado en la
        fuente — spec 051) y ``S1_SKIPPED`` (ya ``S5_DONE`` en un
        `batch` previo — `idempotency` cross-`batch`).

        Distinta de :meth:`mark_stage_failed`:

        * Acepta cualquier etapa terminal (``*_FAILED``, ``*_FILTERED``,
          ``*_SKIPPED``).
        * **NO** incrementa ``retry_count`` — el doc no "falló", terminó
          su recorrido aquí por un motivo no-error.
        * Setea ``completed_at``.

        148 REQ-004: acepta ``reason_code`` con la misma semántica que
        :meth:`mark_stage_failed` — opcional, y ``None`` deja la columna
        intacta.
        """

    @abstractmethod
    def record_staged_file_metadata(
        self,
        txn_num: str,
        batch_id: str,
        *,
        source_file_path: str,
        page_count: int,
        file_size_bytes: int,
    ) -> None:
        """058: persiste la metadata del staged-file una vez que S4 tiene éxito.

        La metadata (``source_file_path``, ``page_count``,
        ``file_size_bytes``) es desconocida cuando S1 hace por primera
        vez el INSERT-OR-IGNORE de la fila — ``item.staged_file`` es
        ``None`` hasta que S4 ensambla el documento. `mark_stage_pending`
        no puede rellenarla retroactivamente porque la fila ya existe.
        Este método UPDATEa la fila existente con la salida del
        `assembler`. `Idempotent` — llamarlo dos veces con los mismos
        valores es un no-op.
        """

    @abstractmethod
    def start_batch(self, total_records: int) -> str:
        """Crea un nuevo `batch` y devuelve su identificador.

        148: ``total_records`` acá es apenas una SEMILLA — los callers no
        conocen el conteo del origen en este punto (streaming pasa ``0``,
        staged pasa el ``batch_size`` configurado). El denominador real
        lo escriben :meth:`increment_source_total` /
        :meth:`set_source_total` a medida que S1 ve los documentos.
        """

    @abstractmethod
    def complete_batch(self, batch_id: str) -> None:
        """Marca el `batch` como cerrado (no se agregarán más filas)."""

    @abstractmethod
    def increment_source_total(self, batch_id: str, delta: int) -> None:
        """148 REQ-005: suma *delta* documentos del origen al denominador.

        Es el camino NORMAL, porque el total no se conoce de antemano: S1
        ve los documentos de a chunks y streaming nunca sabe cuántos hay.
        Es el número contra el que todo lo demás tiene que sumar.

        Idempotente NO es: cada llamada suma. Un ``batch_id`` desconocido
        DEBE ser un no-op, NO lanzar.
        """

    @abstractmethod
    def set_source_total(self, batch_id: str, total: int) -> None:
        """148 REQ-005: fija el denominador en un valor absoluto.

        Para sembrar en ``0`` un `batch` cuyo ``start_batch`` escribió un
        valor que no es un conteo, y para los caminos donde el total del
        origen se conoce exacto de una. Un ``batch_id`` desconocido DEBE
        ser un no-op, NO lanzar.
        """

    @abstractmethod
    def list_txn_nums_for_batch(self, batch_id: str) -> set[str]:
        """Devuelve cada ``rvabrep_txn_num`` actualmente trackeado bajo *batch_id*.

        Lo usan los `orchestrator`s para acotar las corridas de resume:
        re-correr S0+S1 puede emitir documentos que no existían en el
        `batch` previo (p. ej., cambió el CSV de `trigger`s). El
        `orchestrator` filtra la salida fresca de S1 por este set, así
        solo se procesan los docs que pertenecen al `batch` previo.

        Un ``batch_id`` desconocido DEBE devolver un set vacío, NO lanzar.
        """

    @abstractmethod
    def flush(self) -> None:
        """Bloquea hasta que las escrituras pendientes estén durables en disco.

        Los `orchestrator`s lo invocan antes de cualquier lectura que
        dependa de escrituras de la misma corrida (el anchor de "leer
        mis propias escrituras"). Las implementaciones síncronas PUEDEN
        implementarlo como no-op.
        """

    @abstractmethod
    def close(self) -> None:
        """Libera cualquier recurso (`writer thread`, `cursor`s, `file handle`s)."""

    # -------------------------------------------------- de cara al operador (021)

    @abstractmethod
    def list_batches(
        self,
        status: Literal["in_progress", "completed"] | None = None,
    ) -> list[BatchInfo]:
        """Enumera los `batch`es, opcionalmente filtrado por estado de completitud.

        La lista devuelta está ordenada por ``started_at`` DESC. Vacía
        cuando no hay `batch`es registrados. La usa
        ``cmcourier batch list``.
        """

    @abstractmethod
    def get_batch_details(self, batch_id: str) -> BatchDetails | None:
        """Agrega conteos por etapa + registros fallidos para un `batch`.

        Devuelve ``None`` para un ``batch_id`` desconocido. La usa
        ``cmcourier batch show``.
        """

    @abstractmethod
    def list_docs_for_batch(self, batch_id: str) -> list[DocDetail]:
        """Detalle por documento de un `batch` — un :class:`DocDetail`
        por fila de ``migration_log``, ordenado por ``rvabrep_txn_num``.

        Alimenta el `drill-down` por `chunk` del TUI (052): el operador
        elige un `chunk` y ve nombre, tamaño, estado y razón de
        fallo/skip de cada doc. Leer desde el store mantiene la memoria
        acotada — el detalle nunca se mantiene en RAM para cada `chunk`.

        Un ``batch_id`` desconocido DEBE devolver una lista vacía, NO lanzar.
        """

    @abstractmethod
    def retry_failed(
        self,
        batch_id: str,
        stage: StageStatus | None = None,
    ) -> int:
        """Resetea las filas ``*_FAILED`` de ``batch_id`` de vuelta a ``*_PENDING``.

        Cuando ``stage`` es None, se resetean TODAS las etapas
        fallidas. Cuando ``stage`` es un valor ``Sn_FAILED``, solo se
        resetea esa etapa. Devuelve la cantidad de filas tocadas.
        `Idempotent`: un `batch` limpio devuelve 0. La usa
        ``cmcourier batch retry-failed``.

        150: **NUNCA toca una fila cuyo ``reason_code`` esté en el balde
        ``EXCLUIDO``**, sin importar su ``status``. El discriminador es el
        balde, no el estado: por el eje ortogonal de 148 una exclusión puede
        vivir en una fila ``*_FAILED`` (``CLIENT_NOT_ACTIVE`` es
        ``S2_FAILED``), y reintentar una decisión de negocio no tiene
        sentido — no va a cambiar de opinión, y con la lista de activos real
        del operador serían cientos de miles de documentos re-procesados,
        cada uno pagando otra vez la cadena completa de identidad.
        ``BLOQUEADO`` y ``FALLO`` se reintentan como siempre.
        """


# ---------------------------------------------------------------------------
# IAssembler — etapa S4
# ---------------------------------------------------------------------------


class IAssembler(ABC):
    """Ensambla un documento multi-página en un único PDF `staged` en disco."""

    @abstractmethod
    def assemble(self, document: RVABREPDocument) -> StagedFile:
        """Verifica que los archivos fuente existan y produce un PDF ensamblado.

        Lanza ``SourceFileMissingError`` si falta un archivo de página, y
        ``PDFAssemblyFailedError`` si el tooling subyacente falla.
        """


# ---------------------------------------------------------------------------
# IUploader — etapa S5
# ---------------------------------------------------------------------------


class IUploader(ABC):
    """Sube un archivo `staged` a IBM Content Manager vía `cmis`."""

    @abstractmethod
    def verify_folder_exists(self, folder_path: str) -> bool:
        """Devuelve ``True`` sii *folder_path* existe en el server CM Y
        su ``cmis:baseTypeId`` es ``cmis:folder``.

        Solo lectura — nunca crea la carpeta. CMCourier solamente
        deposita documentos; el árbol de carpetas destino lo gobierna el
        administrador `cmis`. Lo usa el paso de `pre-flight`
        ``doctor --check cm-targets`` (038) para fallar fuerte antes de
        que S5 intente un upload.

        Devuelve ``False`` ante 404 o cuando el path existe pero
        resuelve a un objeto no-folder (un documento, un item, etc.).
        Lanza solo ante fallas de conectividad / autenticación
        (``CMISClientError`` para 401/403, ``CMISServerError`` para 5xx).
        """

    @abstractmethod
    def upload(
        self,
        file: StagedFile,
        folder_path: str,
        object_type_id: str,
        document_name: str,
        mime_type: str,
        properties: Mapping[str, str],
        *,
        batch_id: str,
    ) -> str:
        """Sube *file* y devuelve el ``cmis:objectId`` resultante.

        ``batch_id`` etiqueta cada evento de red emitido durante el
        upload para que los `handler`s de ancho de banda y operaciones
        lentas por `batch` lo atribuyan al `chunk` correcto. Requerido
        — un uploader compartido sirve a múltiples `chunk`s en
        simultáneo, así que el id tiene que viajar con la llamada.

        Lanza ``CMISClientError`` para HTTP 4xx (no hacer `retry`) y
        ``CMISServerError`` para HTTP 5xx (el caller puede hacer `retry`).
        """

    @abstractmethod
    def test_connection(self) -> Mapping[str, str]:
        """Verifica que el endpoint CM sea alcanzable y que las credenciales
        sean válidas. Devuelve un dict con info del `repository` para
        diagnósticos."""

    @abstractmethod
    def get_type_definition(self, object_type_id: str) -> Mapping[str, Any]:
        """Devuelve la `typeDefinition` `cmis` para *object_type_id*.

        Lo usa el comando ``doctor`` de `pre-flight` para verificar que
        cada ``cm_object_type`` referenciado por el Modelo Documental
        exista en el server CM. Pasa por encima de cualquier política
        de `retry` — `pre-flight` prefiere fallar fuerte antes que
        reintentar en silencio.

        Lanza:
            CMISClientError: 4xx (típicamente 404 para tipos faltantes).
            CMISServerError: 5xx.
        """

    @abstractmethod
    def get_type_descendants(
        self, include_property_definitions: bool = True
    ) -> Sequence[Mapping[str, Any]]:
        """145: devuelve el árbol completo de tipos del `repository`.

        Es el ``cmisselector=typeDescendants`` con ``depth=-1``: la lista
        de nodos ``{"type": {...}, "children": [...]}`` con la que
        ``types discover`` (145 REQ-003) arma el manifest de clases
        documentales. Con ``include_property_definitions=False`` el
        server manda sólo la cabecera de cada tipo — mucho más liviano
        cuando alcanza con saber qué tipos existen.

        Como ``get_type_definition``, pasa por encima de cualquier
        política de `retry`: descubrir es una operación de operador, no
        del `pipeline`.

        Lanza:
            CMISClientError: 4xx.
            CMISServerError: 5xx.
        """

    @abstractmethod
    def set_credentials(self, username: str, password: str) -> None:
        """132: reemplaza las credenciales en caliente.

        La sesión vigente se descarta y la próxima operación re-hace el
        `warmup` con las credenciales nuevas. Thread-safe: la llama el
        thread de la consola mientras los workers están pausados o en
        vuelo."""


# ---------------------------------------------------------------------------
# S0Strategy — etapa S0
# ---------------------------------------------------------------------------


class S0Strategy(ABC):
    """Estrategia de la etapa S0: convierte un descriptor de fuente en un
    `stream` de `trigger`s.

    Los tipos soportados de `trigger` mapean cada uno a una subclase
    concreta:

    * ``CsvTriggerStrategy`` — lee un CSV con lista de `trigger`s (tuplas
      de cliente).
    * ``DirectRvabrepTriggerStrategy`` — descubre `trigger`s escaneando
      directamente la fuente RVABREP (un `trigger` por fila). La fuente
      RVABREP es pluggable (CSV ↔ AS400, 048) — "AS400" es una elección
      de fuente, no un tipo de `trigger` separado.
    * ``LocalScanTriggerStrategy`` — escanea una carpeta en busca de
      archivos, cruzando contra RVABREP para encontrar la fila que matchee.
    * ``SingleDocTriggerStrategy`` — emite un único `trigger` a partir de
      args de CLI.
    """

    @abstractmethod
    def acquire(self, source_descriptor: str) -> Iterator[Trigger]:
        """Emite registros de `trigger` de forma lazy. Las listas de `trigger`s
        pueden ser enormes (200k+); los callers iteran, nunca materializan.

        046: el tipo de retorno es la ABC polimórfica ``Trigger``. Cada
        estrategia concreta emite el subtipo que matchea la semántica
        de su fuente (``ClientTrigger`` para csv/single-doc,
        ``RvabrepRowTrigger`` para rvabrep-direct/as400,
        ``LocalScanTrigger`` para local-scan). El `enrichment` de S1
        hace dispatch por subtipo.
        """


# ---------------------------------------------------------------------------
# IDocumentCache — cache de metadata S3 cross-`batch` (POST-MVP §9, 037)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CacheKey:
    """Identifica una entrada del cache. ``fields_hash`` es el SHA-256 hex
    de la lista ordenada de campos requeridos — la evolución del mapping
    invalida por construcción."""

    txn_num: str
    fields_hash: str


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """Una fila almacenada en ``document_cache``."""

    txn_num: str
    fields_hash: str
    trigger_cif: str | None
    properties: Mapping[str, str]
    cached_at: datetime


@dataclass(frozen=True, slots=True)
class CacheStats:
    """`Snapshot` del estado de la tabla de cache en un instante dado."""

    total_rows: int
    oldest_cached_at: datetime | None
    newest_cached_at: datetime | None


class IDocumentCache(ABC):
    """Cache de metadata cross-`batch`. Implementación SQLite en la Fase 1 de 037."""

    @abstractmethod
    def get(self, key: CacheKey) -> CacheEntry | None:
        """Devuelve la entrada o ``None``. El TTL lo chequea el servicio."""

    @abstractmethod
    def put(self, entry: CacheEntry) -> None:
        """`Upsert`. Reemplaza una entrada con el mismo ``(txn_num, fields_hash)``."""

    @abstractmethod
    def clear_txn(self, txn_num: str) -> int:
        """Borra cada fila que matchee ``txn_num`` (cualquier fields hash).

        Devuelve la cantidad de filas borradas.
        """

    @abstractmethod
    def clear_all(self) -> int:
        """Truncate. Devuelve filas borradas."""

    @abstractmethod
    def clear_older_than(self, threshold: datetime) -> int:
        """Borra las filas cuyo ``cached_at`` < threshold. Devuelve filas borradas."""

    @abstractmethod
    def stats(self) -> CacheStats:
        """Devuelve las stats actuales de la tabla."""


class PracticeUploadPort(Protocol):
    """Lo que el tiro de prueba necesita de un servidor CMIS.

    Deliberadamente NO es ``IUploader``: el puerto del `pipeline` sube
    con reintentos y devuelve un objectId. Acá se quiere un solo POST y
    la respuesta cruda, incluida la fallida.
    """

    def upload_raw(
        self,
        file: StagedFile,
        folder_path: str,
        object_type_id: str,
        document_name: str,
        mime_type: str,
        properties: Mapping[str, str],
    ) -> RawResponse:
        """UN solo POST ``createDocument``; el 4xx/5xx se devuelve, no se lanza."""
        ...

    def delete_object(self, object_id: str) -> RawResponse:
        """Borra el objeto recién subido (``cmisaction=delete``)."""
        ...
