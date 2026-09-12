"""Operaciones del sync SQLite ↔ AS400 NIARVILOG, sin click (128).

La lógica de ``cmcourier sync status | resolve | recover`` (034/099)
vivía pegada a click (``sys.exit`` / ``click.echo``). Acá está la misma
lógica como funciones puras que usan tanto el CLI como la pestaña
SYNC de la consola:

* errores de USO / configuración → :class:`SyncOpError` (mensaje para
  el operador);
* errores del AS400 → :class:`As400CoordinationError` (propaga);
* los stores se abren y se cierran adentro de cada operación.
"""

from __future__ import annotations

__all__ = [
    "StatusResult",
    "build_as400_store",
    "SyncOpError",
    "build_sync_stores",
    "sync_pull",
    "sync_recover",
    "sync_resolve",
    "sync_status",
    "sync_unavailable_reason",
]

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from cmcourier.adapters.tracking import SQLiteTrackingStore
from cmcourier.adapters.tracking.as400_niarvilog import As400NiarvilogStore
from cmcourier.config.loader import Secrets
from cmcourier.config.schema import PipelineConfig
from cmcourier.config.wiring import build_as400_recovery, build_niarvilog_store
from cmcourier.services.pull import As400Pull, PullItem, PullResult
from cmcourier.services.recovery import RecoveryResult, SyncProgress


class SyncOpError(Exception):
    """Mal uso o configuración insuficiente — el mensaje es para el operador."""


@dataclass(frozen=True, slots=True)
class StatusResult:
    """151 REQ-005: ``sync status`` decía *"reporta cualquier conflicto sin
    tocar estado"* y sólo limpiaba los ``'I'`` vencidos. Ahora los reporta:
    con el pull construido, listar divergencias es la misma consulta sin
    escribir.

    * ``scanned`` — filas ``'O'`` / ``'F'`` vistas en NIARVILOG.
    * ``importable`` — las que el tracking local no tiene terminadas (las
      traería ``sync pull``).
    * ``divergences`` — los dos lados afirman cosas distintas. No las
      resuelve ninguna dirección sola: ``sync resolve``.
    """

    stale_cleaned: int
    scanned: int = 0
    importable: int = 0
    divergences: list[PullItem] = field(default_factory=list)


def sync_unavailable_reason(config: PipelineConfig, secrets: Secrets) -> str | None:
    """``None`` si se puede operar el sync; si no, el motivo en claro.

    Los textos son los históricos del CLI ``sync`` (E5 de 128: mismos
    mensajes) — la consola les agrega su propia pista de navegación.
    """
    sync_cfg = config.tracking.as400_sync
    if not sync_cfg.enabled:
        return (
            "tracking.as400_sync.enabled=false; "
            "`sync` commands require AS400 sync to be enabled in the YAML."
        )
    ref = config.connection_ref("tracking.as400_sync")
    if ref is None:
        return "tracking.as400_sync.connection is missing"
    if secrets.get(ref.alias) is None:
        user_var, pass_var = ref.env_vars
        return (
            f"AS400 credentials for connection {ref.alias!r} missing in environment "
            f"(set {user_var} / {pass_var})."
        )
    return None


def build_as400_store(config: PipelineConfig, secrets: Secrets) -> As400NiarvilogStore:
    """Construye SOLO el store AS400. Asume ``sync_unavailable_reason`` == None."""
    return build_niarvilog_store(config, secrets)


def build_sync_stores(
    config: PipelineConfig, secrets: Secrets
) -> tuple[SQLiteTrackingStore, As400NiarvilogStore]:
    """Construye ambos stores. Asume ``sync_unavailable_reason`` == None."""
    return SQLiteTrackingStore(config.tracking.db_path), build_as400_store(config, secrets)


def _require_available(config: PipelineConfig, secrets: Secrets) -> None:
    reason = sync_unavailable_reason(config, secrets)
    if reason is not None:
        raise SyncOpError(reason)


def sync_status(
    config: PipelineConfig,
    secrets: Secrets,
    *,
    on_progress: Callable[[SyncProgress], None] | None = None,
) -> StatusResult:
    """Cleanup de in_progress vencidos + **reporte de divergencias**.

    151 REQ-005: es el barrido de :func:`sync_pull` en dry-run, así que no
    escribe una sola fila del tracking local. Lo único que muta es el
    cleanup de los ``'I'`` vencidos, que es idempotente y ya estaba."""
    _require_available(config, secrets)
    sqlite, as400 = build_sync_stores(config, secrets)
    try:
        stale = as400.cleanup_stale_in_progress()
        report = As400Pull(sqlite_store=sqlite, as400_store=as400).pull(
            apply=False, on_progress=on_progress
        )
        return StatusResult(
            stale_cleaned=stale,
            scanned=report.scanned,
            importable=report.imported_uploaded + report.imported_failed,
            divergences=report.divergent,
        )
    finally:
        sqlite.close()
        as400.close()


def sync_pull(
    config: PipelineConfig,
    secrets: Secrets,
    *,
    apply: bool,
    on_progress: Callable[[SyncProgress], None] | None = None,
) -> PullResult:
    """151 REQ-004: trae de NIARVILOG lo que hicieron los otros programas.

    ``apply=False`` (default del CLI) es dry-run, igual que ``recover``.
    El pull sólo rellena huecos: nunca pisa un estado terminal local."""
    _require_available(config, secrets)
    sqlite, as400 = build_sync_stores(config, secrets)
    pull = As400Pull(sqlite_store=sqlite, as400_store=as400)
    try:
        return pull.pull(apply=apply, on_progress=on_progress)
    finally:
        pull.close()  # cierra el store AS400
        sqlite.close()


def sync_recover(
    config: PipelineConfig,
    secrets: Secrets,
    *,
    batch_id: str | None,
    apply: bool,
    on_progress: Callable[[SyncProgress], None] | None = None,
) -> RecoveryResult:
    """099: recupera filas faltantes en NIARVILOG. ``apply=False`` = dry-run.
    144: ``on_progress`` va tal cual a :meth:`As400Recovery.recover`."""
    _require_available(config, secrets)
    sqlite = SQLiteTrackingStore(config.tracking.db_path)
    try:
        recovery = build_as400_recovery(config, secrets, sqlite_store=sqlite)
    except Exception:
        sqlite.close()
        raise
    try:
        return recovery.recover(batch_id=batch_id, apply=apply, on_progress=on_progress)
    finally:
        recovery.close()  # store AS400 + fuente RVABREP del wiring
        sqlite.close()


def sync_resolve(
    config: PipelineConfig,
    secrets: Secrets,
    *,
    txn: str,
    prefer: Literal["as400", "local"],
    cm_object_id: str | None,
) -> str:
    """Resuelve UNA divergencia por TRNNUM. Devuelve el mensaje de resultado."""
    if prefer == "local" and not cm_object_id:
        raise SyncOpError(
            "prefer=local requiere cm_object_id (buscalo con `cmcourier batch show <batch_id>`)."
        )
    _require_available(config, secrets)
    sqlite, as400 = build_sync_stores(config, secrets)
    try:
        row = as400.read_state_by_txn(trnnum=txn)
        if prefer == "as400":
            if row is None:
                raise SyncOpError(f"{txn} not found in AS400 — nothing to import")
            if row.stscod != "O":
                raise SyncOpError(
                    f"{txn} AS400 STSCOD={row.stscod!r}; nothing to import "
                    "(only 'O' rows have a cm_object_id)."
                )
            # El update en SQLite queda como follow-up (ver sync.py): re-correr
            # el pipeline saltea este doc porque AS400 ya dice 'O'.
            return f"resolved {txn}: imported AS400 state — STSCOD='O', OBJIDN={row.objidn!r}"
        if row is None:
            raise SyncOpError(
                f"{txn} not present in AS400 — cannot UPDATE a row that doesn't exist. "
                "Re-run the pipeline to trigger try_claim."
            )
        assert cm_object_id is not None
        rowcount = as400.mark_uploaded_by_txn(trnnum=txn, cm_object_id=cm_object_id)
        if rowcount != 1:
            raise SyncOpError(f"{txn}: expected to UPDATE 1 row, got {rowcount}")
        return f"resolved {txn}: pushed local cm_object_id={cm_object_id!r} to AS400."
    finally:
        sqlite.close()
        as400.close()
