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
    "SyncOpError",
    "build_sync_stores",
    "sync_recover",
    "sync_resolve",
    "sync_status",
    "sync_unavailable_reason",
]

from dataclasses import dataclass
from typing import Literal

from cmcourier.adapters.tracking import SQLiteTrackingStore
from cmcourier.adapters.tracking.as400_niarvilog import As400NiarvilogStore
from cmcourier.config.loader import Secrets
from cmcourier.config.schema import PipelineConfig
from cmcourier.config.wiring import _niarvilog_columns_from_schema, build_as400_recovery
from cmcourier.services.recovery import RecoveryResult


class SyncOpError(Exception):
    """Mal uso o configuración insuficiente — el mensaje es para el operador."""


@dataclass(frozen=True, slots=True)
class StatusResult:
    stale_cleaned: int


def sync_unavailable_reason(config: PipelineConfig, secrets: Secrets) -> str | None:
    """``None`` si se puede operar el sync; si no, el motivo en claro."""
    sync_cfg = config.tracking.as400_sync
    if not sync_cfg.enabled:
        return (
            "tracking.as400_sync.enabled=false — el sync con NIARVILOG está "
            "deshabilitado en el YAML; estas operaciones no aplican a esta config."
        )
    if sync_cfg.connection is None:
        return "tracking.as400_sync.connection falta en el YAML."
    if not secrets.as400_username or not secrets.as400_password:
        return "Faltan las credenciales AS400 (AS400_USERNAME / AS400_PASSWORD)."
    return None


def build_sync_stores(
    config: PipelineConfig, secrets: Secrets
) -> tuple[SQLiteTrackingStore, As400NiarvilogStore]:
    """Construye los stores. Asume ``sync_unavailable_reason`` == None."""
    sync_cfg = config.tracking.as400_sync
    assert sync_cfg.connection is not None
    sqlite = SQLiteTrackingStore(config.tracking.db_path)
    as400 = As400NiarvilogStore(
        connection=sync_cfg.connection,
        username=secrets.as400_username,
        password=secrets.as400_password,
        library=sync_cfg.library,
        table=sync_cfg.table,
        # 086: honrar el override de columnas del YAML (el CLI pre-086 no lo hacía).
        columns=_niarvilog_columns_from_schema(sync_cfg.columns),
        stale_in_progress_minutes=sync_cfg.stale_in_progress_minutes,
        retry_attempts=sync_cfg.retry_attempts,
        retry_base_delay_s=sync_cfg.retry_base_delay_s,
    )
    return sqlite, as400


def _require_available(config: PipelineConfig, secrets: Secrets) -> None:
    reason = sync_unavailable_reason(config, secrets)
    if reason is not None:
        raise SyncOpError(reason)


def sync_status(config: PipelineConfig, secrets: Secrets) -> StatusResult:
    """Cleanup de in_progress vencidos + prueba de conectividad. Read-only
    salvo por el cleanup (que es idempotente)."""
    _require_available(config, secrets)
    sqlite, as400 = build_sync_stores(config, secrets)
    try:
        return StatusResult(stale_cleaned=as400.cleanup_stale_in_progress())
    finally:
        sqlite.close()
        as400.close()


def sync_recover(
    config: PipelineConfig,
    secrets: Secrets,
    *,
    batch_id: str | None,
    apply: bool,
) -> RecoveryResult:
    """099: recupera filas faltantes en NIARVILOG. ``apply=False`` = dry-run."""
    _require_available(config, secrets)
    sqlite = SQLiteTrackingStore(config.tracking.db_path)
    recovery = build_as400_recovery(config, secrets, sqlite_store=sqlite)
    try:
        return recovery.recover(batch_id=batch_id, apply=apply)
    finally:
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
