"""Adaptador de coordinación AS400 NIARVILOG (034 fase 2).

Es dueño de la capa de idempotencia distribuida que se monta sobre el
``SQLiteTrackingStore`` existente. NO es un :class:`ITrackingStore` — es
una superficie de coordinación separada que usa
:class:`IdempotencyCoordinator` cuando ``tracking.as400_sync.enabled=true``.

Aplica el Principio VI de la Constitución: el server AS400 nunca se mockea,
pero los bindings del driver ``pyodbc`` SÍ se fakean a nivel cursor /
conexión para los tests (espejo de :class:`As400DataSource`).

Mapeo de campos (cerrado en el spec 034):

    SISCOD  ← trigger.system_id           (CHAR(1))
    TRNNUM  ← document.txn_num             (CHAR(7), = ABAANB)
    DOCFRM  ← document.index7              (CHAR(30), = ABAHCD)
    IMGARC  ← document.file_name           (CHAR(12), primera página)
    IMGTIP  ← document.image_type          (CHAR(1))
    CTECIF  ← trigger.shortname            (VARCHAR(30))
    CTENUM  ← int(trigger.cif or 0)        (DECIMAL(9,0))
    STSCOD  ← derivado: N/I/O/F
    IDNBAC  ← mapping.id_corto (== IDCM)   (VARCHAR(10))
    TIPIDN  ← mapping.cmis_type            (VARCHAR(128), '' hasta 035)
    OBJIDN  ← record.cm_object_id          (VARCHAR(128), post-S5)
    NUMREI  ← record.retry_count           (INTEGER)
    PMRREI  ← record.started_at o NOW()    (TIMESTAMP)
    FINREI  ← auto-update de DB2           (TIMESTAMP)
    EERRMSG ← record.error_message         (VARCHAR(1024))
"""

from __future__ import annotations

__all__ = [
    "As400CoordinationError",
    "As400NiarvilogStore",
    "As400UnreachableError",
    "NiarvilogColumns",
    "NiarvilogRow",
]

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypeVar

from cmcourier.adapters.connection_pool import ThreadLocalConnectionPool
from cmcourier.config.schema import As400ConnectionConfig
from cmcourier.domain.models import (
    CMMapping,
    MigrationRecord,
    RVABREPDocument,
    Trigger,
)

_R = TypeVar("_R")

_network_log = logging.getLogger("cmcourier.metrics.network")
_log = logging.getLogger(__name__)

# Import lazy — mismo patrón que As400DataSource.
pyodbc: Any = None


class As400CoordinationError(Exception):
    """Se lanza cuando una operación NIARVILOG falla por una razón no
    transitoria (schema mismatch, error de sintaxis, violación de
    integridad surgida como Error, etc.). No se reintenta.
    """


class As400UnreachableError(As400CoordinationError):
    """Se lanza cuando se agotan los intentos de `retry` ante un
    ``pyodbc.OperationalError`` transitorio. El `pipeline` aborta con exit 2.
    """


_MAX_BACKOFF_S = 300.0  # 5 minutos


@dataclass(frozen=True, slots=True)
class NiarvilogRow:
    """Una fila de RVILIB.NIARVILOG (forma de lectura)."""

    siscod: str
    trnnum: str
    docfrm: str
    imgarc: str
    imgtip: str
    ctecif: str
    ctenum: int
    stscod: str  # 'N' / 'I' / 'O' / 'F'
    idnbac: str
    tipidn: str
    objidn: str
    numrei: int
    pmrrei: datetime
    finrei: datetime
    eerrmsg: str


@dataclass(frozen=True, slots=True)
class NiarvilogColumns:
    """Nombres físicos de columnas de RVILIB.NIARVILOG — por entorno (049).

    Los defaults son los nombres canónicos. Se validan upstream con
    :class:`cmcourier.config.schema.NiarvilogColumnsModel` (chequeo de
    identificador DB2); el adaptador los trata como identificadores
    confiables, seguros para interpolar en SQL.
    """

    system_id: str = "SISCOD"
    txn_num: str = "TRNNUM"
    doc_format: str = "DOCFRM"
    image_archive: str = "IMGARC"
    image_type: str = "IMGTIP"
    client_cif: str = "CTECIF"
    client_num: str = "CTENUM"
    status: str = "STSCOD"
    idcm: str = "IDNBAC"
    cm_type: str = "TIPIDN"
    cm_object_id: str = "OBJIDN"
    retry_count: str = "NUMREI"
    started_at: str = "PMRREI"
    finished_at: str = "FINREI"
    error_message: str = "EERRMSG"

    def select_list(self) -> str:
        """Lista de columnas separadas por coma para ``SELECT`` — orden fijo
        de campos que coincide con :class:`NiarvilogRow`."""
        return ", ".join(
            (
                self.system_id,
                self.txn_num,
                self.doc_format,
                self.image_archive,
                self.image_type,
                self.client_cif,
                self.client_num,
                self.status,
                self.idcm,
                self.cm_type,
                self.cm_object_id,
                self.retry_count,
                self.started_at,
                self.finished_at,
                self.error_message,
            )
        )


class As400NiarvilogStore:
    """Store de idempotencia distribuida sobre RVILIB.NIARVILOG.

    Operaciones:

    * :meth:`try_claim` — ``UPDATE STSCOD='I' WHERE STSCOD='N'`` atómico
      con fallback a INSERT para filas que aparecen por primera vez.
      Devuelve True si ahora somos dueños de la fila.
    * :meth:`mark_uploaded` — ``UPDATE STSCOD='O', OBJIDN=...`` cuando S5
      completa. Loguea WARNING si el rowcount != 1 (la fila cambió bajo
      nuestros pies entre el claim y el complete; investigar pero no
      fallar el `pipeline`).
    * :meth:`mark_failed` — ``UPDATE STSCOD='F', EERRMSG=...,
      NUMREI=NUMREI+1`` ante cualquier falla de etapa.
    * :meth:`read_state` — SELECT de una fila por PK.
    * :meth:`cleanup_stale_in_progress` — resetea filas que quedaron
      pegadas en ``STSCOD='I'`` por demasiado tiempo (una corrida
      anterior crasheó a mitad de claim).

    Nota DB2 for i: ``FINREI`` se declara ``ROW CHANGE TIMESTAMP``, así
    que DB2 lo actualiza implícitamente en cada UPDATE — nuestro SQL nunca
    lo referencia.
    """

    def __init__(
        self,
        *,
        connection: As400ConnectionConfig,
        username: str,
        password: str,
        library: str = "RVILIB",
        table: str = "NIARVILOG",
        columns: NiarvilogColumns | None = None,
        stale_in_progress_minutes: int = 30,
        retry_attempts: int = 3,
        retry_base_delay_s: float = 5.0,
    ) -> None:
        self._cfg = connection
        self._username = username
        self._password = password
        self._library = library
        self._table = table
        self._cols = columns or NiarvilogColumns()
        self._stale_minutes = stale_in_progress_minutes
        self._retry_attempts = max(1, int(retry_attempts))
        self._retry_base_delay_s = max(0.001, float(retry_base_delay_s))
        # 095: connection pool por-worker — cada thread cachea su propia
        # conexión (una conexión ODBC serializa statements). 106: el pool
        # compartido además poda las conexiones de threads muertos — los
        # ThreadPoolExecutor de S5 se reciclan por chunk y sus conexiones
        # quedaban huérfanas hasta el close() final.
        self._pool = ThreadLocalConnectionPool(self._open_connection)
        self._closed = False

    # ----------------------------------------------------------- API pública

    def try_claim(
        self,
        *,
        record: MigrationRecord,
        document: RVABREPDocument,
        mapping: CMMapping,
        trigger: Trigger,
    ) -> bool:
        """Claim atómico. Devuelve True si y solo si este proceso ahora es dueño de la fila."""
        pk = _pk_from(document=document, trigger=trigger)
        c = self._cols
        update_sql = (
            f"UPDATE {self._full_table()} "
            f"SET {c.status} = 'I', {c.idcm} = ?, {c.cm_type} = ? "
            f"WHERE {c.system_id} = ? AND {c.txn_num} = ? "
            f"AND {c.doc_format} = ? AND {c.image_archive} = ? "
            f"AND {c.status} = 'N'"
        )
        params = [mapping.id_corto, mapping.cmis_type, *pk]
        rowcount = self._execute_write(update_sql, params, "niarvilog_claim_update")
        if rowcount >= 1:
            return True
        # La fila no existe (o ya está en un estado distinto de N). Intentamos INSERT.
        try:
            self._insert_new_claim(
                document=document, mapping=mapping, trigger=trigger, record=record
            )
        except _pyodbc_integrity_error_type():
            # `race condition`: otro proceso insertó la fila entre nuestro
            # UPDATE y el INSERT. Eso significa que alguien más es dueño
            # ahora → False.
            return False
        return True

    def mark_uploaded(
        self,
        *,
        record: MigrationRecord,  # noqa: ARG002 — se mantiene por simetría de API
        document: RVABREPDocument,
        mapping: CMMapping,  # noqa: ARG002 — se mantiene por simetría de API
        trigger: Trigger,
        cm_object_id: str,
    ) -> None:
        pk = _pk_from(document=document, trigger=trigger)
        c = self._cols
        sql = (
            f"UPDATE {self._full_table()} "
            f"SET {c.status} = 'O', {c.cm_object_id} = ?, {c.error_message} = '' "
            f"WHERE {c.system_id} = ? AND {c.txn_num} = ? "
            f"AND {c.doc_format} = ? AND {c.image_archive} = ?"
        )
        params = [cm_object_id, *pk]
        rowcount = self._execute_write(sql, params, "niarvilog_mark_uploaded")
        if rowcount != 1:
            _log.warning(
                "niarvilog_mark_uploaded: unexpected rowcount=%s for trnnum=%s",
                rowcount,
                pk[1],
            )

    def mark_failed(
        self,
        *,
        record: MigrationRecord,  # noqa: ARG002
        document: RVABREPDocument,
        mapping: CMMapping,  # noqa: ARG002
        trigger: Trigger,
        error: str,
    ) -> None:
        pk = _pk_from(document=document, trigger=trigger)
        c = self._cols
        sql = (
            f"UPDATE {self._full_table()} "
            f"SET {c.status} = 'F', {c.error_message} = ?, "
            f"{c.retry_count} = {c.retry_count} + 1 "
            f"WHERE {c.system_id} = ? AND {c.txn_num} = ? "
            f"AND {c.doc_format} = ? AND {c.image_archive} = ?"
        )
        # AS400 VARCHAR(1024) — truncamos defensivamente.
        params = [error[:1024], *pk]
        self._execute_write(sql, params, "niarvilog_mark_failed")

    def read_state(
        self,
        *,
        siscod: str,
        trnnum: str,
        docfrm: str,
        imgarc: str,
    ) -> NiarvilogRow | None:
        c = self._cols
        sql = (
            f"SELECT {c.select_list()} FROM {self._full_table()} "
            f"WHERE {c.system_id} = ? AND {c.txn_num} = ? "
            f"AND {c.doc_format} = ? AND {c.image_archive} = ?"
        )
        params = [siscod, trnnum, docfrm, imgarc]
        rows = self._execute_read(sql, params, "niarvilog_read_state")
        if not rows:
            return None
        return self._row_from_dict(rows[0])

    def read_state_by_txn(self, *, trnnum: str) -> NiarvilogRow | None:
        """Lookup solo por TRNNUM (034 fase 4).

        Para el `pre-flight` de sync + ``cmcourier sync resolve``, el caller
        normalmente solo conoce el txn_num, no la PK compuesta completa
        (SISCOD/DOCFRM/IMGARC). La convención operativa del banco es una
        fila por txn_num — este método lo asume y devuelve la primera
        fila que coincida (o None).
        """
        c = self._cols
        sql = (
            f"SELECT {c.select_list()} FROM {self._full_table()} "
            f"WHERE {c.txn_num} = ? FETCH FIRST 1 ROWS ONLY"
        )
        rows = self._execute_read(sql, [trnnum], "niarvilog_read_state_by_txn")
        if not rows:
            return None
        return self._row_from_dict(rows[0])

    def mark_uploaded_by_txn(self, *, trnnum: str, cm_object_id: str) -> int:
        """Helper de fase 4 para ``sync resolve --prefer-local``.

        Actualiza la fila NIARVILOG existente por TRNNUM, seteando
        ``STSCOD='O'`` + ``OBJIDN=cm_object_id``. Devuelve el row count.
        Los operadores usan esto cuando SQLite sabe que el doc está
        completo pero AS400 no recibió la notificación (por ejemplo,
        AS400 estaba caído).
        """
        c = self._cols
        sql = (
            f"UPDATE {self._full_table()} "
            f"SET {c.status} = 'O', {c.cm_object_id} = ?, {c.error_message} = '' "
            f"WHERE {c.txn_num} = ?"
        )
        return self._execute_write(sql, [cm_object_id, trnnum], "niarvilog_mark_uploaded_by_txn")

    def insert_recovered_row(
        self,
        *,
        siscod: str,
        trnnum: str,
        docfrm: str,
        imgarc: str,
        imgtip: str,
        ctecif: str,
        ctenum: int,
        idnbac: str,
        tipidn: str,
        objidn: str,
        numrei: int = 0,
    ) -> int:
        """099: INSERT directo de una fila terminal ``STSCOD='O'``.

        Recupera un documento que se subió a CM pero perdió su fila en
        NIARVILOG (bug 096, ver cambio 098). A diferencia de
        ``try_claim``/``mark_uploaded``, recibe los 11 campos explícitos
        — la recuperación los re-deriva desde SQLite + RVABREP + mapping,
        no desde los objetos de dominio de una corrida en vuelo.
        Devuelve el row count."""
        c = self._cols
        sql = (
            f"INSERT INTO {self._full_table()} "
            f"({c.system_id}, {c.txn_num}, {c.doc_format}, {c.image_archive}, "
            f"{c.image_type}, {c.client_cif}, {c.client_num}, {c.status}, "
            f"{c.idcm}, {c.cm_type}, {c.cm_object_id}, {c.retry_count}, "
            f"{c.error_message}) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, 'O', ?, ?, ?, ?, '')"
        )
        params: list[Any] = [
            siscod,
            trnnum,
            docfrm,
            imgarc,
            imgtip,
            ctecif,
            ctenum,
            idnbac,
            tipidn,
            objidn,
            numrei,
        ]
        return self._execute_write(sql, params, "niarvilog_insert_recovered")

    def cleanup_stale_in_progress(self) -> int:
        """Resetea las filas con STSCOD='I' cuyo FINREI es más viejo que el umbral.

        Devuelve el row count. Útil cuando un claim anterior crasheó
        entre el UPDATE 'I' y la escritura eventual de 'O' / 'F'.
        """
        c = self._cols
        sql = (
            f"UPDATE {self._full_table()} "
            f"SET {c.status} = 'N' "
            f"WHERE {c.status} = 'I' "
            f"AND {c.finished_at} < (CURRENT_TIMESTAMP - ? MINUTES)"
        )
        return self._execute_write(sql, [self._stale_minutes], "niarvilog_cleanup_stale")

    def close(self) -> None:
        """095: cierra TODAS las conexiones del pool, sin importar qué
        thread llame. Idempotente vía ``_closed``."""
        if self._closed:
            return
        self._closed = True
        self._pool.close_all()

    # ----------------------------------------------------------- internos

    def _full_table(self) -> str:
        return f"{self._library}.{self._table}"

    def _row_from_dict(self, row: dict[str, Any]) -> NiarvilogRow:
        """Construye un :class:`NiarvilogRow` a partir de un dict de resultados
        indexado por los nombres físicos de columna configurados."""
        c = self._cols
        return NiarvilogRow(
            siscod=str(row[c.system_id]).strip(),
            trnnum=str(row[c.txn_num]).strip(),
            docfrm=str(row[c.doc_format]).strip(),
            imgarc=str(row[c.image_archive]).strip(),
            imgtip=str(row[c.image_type]).strip(),
            ctecif=str(row[c.client_cif]).strip(),
            ctenum=int(row[c.client_num] or 0),
            stscod=str(row[c.status]).strip(),
            idnbac=str(row[c.idcm]).strip(),
            tipidn=str(row[c.cm_type]).strip(),
            objidn=str(row[c.cm_object_id]).strip(),
            numrei=int(row[c.retry_count] or 0),
            pmrrei=row[c.started_at],
            finrei=row[c.finished_at],
            eerrmsg=str(row[c.error_message]).strip(),
        )

    def _insert_new_claim(
        self,
        *,
        record: MigrationRecord,  # noqa: ARG002 — se mantiene para futuros campos
        document: RVABREPDocument,
        mapping: CMMapping,
        trigger: Trigger,
    ) -> None:
        c = self._cols
        sql = (
            f"INSERT INTO {self._full_table()} "
            f"({c.system_id}, {c.txn_num}, {c.doc_format}, {c.image_archive}, "
            f"{c.image_type}, {c.client_cif}, {c.client_num}, {c.status}, "
            f"{c.idcm}, {c.cm_type}, {c.cm_object_id}, {c.retry_count}, "
            f"{c.error_message}) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, 'I', ?, ?, '', 0, '')"
        )
        # 046: trigger es polimórfico; usamos audit_row() para extraer la
        # tripleta (shortname, cif, system_id) que NIARVILOG indexa.
        audit = trigger.audit_row()
        cif_str = audit.get("cif") or ""
        params: list[Any] = [
            audit.get("system_id") or "",
            document.txn_num,
            document.index7,
            document.file_name,
            document.image_type,
            audit.get("shortname") or "",
            int(cif_str) if cif_str.isdigit() else 0,
            mapping.id_corto,
            mapping.cmis_type,
        ]
        self._execute_write(sql, params, "niarvilog_insert_claim")

    def _execute_write(self, sql: str, params: list[Any], kind: str) -> int:
        return self._with_retry(kind, lambda: self._do_execute_write(sql, params, kind))

    def _do_execute_write(self, sql: str, params: list[Any], kind: str) -> int:
        conn = self._connect()
        cursor = conn.cursor()
        t0 = time.monotonic()
        try:
            cursor.execute(sql, params)
            rowcount = int(cursor.rowcount)
            conn.commit()
            _network_log.info(
                kind,
                extra={
                    "kind": kind,
                    "duration_ms": round((time.monotonic() - t0) * 1000.0, 3),
                    "row_count": rowcount,
                    "sql_prefix": sql[:80],
                },
            )
            return rowcount
        except _pyodbc_integrity_error_type():
            # El caller (try_claim) maneja esto. Re-raise sin envolver — y
            # crucialmente, NUNCA reintentamos: un IntegrityError significa
            # que la fila ya existe o que la constraint falló de manera
            # determinística.
            raise
        except _pyodbc_operational_error_type():
            # Transitorio — dejamos que _with_retry se encargue.
            raise
        except _pyodbc_error_type() as exc:
            # Error de pyodbc no transitorio — envolvemos y propagamos
            # inmediatamente.
            raise As400CoordinationError(f"NIARVILOG {kind} failed: {exc}") from exc
        finally:
            cursor.close()

    def _execute_read(self, sql: str, params: list[Any], kind: str) -> list[dict[str, Any]]:
        return self._with_retry(kind, lambda: self._do_execute_read(sql, params, kind))

    def _do_execute_read(self, sql: str, params: list[Any], kind: str) -> list[dict[str, Any]]:
        conn = self._connect()
        cursor = conn.cursor()
        t0 = time.monotonic()
        try:
            cursor.execute(sql, params)
            columns = [col[0] for col in cursor.description or []]
            rows = [dict(zip(columns, row, strict=False)) for row in cursor.fetchall()]
            _network_log.info(
                kind,
                extra={
                    "kind": kind,
                    "duration_ms": round((time.monotonic() - t0) * 1000.0, 3),
                    "row_count": len(rows),
                    "sql_prefix": sql[:80],
                },
            )
            return rows
        except _pyodbc_operational_error_type():
            raise
        except _pyodbc_error_type() as exc:
            raise As400CoordinationError(f"NIARVILOG {kind} failed: {exc}") from exc
        finally:
            cursor.close()

    def _with_retry(self, kind: str, op: Callable[[], _R]) -> _R:
        """Reintenta una operación NIARVILOG ante un ``OperationalError`` transitorio.

        Secuencia: ``base, base*2, base*4, ...`` capada a 5 minutos. Usa el
        ``retry_attempts`` configurado (intentos totales) y
        ``retry_base_delay_s`` del YAML.

        IntegrityError y otras subclases de pyodbc.Error NO se reintentan
        — o son determinísticas (`race condition` de PK en try_claim) o
        son schema mismatches que no se van a arreglar solas.
        """
        last_exc: BaseException | None = None
        for attempt in range(1, self._retry_attempts + 1):
            try:
                return op()
            except _pyodbc_integrity_error_type():
                # Determinístico — NO reintentar. Propagamos para que
                # try_claim pueda detectar la `race condition`.
                raise
            except _pyodbc_operational_error_type() as exc:
                last_exc = exc
                if attempt >= self._retry_attempts:
                    break
                delay = min(
                    self._retry_base_delay_s * (2 ** (attempt - 1)),
                    _MAX_BACKOFF_S,
                )
                _log.warning(
                    "NIARVILOG %s attempt %d/%d failed (%s); retrying in %.1fs",
                    kind,
                    attempt,
                    self._retry_attempts,
                    exc,
                    delay,
                )
                # Reseteamos la conexión cacheada — los errores operacionales
                # suelen dejarla en mal estado. La próxima op va a reconectar.
                self._reset_connection()
                time.sleep(delay)
        raise As400UnreachableError(
            f"NIARVILOG {kind} unreachable after {self._retry_attempts} attempts: {last_exc}"
        ) from last_exc

    def _reset_connection(self) -> None:
        """095: resetea SOLO la conexión del thread que está reintentando.
        Las conexiones de los demás workers quedan intactas."""
        self._pool.reset_current()

    def _connect(self) -> Any:
        """095: devuelve la conexión thread-local del worker actual,
        abriéndola lazy la primera vez que este thread la necesita."""
        return self._pool.acquire()

    def _open_connection(self) -> Any:
        _import_pyodbc()
        try:
            return pyodbc.connect(self._build_connection_string())
        except _pyodbc_error_type() as exc:
            raise As400CoordinationError(f"NIARVILOG connect failed: {exc}") from exc

    def _build_connection_string(self) -> str:
        # 087: ``CommitMode=0`` (= ``*NONE``) desactiva commitment control.
        # Pre-087 el driver "IBM i Access ODBC Driver" abría con default
        # ``*CHG`` que requiere que la tabla NIARVILOG esté journaled —
        # tablas no-journaled rompían con SQL7008 "not valid for
        # operation". CMCourier no usa transacciones multi-statement
        # (claim atómico viene del ``WHERE STSCOD='N'``, no del commit),
        # así que sin commitment control el comportamiento es idéntico
        # contra tablas journaled o no. El ``conn.commit()`` queda como
        # no-op inofensivo.
        return (
            f"DRIVER={{{self._cfg.driver}}};"
            f"SYSTEM={self._cfg.host};"
            f"PORT={self._cfg.port};"
            f"DATABASE={self._cfg.database};"
            f"UID={self._username};"
            f"PWD={self._password};"
            f"CommitMode=0;"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pk_from(*, document: RVABREPDocument, trigger: Trigger) -> tuple[str, str, str, str]:
    """Construye las cuatro columnas de PK (SISCOD, TRNNUM, DOCFRM, IMGARC)."""
    return (
        trigger.audit_row().get("system_id") or "",
        document.txn_num,
        document.index7,
        document.file_name,
    )


def _import_pyodbc() -> None:
    global pyodbc
    if pyodbc is not None:
        return
    import pyodbc as _pyodbc  # noqa: PLC0415

    pyodbc = _pyodbc


def _pyodbc_error_type() -> type[BaseException]:
    if pyodbc is None:
        return RuntimeError
    return pyodbc.Error  # type: ignore[no-any-return]


def _pyodbc_integrity_error_type() -> type[BaseException]:
    if pyodbc is None:
        return RuntimeError
    # pyodbc expone IntegrityError como subclase de Error.
    return getattr(pyodbc, "IntegrityError", pyodbc.Error)  # type: ignore[no-any-return]


def _pyodbc_operational_error_type() -> type[BaseException]:
    """Errores transitorios que justifican un `retry`. ``OperationalError``
    cubre caídas de red, deadlocks, y la mayoría de los estados de
    "servidor temporalmente no disponible". Cuando pyodbc no está
    instalado (entorno de test), devuelve un centinela que no va a
    coincidir con excepciones reales."""
    if pyodbc is None:
        return RuntimeError
    return getattr(pyodbc, "OperationalError", pyodbc.Error)  # type: ignore[no-any-return]
