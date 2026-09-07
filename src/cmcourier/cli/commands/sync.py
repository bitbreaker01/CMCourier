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

128: la lógica vive en :mod:`cmcourier.cli.sync_ops` (compartida con
la pestaña SYNC de la consola); acá sólo queda el mapeo a click:
``SyncOpError`` de configuración → exit 2, de uso → exit 1,
``As400CoordinationError`` → exit 3.
"""

from __future__ import annotations

__all__ = ["sync_group"]

import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Literal, TypeVar

import click

from cmcourier.adapters.tracking.as400_niarvilog import As400CoordinationError
from cmcourier.cli.sync_ops import (
    SyncOpError,
    sync_recover,
    sync_resolve,
    sync_status,
    sync_unavailable_reason,
)
from cmcourier.config.loader import Secrets, load_config, load_secrets
from cmcourier.config.schema import PipelineConfig
from cmcourier.domain.exceptions import ConfigurationError

_log = logging.getLogger(__name__)

_T = TypeVar("_T")


@click.group(name="sync")
def sync_group() -> None:
    """Reconcilia el tracking de SQLite con AS400 NIARVILOG (034)."""


# ---------------------------------------------------------------------------
# Setup compartido
# ---------------------------------------------------------------------------


def _load(config_path: Path) -> tuple[PipelineConfig, Secrets]:
    """Carga YAML + secrets y valida que el sync sea operable. Sale con 2."""
    try:
        config = load_config(config_path)
        secrets = load_secrets(config)
    except ConfigurationError as exc:
        click.echo(f"ConfigurationError: {exc}", err=True)
        sys.exit(2)
    reason = sync_unavailable_reason(config, secrets)
    if reason is not None:
        click.echo(f"ConfigurationError: {reason}", err=True)
        sys.exit(2)
    return config, secrets


def _run(op: Callable[[], _T]) -> _T:
    """Mapea las excepciones de ``sync_ops`` a exit codes (1 uso, 3 AS400)."""
    try:
        return op()
    except SyncOpError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)
    except As400CoordinationError as exc:
        click.echo(f"AS400 error: {exc}", err=True)
        sys.exit(3)


_CONFIG_OPTION = click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)


# ---------------------------------------------------------------------------
# sync status
# ---------------------------------------------------------------------------


@sync_group.command(name="status")
@_CONFIG_OPTION
def status_command(config_path: Path) -> None:
    """Reporta el conteo de stale-cleanup + conectividad AS400. Read-only."""
    config, secrets = _load(config_path)
    result = _run(lambda: sync_status(config, secrets))
    click.echo(f"sync status: stale_cleaned={result.stale_cleaned}")


# ---------------------------------------------------------------------------
# sync resolve
# ---------------------------------------------------------------------------


@sync_group.command(name="resolve")
@click.argument("txn", type=str)
@_CONFIG_OPTION
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
    prefer: Literal["as400", "local"] = "as400" if prefer_as400 else "local"
    config, secrets = _load(config_path)
    message = _run(
        lambda: sync_resolve(config, secrets, txn=txn, prefer=prefer, cm_object_id=cm_object_id)
    )
    click.echo(message)


# ---------------------------------------------------------------------------
# sync recover (099)
# ---------------------------------------------------------------------------


@sync_group.command(name="recover")
@_CONFIG_OPTION
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
    config, secrets = _load(config_path)
    result = _run(lambda: sync_recover(config, secrets, batch_id=batch_id, apply=apply_changes))

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
