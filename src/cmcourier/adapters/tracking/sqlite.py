""":class:`ITrackingStore` respaldado por SQLite.

Tres tipos de conexión conviven sobre el mismo archivo de base de datos
en `WAL mode`:

* conexiones de **lectura por thread** (107) vía
  :class:`ThreadLocalConnectionPool` — WAL permite lectores concurrentes
  con conexiones separadas, así que las lecturas del pipeline (hasta
  50-100 worker threads) y del TUI corren sin lock de aplicación;
* una conexión **sync** (``_sync_conn`` + ``_sync_lock``) para las dos
  escrituras que deben ser visibles inmediatamente: ``start_batch`` y
  ``retry_failed``;
* una conexión **writer** que pertenece a un thread daemon que drena una
  :class:`queue.Queue` de sentencias y las commitea en `batches` (hasta
  500 sentencias, o cada 1 segundo — lo que ocurra primero).

La separación reader / writer la habilita el `journal mode` WAL de SQLite:
una conexión writer nunca bloquea readers, y viceversa. ``synchronous=OFF``
y una page cache de 64 MiB mantienen el `throughput` alto bajo cargas a
escala productiva.

Principio I de la Constitución: este módulo solo depende de la standard
library y de :mod:`cmcourier.domain`. Todas las excepciones
:class:`sqlite3.Error` se envuelven en :class:`TrackingError` antes de
propagarse hacia arriba.
"""

from __future__ import annotations

__all__ = ["SQLiteTrackingStore"]

import logging
import queue
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from cmcourier.adapters.connection_pool import ThreadLocalConnectionPool
from cmcourier.domain.exceptions import TrackingError
from cmcourier.domain.models import (
    BatchDetails,
    BatchInfo,
    DocDetail,
    FailedRecord,
    MigrationRecord,
    ReasonCode,
    ReasonCount,
    StageStatus,
)
from cmcourier.domain.ports import ITrackingStore

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema (REQ-014..018)
# ---------------------------------------------------------------------------


_CREATE_MIGRATION_LOG = """
CREATE TABLE IF NOT EXISTS migration_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger_shortname   TEXT    NOT NULL,
    trigger_cif         TEXT    NOT NULL,
    trigger_system_id   TEXT    NOT NULL,
    rvabrep_txn_num     TEXT    NOT NULL,
    rvabrep_file_name   TEXT    NOT NULL,
    batch_id            TEXT    NOT NULL,
    status              TEXT    NOT NULL,
    created_at          TEXT    NOT NULL,
    cm_object_id        TEXT,
    cm_folder           TEXT,
    cm_object_type      TEXT,
    error_message       TEXT,
    source_file_path    TEXT,
    page_count          INTEGER,
    file_size_bytes     INTEGER,
    started_at          TEXT,
    completed_at        TEXT,
    retry_count         INTEGER NOT NULL DEFAULT 0,
    reason_code         TEXT,
    id_rvi              TEXT
)
"""

# 148 REQ-004: las dos columnas del censo. Nullable y con migración
# aditiva idempotente (mismo patrón que ``_AUDIT_COLUMNS`` de la 124) para
# que las bases existentes se actualicen en el lugar:
#
# * ``reason_code`` — el enum ``ReasonCode`` (REQ-002), NULL para los
#   documentos que subieron bien: no hay nada que explicar.
# * ``id_rvi`` — el código RVI del documento. Se llena SIEMPRE, no sólo
#   en las exclusiones: sin él no se puede agrupar el censo por código,
#   que es exactamente la pregunta del operador, y hasta 148 ese código
#   no se guardaba en ninguna parte de la base (sólo vivía en
#   ``RVABREPDocument.index7``, en memoria).
_CENSUS_COLUMNS: tuple[str, ...] = ("reason_code", "id_rvi")

# 096: batch_id sintético bajo el que el As400Reconciler importa docs
# que otro sistema subió (no choca con UUIDs de corridas reales).
_EXTERNAL_IMPORT_BATCH = "__as400_import__"

_CREATE_MIGRATION_BATCH = """
CREATE TABLE IF NOT EXISTS migration_batch (
    batch_id        TEXT PRIMARY KEY,
    total_records   INTEGER NOT NULL,
    started_at      TEXT NOT NULL,
    completed_at    TEXT
)
"""

_CREATE_IDX_TXN_BATCH = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_migration_log_txn_batch
ON migration_log (rvabrep_txn_num, batch_id)
"""

_CREATE_IDX_UPLOADED = """
CREATE INDEX IF NOT EXISTS idx_migration_log_uploaded
ON migration_log (rvabrep_txn_num)
WHERE status = 'S5_DONE'
"""

# 107: las queries que filtran SOLO por batch_id (tab DETAIL del TUI a
# 4 Hz, get_batch_details, retry_failed, resume scope) hacían full table
# scan — el índice único (rvabrep_txn_num, batch_id) tiene el txn como
# columna líder y no les sirve.
_CREATE_IDX_BATCH = """
CREATE INDEX IF NOT EXISTS idx_migration_log_batch
ON migration_log (batch_id)
"""

# 037: cache de metadatos cross-`batch` (POST-MVP §9). La tabla se crea
# incondicionalmente — la migración de schema es barata e idempotente. El
# `pipeline` solo lee / escribe sobre ella cuando ``metadata.cache.enabled``
# es True; en caso contrario queda vacía.
_CREATE_DOCUMENT_CACHE = """
CREATE TABLE IF NOT EXISTS document_cache (
    txn_num         TEXT NOT NULL,
    fields_hash     TEXT NOT NULL,
    trigger_cif     TEXT,
    properties_json TEXT NOT NULL,
    cached_at       TEXT NOT NULL,
    PRIMARY KEY (txn_num, fields_hash)
)
"""

_CREATE_IDX_DOCUMENT_CACHE_AGE = """
CREATE INDEX IF NOT EXISTS idx_document_cache_cached_at
ON document_cache (cached_at)
"""

# 124: columnas de auditoría de migration_batch (C3 del informe UX v2):
# quién lanzó, desde dónde, con qué config/overrides, con qué veredicto
# del doctor y cómo terminó la corrida. Nullable — las filas legacy y
# los comandos headless que no auditan quedan en NULL. La migración es
# idempotente vía PRAGMA table_info + ALTER TABLE.
_AUDIT_COLUMNS: tuple[str, ...] = (
    "operator",
    "station",
    "pipeline_kind",
    "environment",
    "config_hash",
    "overrides_json",
    "doctor_verdict",
    "outcome",
    # 150 REQ-005: QUÉ lista de clientes activos se usó — ruta, fecha de
    # modificación y cantidad de filas. El CSV de activos es una foto de un
    # momento; sin esto, dentro de seis meses nadie puede responder "¿activo
    # según qué lista?" leyendo el censo. Mismo patrón aditivo de 124: NULL
    # en las filas legacy y en toda corrida con la perilla apagada.
    "eligibility_source_path",
    "eligibility_modified_at",
    "eligibility_rows",
)


# ---------------------------------------------------------------------------
# PRAGMAs
# ---------------------------------------------------------------------------


_PRAGMAS_WAL: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=OFF",
    "PRAGMA cache_size=-64000",
    "PRAGMA temp_store=MEMORY",
)

# 107: las conexiones de lectura por-thread usan un page cache chico —
# con hasta 50-100 worker threads de S5, el cache de 64 MiB de las
# conexiones de escritura multiplicado por N lectores sería un techo de
# RAM absurdo para queries de una fila.
_PRAGMAS_READER: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA cache_size=-8000",
    "PRAGMA temp_store=MEMORY",
)

_BATCH_FLUSH_SIZE = 500
_BATCH_FLUSH_INTERVAL_S = 1.0


# is_stage_done(stage) devuelve True si la fila alcanzó AL MENOS el estado de
# éxito de esa etapa. Después de mark_stage_done(SN_DONE) la fila puede
# transicionar luego al PENDING/DONE/FAILED de una etapa posterior — todos
# esos cuentan como "pasado SN_DONE" para que la lógica de resume pueda
# saltearse el trabajo.
_STATUSES_AT_OR_PAST: dict[StageStatus, frozenset[str]] = {
    StageStatus.S1_DONE: frozenset(
        {
            "S1_DONE",
            "S2_PENDING",
            "S2_DONE",
            "S2_FAILED",
            "S3_PENDING",
            "S3_DONE",
            "S3_FAILED",
            "S4_PENDING",
            "S4_DONE",
            "S4_FAILED",
            "S5_PENDING",
            "S5_DONE",
            "S5_FAILED",
        }
    ),
    StageStatus.S2_DONE: frozenset(
        {
            "S2_DONE",
            "S3_PENDING",
            "S3_DONE",
            "S3_FAILED",
            "S4_PENDING",
            "S4_DONE",
            "S4_FAILED",
            "S5_PENDING",
            "S5_DONE",
            "S5_FAILED",
        }
    ),
    StageStatus.S3_DONE: frozenset(
        {
            "S3_DONE",
            "S4_PENDING",
            "S4_DONE",
            "S4_FAILED",
            "S5_PENDING",
            "S5_DONE",
            "S5_FAILED",
        }
    ),
    StageStatus.S4_DONE: frozenset(
        {
            "S4_DONE",
            "S5_PENDING",
            "S5_DONE",
            "S5_FAILED",
        }
    ),
    StageStatus.S5_DONE: frozenset({"S5_DONE"}),
}

# 107: el SQL de is_stage_done se precomputa por stage — armarlo con
# f-string en cada llamada (6 veces por documento) era trabajo repetido
# en el camino más caliente del store y rompía el statement cache.
_STAGE_DONE_SQL: dict[StageStatus, tuple[str, tuple[str, ...]]] = {
    stage: (
        "SELECT 1 FROM migration_log "
        "WHERE rvabrep_txn_num = ? AND batch_id = ? "
        f"AND status IN ({','.join('?' * len(statuses))}) LIMIT 1",
        tuple(sorted(statuses)),
    )
    for stage, statuses in _STATUSES_AT_OR_PAST.items()
}


# ---------------------------------------------------------------------------
# Envelope de tarea de escritura
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _WriteTask:
    """Una sentencia SQL única y sus parámetros bind, encolados para el writer."""

    sql: str
    params: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class UploadedRecord:
    """099: proyección de un doc ``S5_DONE`` con los campos que la
    recuperación AS400 (`cmcourier sync recover`) necesita de SQLite."""

    txn_num: str
    cm_object_id: str
    shortname: str
    cif: str
    system_id: str
    file_name: str
    retry_count: int


# ---------------------------------------------------------------------------
# Implementación
# ---------------------------------------------------------------------------


class SQLiteTrackingStore(ITrackingStore):
    """Tracking store concreto respaldado por SQLite (`WAL mode` + escritura asíncrona)."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._queue: queue.Queue[_WriteTask] = queue.Queue()
        self._stop = threading.Event()
        self._closed = False

        # 107: las lecturas usan una conexión POR THREAD (WAL permite
        # lectores concurrentes con conexiones separadas) — el viejo
        # ``_reader_lock`` global serializaba 6 lecturas por documento
        # contra una única conexión compartida. ``_sync_conn`` queda solo
        # para las dos escrituras síncronas (``start_batch`` /
        # ``retry_failed``), serializadas por ``_sync_lock``.
        try:
            self._sync_conn = sqlite3.connect(str(db_path), check_same_thread=False)
            self._apply_pragmas(self._sync_conn)
            self._create_schema(self._sync_conn)
        except sqlite3.Error as exc:
            raise TrackingError("failed to open tracking store", path=str(db_path)) from exc
        self._sync_lock = threading.Lock()
        # El pool poda las conexiones de threads muertos (106) — los
        # ThreadPoolExecutor del pipeline se reciclan por chunk.
        self._read_pool = ThreadLocalConnectionPool(self._open_read_connection)

        self._writer_thread = threading.Thread(
            target=self._writer_loop, name="cmcourier-tracking-writer", daemon=True
        )
        self._writer_thread.start()

    # ------------------------------------------------------------------ inicialización

    @staticmethod
    def _apply_pragmas(conn: sqlite3.Connection) -> None:
        for stmt in _PRAGMAS_WAL:
            conn.execute(stmt)

    @staticmethod
    def _create_schema(conn: sqlite3.Connection) -> None:
        conn.execute(_CREATE_MIGRATION_LOG)
        conn.execute(_CREATE_MIGRATION_BATCH)
        conn.execute(_CREATE_IDX_TXN_BATCH)
        conn.execute(_CREATE_IDX_UPLOADED)
        conn.execute(_CREATE_IDX_BATCH)
        conn.execute(_CREATE_DOCUMENT_CACHE)
        conn.execute(_CREATE_IDX_DOCUMENT_CACHE_AGE)
        # 124: migración idempotente de las columnas de auditoría.
        _add_missing_columns(conn, "migration_batch", _AUDIT_COLUMNS)
        # 148: ídem para las dos columnas del censo. ``CREATE TABLE IF NOT
        # EXISTS`` no toca una tabla que ya existe, así que las bases
        # abiertas antes de 148 se actualizan sólo por acá.
        _add_missing_columns(conn, "migration_log", _CENSUS_COLUMNS)
        conn.commit()

    def _open_read_connection(self) -> sqlite3.Connection:
        """107: conexión de lectura del thread actual. ``check_same_thread=
        False`` solo para que ``close_all()`` pueda cerrarla desde otro
        thread — cada conexión la usa un único thread."""
        try:
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            for stmt in _PRAGMAS_READER:
                conn.execute(stmt)
        except sqlite3.Error as exc:
            raise TrackingError("failed to open read connection", path=str(self._db_path)) from exc
        return conn

    # ------------------------------------------------------------ loop del writer

    def _writer_loop(self) -> None:
        try:
            writer = sqlite3.connect(str(self._db_path))
            self._apply_pragmas(writer)
        except sqlite3.Error:
            _log.exception("tracking writer: failed to open writer connection")
            return

        # 082: outer try/except defensivo. Cualquier excepción que escape
        # del while (de ``_drain_batch``, ``queue.get`` con sentinel raro,
        # etc.) NO debe matar el thread silenciosamente — eso causaría
        # que ``mark_stage_done`` y demás escrituras encoladas se
        # pierdan, y los docs queden colgados en su último stage
        # persistido sin ningún error visible. Mantenemos el thread vivo
        # loggeando todo lo que pase.
        while not self._stop.is_set() or not self._queue.empty():
            try:
                batch = self._drain_batch()
                if not batch:
                    continue
                try:
                    writer.execute("BEGIN")
                    for task in batch:
                        writer.execute(task.sql, task.params)
                    writer.commit()
                except Exception:
                    # 082: capturar ``Exception`` (no solo ``sqlite3.Error``)
                    # — pre-082 un ``TypeError`` / ``ValueError`` sobre un
                    # param mal-typed escapaba, mataba el daemon thread, y
                    # las escrituras siguientes se perdían sin trace.
                    _log.exception(
                        "tracking writer: batch commit failed (size=%d) — continuing",
                        len(batch),
                    )
                    try:
                        writer.rollback()
                    except Exception:
                        _log.exception("tracking writer: rollback also failed")
                finally:
                    for _ in batch:
                        self._queue.task_done()
            except Exception:
                _log.exception("tracking writer: unexpected error in loop — continuing")

        writer.close()

    def _drain_batch(self) -> list[_WriteTask]:
        batch: list[_WriteTask] = []
        try:
            batch.append(self._queue.get(timeout=_BATCH_FLUSH_INTERVAL_S))
        except queue.Empty:
            return batch
        while len(batch) < _BATCH_FLUSH_SIZE:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return batch

    # ----------------------------------------------------------- API pública

    def flush(self) -> None:
        """Bloquea hasta que la `queue` del writer queda completamente drenada.

        Lo usan los tests y los orquestadores que necesitan leer estado que
        acaban de escribir.
        """
        self._queue.join()

    def start_batch(self, total_records: int) -> str:
        """Inserta una nueva fila de `batch` de forma sincrónica y devuelve su UUID4."""
        batch_id = str(uuid.uuid4())
        try:
            with self._sync_lock:
                self._sync_conn.execute(
                    "INSERT INTO migration_batch (batch_id, total_records, started_at) "
                    "VALUES (?, ?, ?)",
                    (batch_id, total_records, datetime.now().isoformat()),
                )
                self._sync_conn.commit()
        except sqlite3.Error as exc:
            raise TrackingError("start_batch failed", batch_id=batch_id) from exc
        return batch_id

    def record_batch_audit(
        self,
        batch_id: str,
        *,
        operator: str,
        station: str,
        pipeline_kind: str,
        environment: str,
        config_hash: str,
        overrides_json: str,
        doctor_verdict: str,
    ) -> None:
        """124: escribe la auditoría de lanzamiento del batch (encolada)."""
        self._enqueue(
            "UPDATE migration_batch SET operator = ?, station = ?, pipeline_kind = ?, "
            "environment = ?, config_hash = ?, overrides_json = ?, doctor_verdict = ? "
            "WHERE batch_id = ?",
            (
                operator,
                station,
                pipeline_kind,
                environment,
                config_hash,
                overrides_json,
                doctor_verdict,
                batch_id,
            ),
        )

    def record_eligibility_audit(
        self,
        batch_id: str,
        *,
        source_path: str,
        modified_at: str,
        row_count: int,
    ) -> None:
        """150 REQ-005: qué lista de clientes activos se usó en este batch."""
        self._enqueue(
            "UPDATE migration_batch SET eligibility_source_path = ?, "
            "eligibility_modified_at = ?, eligibility_rows = ? WHERE batch_id = ?",
            (source_path, modified_at, str(row_count), batch_id),
        )

    def batch_audit(self, batch_id: str) -> dict[str, str]:
        """124/125: lee las columnas de auditoría de un batch (para la
        consola). Devuelve solo las columnas con valor no-nulo."""
        cols = ", ".join(_AUDIT_COLUMNS)
        try:
            row = (
                self._read_pool.acquire()
                .execute(
                    f"SELECT {cols} FROM migration_batch WHERE batch_id = ?",
                    (batch_id,),
                )
                .fetchone()
            )
        except sqlite3.Error as exc:
            raise TrackingError("batch_audit failed", batch_id=batch_id) from exc
        if row is None:
            return {}
        return {name: value for name, value in zip(_AUDIT_COLUMNS, row, strict=True) if value}

    def set_batch_outcome(self, batch_id: str, outcome: str) -> None:
        """124: cómo terminó la corrida — completed | cancelled | failed."""
        self._enqueue(
            "UPDATE migration_batch SET outcome = ? WHERE batch_id = ?",
            (outcome, batch_id),
        )

    def complete_batch(self, batch_id: str) -> None:
        self._enqueue(
            "UPDATE migration_batch SET completed_at = ? WHERE batch_id = ?",
            (datetime.now().isoformat(), batch_id),
        )

    def increment_source_total(self, batch_id: str, delta: int) -> None:
        """148 REQ-005: suma *delta* documentos del origen al denominador.

        ``migration_batch.total_records`` se escribía una sola vez en
        ``start_batch`` y no era un conteo del origen: streaming pasa
        ``0`` y staged pasa el ``batch_size`` configurado. Este es el
        camino normal para corregirlo, porque **el total no se conoce de
        antemano**: S1 ve los documentos de a chunks y streaming nunca
        sabe cuántos hay. Se llama a medida que se ven.

        El ``+ delta`` se hace en SQL, no en Python, así que no hay
        read-modify-write: el thread writer serializa todas las
        escrituras encoladas, con lo cual N incrementos concurrentes
        desde N workers suman exacto.

        Un ``batch_id`` desconocido es un no-op (``UPDATE`` sin filas),
        no un error: el tracking nunca debe frenar el pipeline.
        """
        self._enqueue(
            "UPDATE migration_batch SET total_records = total_records + ? WHERE batch_id = ?",
            (int(delta), batch_id),
        )

    def set_source_total(self, batch_id: str, total: int) -> None:
        """148 REQ-005: fija el denominador en un valor absoluto.

        Dos usos: sembrar en ``0`` un `batch` cuyo ``start_batch`` escribió
        un valor que no es un conteo (staged pasa el ``batch_size``), y
        los caminos donde el total del origen SÍ se conoce exacto de una
        (un CSV de triggers ya leído, un ``COUNT(*)``).
        """
        self._enqueue(
            "UPDATE migration_batch SET total_records = ? WHERE batch_id = ?",
            (int(total), batch_id),
        )

    def mark_stage_pending(self, record: MigrationRecord, stage: StageStatus) -> None:
        _require_state(stage, "PENDING")
        # INSERT OR IGNORE vuelve esto idempotente dentro de un `batch` (índice
        # único sobre (rvabrep_txn_num, batch_id)).
        sql = (
            "INSERT OR IGNORE INTO migration_log ("
            "trigger_shortname, trigger_cif, trigger_system_id, "
            "rvabrep_txn_num, rvabrep_file_name, batch_id, status, created_at, "
            "cm_object_id, cm_folder, cm_object_type, error_message, "
            "source_file_path, page_count, file_size_bytes, "
            "started_at, completed_at, retry_count, "
            "reason_code, id_rvi"  # 148
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        self._enqueue(sql, _record_to_params(record, stage))

    def mark_stage_done(
        self,
        txn_num: str,
        batch_id: str,
        stage: StageStatus,
        *,
        cm_object_id: str | None = None,
    ) -> None:
        _require_state(stage, "DONE")
        completed_at = datetime.now().isoformat()
        if cm_object_id is None:
            # 047: el camino con None es byte-idéntico al pre-047 — solo
            # status + completed_at, la columna cm_object_id no se toca, así
            # que cualquier valor previo sobrevive (las transiciones S1..S4
            # nunca lo cargan).
            self._enqueue(
                "UPDATE migration_log SET status = ?, completed_at = ? "
                "WHERE rvabrep_txn_num = ? AND batch_id = ?",
                (stage.value, completed_at, txn_num, batch_id),
            )
        else:
            # 047: S5_DONE lleva el objectId de `cmis` — lo persistimos para
            # que la DB de tracking pueda responder "¿cuál es el objectId del
            # doc X?" sin tener que hacer un walk de hijos contra el server
            # `cmis`.
            self._enqueue(
                "UPDATE migration_log SET status = ?, completed_at = ?, cm_object_id = ? "
                "WHERE rvabrep_txn_num = ? AND batch_id = ?",
                (stage.value, completed_at, cm_object_id, txn_num, batch_id),
            )

    def record_external_upload(
        self,
        *,
        txn_num: str,
        file_name: str,
        shortname: str,
        cif: str,
        system_id: str,
        cm_object_id: str,
        id_rvi: str | None = None,
    ) -> None:
        """096: importa a ``migration_log`` un doc que otro sistema subió.

        Lo usa el :class:`As400Reconciler` cuando AS400 tiene una fila
        ``STSCOD='O'`` que el tracking local no conoce. La fila se inserta
        bajo el ``batch_id`` sintético ``__as400_import__`` para no
        colisionar con corridas reales; ``INSERT OR IGNORE`` la vuelve
        idempotente entre pasadas.

        148: ``id_rvi`` es opcional porque el reconciliador no siempre
        tiene la fila RVABREP a mano. ``reason_code`` queda NULL — este
        documento SÍ se subió."""
        now = datetime.now().isoformat()
        self._enqueue(
            "INSERT OR IGNORE INTO migration_log ("
            "trigger_shortname, trigger_cif, trigger_system_id, "
            "rvabrep_txn_num, rvabrep_file_name, batch_id, status, created_at, "
            "cm_object_id, completed_at, id_rvi"
            ") VALUES (?, ?, ?, ?, ?, ?, 'S5_DONE', ?, ?, ?, ?)",
            (
                shortname,
                cif,
                system_id,
                txn_num,
                file_name,
                _EXTERNAL_IMPORT_BATCH,
                now,
                cm_object_id,
                now,
                id_rvi,
            ),
        )

    def mark_stage_failed(
        self,
        txn_num: str,
        batch_id: str,
        stage: StageStatus,
        error: str,
        *,
        reason_code: ReasonCode | None = None,
    ) -> None:
        _require_state(stage, "FAILED")
        # 148 REQ-004: ``reason_code`` es opcional — cuando es ``None`` la
        # columna no se toca, así una razón escrita antes sobrevive (mismo
        # criterio que ``cm_object_id`` en ``mark_stage_done``).
        clause, extra = _reason_code_assignment(reason_code)
        self._enqueue(
            "UPDATE migration_log "
            f"SET status = ?, error_message = ?, retry_count = retry_count + 1{clause} "
            "WHERE rvabrep_txn_num = ? AND batch_id = ?",
            (stage.value, error, *extra, txn_num, batch_id),
        )

    def mark_stage_terminal(
        self,
        txn_num: str,
        batch_id: str,
        stage: StageStatus,
        error_message: str,
        *,
        reason_code: ReasonCode | None = None,
    ) -> None:
        # 062: transición terminal que NO es una falla — se usa para
        # ``S1_FILTERED`` (borrado en origen) y ``S1_SKIPPED`` (ya subido en
        # un `batch` anterior). A diferencia de ``mark_stage_failed`` esto
        # NO incrementa ``retry_count``; el doc no "falló", solo terminó su
        # recorrido acá por un motivo que no es de error.
        _require_terminal_state(stage)
        completed_at = datetime.now().isoformat()
        clause, extra = _reason_code_assignment(reason_code)
        self._enqueue(
            "UPDATE migration_log "
            f"SET status = ?, error_message = ?, completed_at = ?{clause} "
            "WHERE rvabrep_txn_num = ? AND batch_id = ?",
            (stage.value, error_message, completed_at, *extra, txn_num, batch_id),
        )

    def record_staged_file_metadata(
        self,
        txn_num: str,
        batch_id: str,
        *,
        source_file_path: str,
        page_count: int,
        file_size_bytes: int,
    ) -> None:
        # 058: la fila se insertó originalmente con INSERT-OR-IGNORE en S1
        # cuando ``item.staged_file`` aún era ``None``, así que
        # source_file_path / page_count / file_size_bytes quedaron en NULL.
        # S4 conoce los valores reales — los UPDATEa acá. Idempotente.
        self._enqueue(
            "UPDATE migration_log "
            "SET source_file_path = ?, page_count = ?, file_size_bytes = ? "
            "WHERE rvabrep_txn_num = ? AND batch_id = ?",
            (source_file_path, page_count, file_size_bytes, txn_num, batch_id),
        )

    def uploaded_records(self, batch_id: str | None = None) -> list[UploadedRecord]:
        """099: devuelve los docs ``S5_DONE`` con los campos que la
        recuperación AS400 necesita. ``batch_id`` opcional para acotar
        a un batch; ``None`` recorre todo el tracking."""
        sql = (
            "SELECT rvabrep_txn_num, cm_object_id, trigger_shortname, "
            "trigger_cif, trigger_system_id, rvabrep_file_name, retry_count "
            "FROM migration_log WHERE status = 'S5_DONE'"
        )
        params: tuple[object, ...] = ()
        if batch_id is not None:
            sql += " AND batch_id = ?"
            params = (batch_id,)
        try:
            rows = self._read_pool.acquire().execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            raise TrackingError("uploaded_records failed", batch_id=batch_id) from exc
        return [
            UploadedRecord(
                txn_num=r[0],
                cm_object_id=r[1] or "",
                shortname=r[2],
                cif=r[3],
                system_id=r[4],
                file_name=r[5],
                retry_count=r[6] or 0,
            )
            for r in rows
        ]

    def is_uploaded(self, txn_num: str) -> bool:
        try:
            row = (
                self._read_pool.acquire()
                .execute(
                    "SELECT 1 FROM migration_log "
                    "WHERE rvabrep_txn_num = ? AND status = 'S5_DONE' LIMIT 1",
                    (txn_num,),
                )
                .fetchone()
            )
        except sqlite3.Error as exc:
            raise TrackingError("is_uploaded failed", txn_num=txn_num) from exc
        return row is not None

    def is_stage_done(self, txn_num: str, batch_id: str, stage: StageStatus) -> bool:
        _require_state(stage, "DONE")
        sql, statuses = _STAGE_DONE_SQL[stage]
        try:
            row = self._read_pool.acquire().execute(sql, (txn_num, batch_id, *statuses)).fetchone()
        except sqlite3.Error as exc:
            raise TrackingError("is_stage_done failed", txn_num=txn_num) from exc
        return row is not None

    def list_txn_nums_for_batch(self, batch_id: str) -> set[str]:
        try:
            rows = (
                self._read_pool.acquire()
                .execute(
                    "SELECT DISTINCT rvabrep_txn_num FROM migration_log WHERE batch_id = ?",
                    (batch_id,),
                )
                .fetchall()
            )
        except sqlite3.Error as exc:
            raise TrackingError("list_txn_nums_for_batch failed", batch_id=batch_id) from exc
        return {row[0] for row in rows}

    # -------------------------------------------------- API para operadores (021)

    def list_batches(
        self,
        status: Literal["in_progress", "completed"] | None = None,
    ) -> list[BatchInfo]:
        sql = "SELECT batch_id, started_at, completed_at, total_records FROM migration_batch"
        params: tuple[object, ...] = ()
        if status == "in_progress":
            sql += " WHERE completed_at IS NULL"
        elif status == "completed":
            sql += " WHERE completed_at IS NOT NULL"
        sql += " ORDER BY started_at DESC"
        try:
            rows = self._read_pool.acquire().execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            raise TrackingError("list_batches failed") from exc
        return [_row_to_batch_info(row) for row in rows]

    def get_batch_details(self, batch_id: str) -> BatchDetails | None:
        try:
            conn = self._read_pool.acquire()
            batch_row = conn.execute(
                "SELECT batch_id, started_at, completed_at, total_records "
                "FROM migration_batch WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            if batch_row is None:
                return None
            status_rows = conn.execute(
                "SELECT status, COUNT(*) FROM migration_log WHERE batch_id = ? GROUP BY status",
                (batch_id,),
            ).fetchall()
            failed_rows = conn.execute(
                "SELECT rvabrep_txn_num, status, COALESCE(error_message, ''), "
                "COALESCE(reason_code, '') "
                "FROM migration_log WHERE batch_id = ? AND status LIKE '%_FAILED'",
                (batch_id,),
            ).fetchall()
            # 148 REQ-006: el censo — por qué no se subió cada doc que no
            # se subió, agrupado por razón y código RVI. El balde lo pone
            # el dominio; la base sólo guarda el código.
            reason_rows = conn.execute(
                "SELECT reason_code, COALESCE(id_rvi, ''), COUNT(*) FROM migration_log "
                "WHERE batch_id = ? AND reason_code IS NOT NULL AND reason_code != '' "
                "GROUP BY reason_code, id_rvi ORDER BY reason_code, id_rvi",
                (batch_id,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise TrackingError("get_batch_details failed", batch_id=batch_id) from exc
        return BatchDetails(
            info=_row_to_batch_info(batch_row),
            stage_counts=_pivot_status_counts(status_rows),
            failed_records=tuple(
                FailedRecord(txn_num=r[0], status=r[1], error_message=r[2], reason_code=r[3])
                for r in failed_rows
            ),
            reason_counts=tuple(_row_to_reason_count(r) for r in reason_rows),
        )

    def list_docs_for_batch(self, batch_id: str) -> list[DocDetail]:
        """052: detalle por documento para el drill-down por `chunk` de la TUI."""
        try:
            rows = (
                self._read_pool.acquire()
                .execute(
                    "SELECT rvabrep_txn_num, COALESCE(rvabrep_file_name, ''), status, "
                    "COALESCE(error_message, ''), COALESCE(file_size_bytes, 0), "
                    "COALESCE(reason_code, ''), COALESCE(id_rvi, '') "  # 148
                    "FROM migration_log WHERE batch_id = ? ORDER BY rvabrep_txn_num",
                    (batch_id,),
                )
                .fetchall()
            )
        except sqlite3.Error as exc:
            raise TrackingError("list_docs_for_batch failed", batch_id=batch_id) from exc
        return [
            DocDetail(
                txn_num=str(r[0]),
                file_name=str(r[1]),
                status=str(r[2]),
                error_message=str(r[3]),
                file_size_bytes=int(r[4] or 0),
                reason_code=str(r[5]),
                id_rvi=str(r[6]),
            )
            for r in rows
        ]

    def retry_failed(
        self,
        batch_id: str,
        stage: StageStatus | None = None,
    ) -> int:
        if stage is not None and "_FAILED" not in stage.value:
            raise TrackingError(
                "retry_failed expects a *_FAILED StageStatus or None",
                stage=stage.value,
            )
        # Drenamos cualquier escritura pendiente para que el UPDATE vea un
        # estado consistente.
        self.flush()
        try:
            with self._sync_lock:
                if stage is None:
                    cursor = self._sync_conn.execute(
                        "UPDATE migration_log "
                        "SET status = REPLACE(status, '_FAILED', '_PENDING'), "
                        "    error_message = NULL, "
                        "    reason_code = NULL "  # 148: la razón muere con la falla
                        "WHERE batch_id = ? AND status LIKE '%_FAILED'",
                        (batch_id,),
                    )
                else:
                    cursor = self._sync_conn.execute(
                        "UPDATE migration_log "
                        "SET status = REPLACE(status, '_FAILED', '_PENDING'), "
                        "    error_message = NULL, "
                        "    reason_code = NULL "  # 148: la razón muere con la falla
                        "WHERE batch_id = ? AND status = ?",
                        (batch_id, stage.value),
                    )
                self._sync_conn.commit()
        except sqlite3.Error as exc:
            raise TrackingError("retry_failed failed", batch_id=batch_id) from exc
        return int(cursor.rowcount)

    def close(self) -> None:
        """Apagado idempotente: drena la `queue`, frena el writer, cierra el reader."""
        if self._closed:
            return
        self._closed = True
        self._queue.join()
        self._stop.set()
        self._writer_thread.join(timeout=5.0)
        self._read_pool.close_all()
        try:
            self._sync_conn.close()
        except sqlite3.Error:
            _log.exception("tracking store: failed to close sync connection")

    # --------------------------------------------------------------- helpers

    def _enqueue(self, sql: str, params: tuple[Any, ...]) -> None:
        self._queue.put(_WriteTask(sql=sql, params=params))


# ---------------------------------------------------------------------------
# Helpers a nivel de módulo (fuera de la clase para que los métodos queden cortos)
# ---------------------------------------------------------------------------


def _require_state(stage: StageStatus, expected_suffix: str) -> None:
    """Rechaza valores de stage cuyo nombre no termina con el sufijo esperado."""
    if not stage.value.endswith(f"_{expected_suffix}"):
        raise ValueError(f"expected a {expected_suffix} stage, got {stage.value!r}")


def _require_terminal_state(stage: StageStatus) -> None:
    """062: acepta cualquier sufijo terminal (no-progresivo) — FAILED, FILTERED, SKIPPED."""
    if not any(stage.value.endswith(f"_{s}") for s in ("FAILED", "FILTERED", "SKIPPED")):
        raise ValueError(f"expected a terminal stage, got {stage.value!r}")


def _row_to_batch_info(row: tuple[Any, ...]) -> BatchInfo:
    """Mapea una fila (batch_id, started_at, completed_at, total_records)."""
    completed_at = datetime.fromisoformat(row[2]) if row[2] is not None else None
    return BatchInfo(
        batch_id=row[0],
        started_at=datetime.fromisoformat(row[1]),
        completed_at=completed_at,
        total_records=int(row[3]),
    )


def _row_to_reason_count(row: tuple[Any, ...]) -> ReasonCount:
    """148: mapea ``(reason_code, id_rvi, count)`` resolviendo el balde.

    ``bucket_of`` devuelve ``None`` ante un código que no pertenece a la
    taxonomía vigente (fila legacy o editada a mano): en ese caso el
    balde queda en ``""`` y el conteo se reporta igual. Un documento que
    el censo no sabe clasificar tiene que verse, no desaparecer.
    """
    bucket = ReasonCode.bucket_of(str(row[0]))
    return ReasonCount(
        bucket=bucket.value if bucket is not None else "",
        reason_code=str(row[0]),
        id_rvi=str(row[1]),
        count=int(row[2]),
    )


def _add_missing_columns(conn: sqlite3.Connection, table: str, columns: tuple[str, ...]) -> None:
    """Migración aditiva idempotente: ``ALTER TABLE ADD COLUMN`` sólo lo que falta.

    ``CREATE TABLE IF NOT EXISTS`` no toca una tabla que ya existe, así
    que esta es la única vía por la que una base vieja se pone al día.
    Nunca borra ni reescribe: las filas existentes quedan con ``NULL`` en
    la columna nueva y se siguen leyendo igual.
    """
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for col in columns:
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")


def _reason_code_assignment(reason_code: ReasonCode | None) -> tuple[str, tuple[Any, ...]]:
    """148: el fragmento ``SET`` del reason_code, o nada si no vino.

    Devolver ``("", ())`` cuando es ``None`` deja la columna intacta, así
    los callers pre-148 no pisan con NULL una razón ya escrita.
    """
    if reason_code is None:
        return ("", ())
    return (", reason_code = ?", (reason_code.value,))


# Stages que la tabla ``batch show`` del CLI siempre renderiza, en orden fijo.
_DISPLAY_STAGES: tuple[str, ...] = ("S0", "S1", "S2", "S3", "S4", "S5")
# Las tres salidas históricas. 148 REQ-006: ya NO son la lista completa —
# son sólo las que van primero y en ese orden, para que la salida actual
# no se reordene. Toda otra salida que exista en los datos se agrega
# detrás (ver ``_pivot_status_counts``).
_DISPLAY_OUTCOMES: tuple[str, ...] = ("DONE", "FAILED", "PENDING")


def _pivot_status_counts(
    rows: list[tuple[Any, ...]],
) -> dict[str, dict[str, int]]:
    """Agrupa filas ``(status, count)`` en ``{Sn: {salida: conteo}}``.

    148 REQ-006: rinde **toda** salida que exista en los datos, no sólo
    ``DONE / FAILED / PENDING``. Hasta 148 el pivot descartaba en
    silencio ``S1_FILTERED`` y ``S1_SKIPPED``, con lo cual en
    ``batch show`` la suma de las columnas no daba el total y nadie
    avisaba. Un reporte que no cuadra y no avisa es peor que no tener
    reporte.

    La forma sigue siendo predecible para el renderer: ``S0..S5``
    completo, las tres salidas históricas primero y en orden, y el mismo
    juego de claves en cada etapa (cero para los combos que no existen).
    """
    outcomes = list(_DISPLAY_OUTCOMES)
    parsed: list[tuple[str, str, int]] = []
    for status_value, count in rows:
        stage, _, outcome = str(status_value).partition("_")
        if not outcome or stage not in _DISPLAY_STAGES:
            continue
        parsed.append((stage, outcome, int(count)))
        if outcome not in outcomes:
            outcomes.append(outcome)
    pivot: dict[str, dict[str, int]] = {
        stage: dict.fromkeys(outcomes, 0) for stage in _DISPLAY_STAGES
    }
    for stage, outcome, count in parsed:
        pivot[stage][outcome] = count
    return pivot


def _record_to_params(record: MigrationRecord, stage: StageStatus) -> tuple[Any, ...]:
    """Aplana un :class:`MigrationRecord` en la tupla de 20 elementos para INSERT."""
    return (
        record.trigger_shortname,
        record.trigger_cif,
        record.trigger_system_id,
        record.rvabrep_txn_num,
        record.rvabrep_file_name,
        record.batch_id,
        stage.value,
        record.created_at.isoformat(),
        record.cm_object_id,
        record.cm_folder,
        record.cm_object_type,
        record.error_message,
        record.source_file_path,
        record.page_count,
        record.file_size_bytes,
        record.started_at.isoformat() if record.started_at else None,
        record.completed_at.isoformat() if record.completed_at else None,
        record.retry_count,
        # 148: el enum va como string; ``None`` ⇒ NULL (subió bien).
        record.reason_code.value if record.reason_code is not None else None,
        record.id_rvi,
    )
