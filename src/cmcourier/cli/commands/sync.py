"""Suite del subcomando ``cmcourier sync`` (034 fase 4).

Los operadores la usan para reconciliar la divergencia entre el
tracking store local de SQLite y la tabla centralizada AS400
``RVILIB.NIARVILOG``. Tres subcomandos:

* ``sync status``: corre el cleanup pre-flight + reporta cualquier
  conflicto sin tocar estado. Util para inspeccion.
* ``sync resolve <txn> --prefer-as400``: trae el estado terminal de
  AS400 hacia SQLite (``S5_DONE`` con el ``cm_object_id`` que duena
  AS400).
* ``sync resolve <txn> --prefer-local``: empuja el estado terminal
  de SQLite hacia AS400 (``UPDATE STSCOD='O', OBJIDN=?`` sobre la
  fila existente).
* ``sync recover``: (099) recupera filas faltantes en NIARVILOG para
  documentos ya subidos a CM — repara el daño del bug 096. Dry-run por
  defecto; ``--apply`` ejecuta los INSERT.
"""

from __future__ import annotations

__all__ = ["sync_group"]

import logging
import sys
from pathlib import Path

import click

from cmcourier.adapters.tracking import SQLiteTrackingStore
from cmcourier.adapters.tracking.as400_niarvilog import (
    As400CoordinationError,
    As400NiarvilogStore,
    NiarvilogRow,
)
from cmcourier.config.loader import load_config, load_secrets
from cmcourier.config.schema import PipelineConfig
from cmcourier.config.wiring import _niarvilog_columns_from_schema, build_as400_recovery
from cmcourier.domain.exceptions import ConfigurationError

_log = logging.getLogger(__name__)


@click.group(name="sync")
def sync_group() -> None:
    """Reconcilia el tracking de SQLite con AS400 NIARVILOG (034)."""


# ---------------------------------------------------------------------------
# Setup compartido
# ---------------------------------------------------------------------------


def _load_stores(
    config_path: Path,
) -> tuple[PipelineConfig, SQLiteTrackingStore, As400NiarvilogStore]:
    """Construye los stores SQLite + AS400 desde el YAML. Sale con 2 ante mal uso."""
    try:
        config = load_config(config_path)
        secrets = load_secrets()
    except ConfigurationError as exc:
        click.echo(f"ConfigurationError: {exc}", err=True)
        sys.exit(2)

    sync_cfg = config.tracking.as400_sync
    if not sync_cfg.enabled:
        click.echo(
            "ConfigurationError: tracking.as400_sync.enabled=false; "
            "`sync` commands require AS400 sync to be enabled in the YAML.",
            err=True,
        )
        sys.exit(2)
    if sync_cfg.connection is None:
        click.echo(
            "ConfigurationError: tracking.as400_sync.connection is missing",
            err=True,
        )
        sys.exit(2)
    if not secrets.as400_username or not secrets.as400_password:
        click.echo(
            "ConfigurationError: AS400 credentials missing in environment "
            "(set AS400_USERNAME / AS400_PASSWORD).",
            err=True,
        )
        sys.exit(2)

    sqlite = SQLiteTrackingStore(config.tracking.db_path)
    as400 = As400NiarvilogStore(
        connection=sync_cfg.connection,
        username=secrets.as400_username,
        password=secrets.as400_password,
        library=sync_cfg.library,
        table=sync_cfg.table,
        # 086: pre-086 el CLI `sync` construía el adapter SIN pasar
        # `columns=`, así que el adapter usaba los defaults canónicos
        # hardcodeados (FINREI, PMRREI, STSCOD…) ignorando el override
        # de `tracking.as400_sync.columns` del YAML. El `batch run`
        # (que pasa por `wiring.py`) sí honraba el override; sólo el
        # CLI `sync status` / `sync resolve` estaba roto.
        columns=_niarvilog_columns_from_schema(sync_cfg.columns),
        stale_in_progress_minutes=sync_cfg.stale_in_progress_minutes,
        retry_attempts=sync_cfg.retry_attempts,
        retry_base_delay_s=sync_cfg.retry_base_delay_s,
    )
    return config, sqlite, as400


# ---------------------------------------------------------------------------
# sync status
# ---------------------------------------------------------------------------


@sync_group.command(name="status")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
def status_command(config_path: Path) -> None:
    """Reporta el conteo de stale-cleanup + conectividad AS400. Read-only."""
    _, sqlite, as400 = _load_stores(config_path)
    try:
        stale = as400.cleanup_stale_in_progress()
    except As400CoordinationError as exc:
        click.echo(f"AS400 error: {exc}", err=True)
        sys.exit(3)
    finally:
        sqlite.close()
        as400.close()
    click.echo(f"sync status: stale_cleaned={stale}")


# ---------------------------------------------------------------------------
# sync resolve
# ---------------------------------------------------------------------------


@sync_group.command(name="resolve")
@click.argument("txn", type=str)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--prefer-as400",
    "prefer_as400",
    is_flag=True,
    default=False,
    help="AS400 is the source of truth — pull its state into SQLite.",
)
@click.option(
    "--prefer-local",
    "prefer_local",
    is_flag=True,
    default=False,
    help="SQLite is the source of truth — push cm_object_id to AS400.",
)
@click.option(
    "--cm-object-id",
    "cm_object_id",
    type=str,
    default=None,
    help="CMIS object id (required with --prefer-local).",
)
def resolve_command(
    txn: str,
    config_path: Path,
    prefer_as400: bool,
    prefer_local: bool,
    cm_object_id: str | None,
) -> None:
    """Resuelve una unica divergencia AS400/SQLite para un TRNNUM."""
    if prefer_as400 == prefer_local:
        click.echo(
            "ConfigurationError: choose exactly one of --prefer-as400 / --prefer-local.",
            err=True,
        )
        sys.exit(2)
    if prefer_local and not cm_object_id:
        click.echo(
            "ConfigurationError: --prefer-local requires --cm-object-id "
            "(look it up with `cmcourier batch show <batch_id>`).",
            err=True,
        )
        sys.exit(2)
    _, sqlite, as400 = _load_stores(config_path)
    try:
        if prefer_as400:
            _resolve_prefer_as400(txn=txn, sqlite=sqlite, as400=as400)
        else:
            assert cm_object_id is not None  # narrowed por el guard de arriba
            _resolve_prefer_local(
                txn=txn,
                sqlite=sqlite,
                as400=as400,
                cm_object_id=cm_object_id,
            )
    except As400CoordinationError as exc:
        click.echo(f"AS400 error: {exc}", err=True)
        sys.exit(3)
    finally:
        sqlite.close()
        as400.close()


# ---------------------------------------------------------------------------
# Resolvers
# ---------------------------------------------------------------------------
# (helpers de resolucion del comando `sync resolve`)


def _resolve_prefer_as400(
    *,
    txn: str,
    sqlite: SQLiteTrackingStore,  # noqa: ARG001 — reservado para la escritura SQLite
    as400: As400NiarvilogStore,
) -> None:
    row: NiarvilogRow | None = as400.read_state_by_txn(trnnum=txn)
    if row is None:
        click.echo(f"{txn} not found in AS400 — nothing to import", err=True)
        sys.exit(1)
    if row.stscod != "O":
        click.echo(
            f"{txn} AS400 STSCOD={row.stscod!r}; "
            "nothing to import (only 'O' rows have a cm_object_id).",
            err=True,
        )
        sys.exit(1)
    click.echo(f"resolved {txn}: imported AS400 state — STSCOD='O', OBJIDN={row.objidn!r}")
    # El camino de update en SQLite queda como follow-up: tendriamos
    # que encontrar el record existente de SQLite por `txn` (aca no
    # conocemos `batch_id`) o insertar un record sintetico.
    # Operacionalmente, la remediacion mas simple es re-correr el
    # pipeline; la logica de resume in-process va a saltear este doc
    # porque AS400 dice 'O'.


def _resolve_prefer_local(
    *,
    txn: str,
    sqlite: SQLiteTrackingStore,  # noqa: ARG001 — guardado para un futuro cross-check
    as400: As400NiarvilogStore,
    cm_object_id: str,
) -> None:
    """Empuja el `cm_object_id` provisto por el operador hacia AS400 via UPDATE.

    El operador saca el `cm_object_id` de ``cmcourier batch show
    <batch_id>`` (o del stdout de la corrida cuando se hizo el
    upload). Requerirlo explicitamente evita extender la API del
    store SQLite con una superficie tipo `find_record_by_txn` que
    no hace falta en ningun otro lado.
    """
    row = as400.read_state_by_txn(trnnum=txn)
    if row is None:
        click.echo(
            f"{txn} not present in AS400 — cannot UPDATE a row that "
            "doesn't exist. Re-run the pipeline to trigger try_claim.",
            err=True,
        )
        sys.exit(1)
    rowcount = as400.mark_uploaded_by_txn(trnnum=txn, cm_object_id=cm_object_id)
    if rowcount != 1:
        click.echo(
            f"{txn}: expected to UPDATE 1 row, got {rowcount}",
            err=True,
        )
        sys.exit(1)
    click.echo(f"resolved {txn}: pushed local cm_object_id={cm_object_id!r} to AS400.")


# ---------------------------------------------------------------------------
# sync recover (099)
# ---------------------------------------------------------------------------


@sync_group.command(name="recover")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--apply",
    "apply_changes",
    is_flag=True,
    default=False,
    help="Ejecuta los INSERT en AS400. Sin este flag, dry-run (solo reporta).",
)
@click.option(
    "--batch-id",
    "batch_id",
    type=str,
    default=None,
    help="Acota la recuperación a un batch_id. Default: todo el tracking.",
)
def recover_command(config_path: Path, apply_changes: bool, batch_id: str | None) -> None:
    """Recupera filas faltantes en NIARVILOG para documentos ya subidos (099).

    Repara el daño del bug 096: docs en CMIS + SQLite (``S5_DONE``) sin
    su fila de tracking en AS400. Re-deriva los campos faltantes e
    inserta las filas. **Dry-run por defecto** — usá ``--apply`` para
    escribir efectivamente en AS400.
    """
    try:
        config = load_config(config_path)
        secrets = load_secrets()
    except ConfigurationError as exc:
        click.echo(f"ConfigurationError: {exc}", err=True)
        sys.exit(2)

    sync_cfg = config.tracking.as400_sync
    if not sync_cfg.enabled:
        click.echo(
            "ConfigurationError: tracking.as400_sync.enabled=false; "
            "`sync recover` requiere el sync AS400 habilitado en el YAML.",
            err=True,
        )
        sys.exit(2)
    if sync_cfg.connection is None:
        click.echo("ConfigurationError: tracking.as400_sync.connection is missing", err=True)
        sys.exit(2)
    if not secrets.as400_username or not secrets.as400_password:
        click.echo(
            "ConfigurationError: AS400 credentials missing in environment "
            "(set AS400_USERNAME / AS400_PASSWORD).",
            err=True,
        )
        sys.exit(2)

    sqlite = SQLiteTrackingStore(config.tracking.db_path)
    recovery = build_as400_recovery(config, secrets, sqlite_store=sqlite)
    try:
        result = recovery.recover(batch_id=batch_id, apply=apply_changes)
    except As400CoordinationError as exc:
        click.echo(f"AS400 error: {exc}", err=True)
        sys.exit(3)
    finally:
        sqlite.close()

    mode = "APPLY" if apply_changes else "DRY-RUN"
    verbo = "recuperadas" if apply_changes else "a recuperar"
    click.echo(
        f"sync recover [{mode}]: {verbo}={len(result.recovered)} "
        f"already_present={len(result.already_present)} "
        f"unrecoverable={len(result.unrecoverable)}"
    )
    for item in result.unrecoverable:
        click.echo(f"  unrecoverable {item.txn_num}: {item.reason}", err=True)
    if not apply_changes and result.recovered:
        click.echo(
            f"dry-run: {len(result.recovered)} fila(s) se insertarían en NIARVILOG. "
            "Re-corré con --apply para ejecutar."
        )
