"""Suite del subcomando ``cmcourier sync`` (034 fase 4).

Los operadores la usan para reconciliar la divergencia entre el
tracking store local de SQLite y la tabla centralizada AS400
``RVILIB.NIARVILOG``.

151 REQ-001 — la regla de autoridad que gobierna las dos direcciones:

    El lado local manda sobre los documentos que CMCourier procesó.
    El AS400 manda sobre los documentos que CMCourier nunca vio.

``recover`` (local → AS400) corrige lo que hicimos nosotros; ``pull``
(AS400 → local) sólo rellena huecos y NUNCA pisa un estado terminal
local. Cuando los dos afirman cosas distintas sobre el mismo documento,
ninguna dirección decide sola: se reporta como divergencia y la resuelve
el operador con ``sync resolve``.

Subcomandos:

* ``sync status``: cleanup pre-flight + **reporte de divergencias**, sin
  tocar el tracking local (151 REQ-005: antes decía reportarlas y sólo
  limpiaba los ``'I'`` vencidos).
* ``sync pull``: (151) trae de NIARVILOG los documentos que subió — o
  rompió — otro programa de la migración. Dry-run por defecto;
  ``--apply`` escribe en SQLite.
* ``sync resolve <txn> --prefer-as400``: trae el estado terminal de
  AS400 hacia SQLite (``S5_DONE`` con el ``cm_object_id`` que duena
  AS400).
* ``sync resolve <txn> --prefer-local``: empuja el estado terminal
  de SQLite hacia AS400 (``UPDATE STSCOD='O', OBJIDN=?`` sobre la
  fila existente).
* ``sync recover``: (099) recupera filas faltantes en NIARVILOG para
  documentos ya subidos a CM — repara el daño del bug 096 — y (151)
  ACTUALIZA las que quedaron desactualizadas. Dry-run por defecto;
  ``--apply`` ejecuta los INSERT y los UPDATE.

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
    sync_pull,
    sync_recover,
    sync_resolve,
    sync_status,
    sync_unavailable_reason,
)
from cmcourier.config.loader import Secrets, load_config, load_secrets
from cmcourier.config.schema import PipelineConfig
from cmcourier.domain.exceptions import ConfigurationError
from cmcourier.services.pull import PullItem
from cmcourier.services.recovery import SyncProgress

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
    """Stale-cleanup + divergencias entre SQLite y NIARVILOG. Read-only.

    151 REQ-005: el barrido de ``sync pull`` en dry-run. No escribe una
    sola fila del tracking local; lo único que muta es el cleanup de los
    ``'I'`` vencidos, que es idempotente.
    """
    config, secrets = _load(config_path)
    result = _run(lambda: sync_status(config, secrets, on_progress=_echo_progress))
    click.echo(
        f"sync status: stale_cleaned={result.stale_cleaned} "
        f"escaneadas={result.scanned} importables={result.importable} "
        f"divergentes={len(result.divergences)}"
    )
    _echo_divergences(result.divergences)
    if result.importable:
        click.echo(
            f"{result.importable} fila(s) del AS400 no están en el tracking local. "
            "Traelas con `cmcourier sync pull --apply`."
        )


# ---------------------------------------------------------------------------
# sync pull (151 REQ-004)
# ---------------------------------------------------------------------------


@sync_group.command(name="pull")
@_CONFIG_OPTION
@click.option(
    "--apply",
    "apply_changes",
    is_flag=True,
    default=False,
    help="Escribe las filas importadas en SQLite. Sin este flag, dry-run (solo reporta).",
)
def pull_command(config_path: Path, apply_changes: bool) -> None:
    """Trae de NIARVILOG lo que hicieron los otros programas (151).

    NIARVILOG es el punto de coordinación entre CMCourier y el proceso
    Java del banco. ``pull`` importa al tracking local los documentos que
    el otro programa subió (``STSCOD='O'`` → ``S5_DONE``) o rompió
    (``'F'`` → ``S5_FAILED`` + ``EXTERNAL_FAILURE``), bajo un batch
    sintético para no ensuciar el censo de un batch real.

    **Nunca pisa un estado terminal local** (REQ-001): si los dos lados
    afirman cosas distintas, se reporta la divergencia y la resuelve el
    operador. **Dry-run por defecto** — usá ``--apply`` para escribir.
    """
    config, secrets = _load(config_path)
    result = _run(
        lambda: sync_pull(config, secrets, apply=apply_changes, on_progress=_echo_progress)
    )
    # El tag [DRY-RUN] / [APPLY] ya dice si se escribió: los nombres de los
    # campos se quedan quietos para que un `grep` del operador sirva igual
    # en los dos modos.
    mode = "APPLY" if apply_changes else "DRY-RUN"
    click.echo(
        f"sync pull [{mode}]: escaneadas={result.scanned} "
        f"importadas_ok={result.imported_uploaded} "
        f"importadas_fallidas={result.imported_failed} "
        f"consistentes={result.consistent} "
        f"divergentes={len(result.divergent)}"
    )
    _echo_divergences(result.divergent)
    total = result.imported_uploaded + result.imported_failed
    if not apply_changes and total:
        click.echo(
            f"dry-run: {total} fila(s) se importarían al tracking local. "
            "Re-corré con --apply para escribir."
        )


def _echo_divergences(items: list[PullItem]) -> None:
    """151 REQ-001: las divergencias se NOMBRAN. Ninguna dirección decide
    sola cuál de las dos verdades gana — eso es exactamente lo que no
    queremos."""
    for item in items:
        click.echo(f"  divergente {item.txn_num}: {item.reason}", err=True)
    if items:
        click.echo(
            f"{len(items)} divergencia(s) sin resolver. Ninguna dirección las pisa: "
            "elegí con `cmcourier sync resolve <txn> --prefer-as400|--prefer-local`."
        )


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


def _echo_progress(event: SyncProgress) -> None:
    """144: una línea por evento en stderr — stdout queda para el reporte
    (``sync recover > plan.txt`` no se ensucia)."""
    click.echo(f"{event.phase} {event.done}/{event.total}", err=True)


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
    result = _run(
        lambda: sync_recover(
            config, secrets, batch_id=batch_id, apply=apply_changes, on_progress=_echo_progress
        )
    )

    mode = "APPLY" if apply_changes else "DRY-RUN"
    verbo = "recuperadas" if apply_changes else "a recuperar"
    # 151 REQ-003: los tres grupos se nombran por separado. El viejo
    # ``already_present`` mezclaba las filas consistentes con las
    # DESACTUALIZADAS detrás de un conteo que sonaba a éxito.
    click.echo(
        f"sync recover [{mode}]: {verbo}={len(result.recovered)} "
        f"actualizadas={len(result.updated)} "
        f"consistentes={len(result.consistent)} "
        f"divergentes={len(result.divergent)} "
        f"unrecoverable={len(result.unrecoverable)}"
    )
    for item in result.divergent:
        click.echo(f"  divergente {item.txn_num}: {item.reason}", err=True)
    for item in result.unrecoverable:
        click.echo(f"  unrecoverable {item.txn_num}: {item.reason}", err=True)
    if result.divergent:
        click.echo(
            f"{len(result.divergent)} divergencia(s) NO se tocan: el AS400 dice 'O' con otro "
            "OBJIDN. Resolvelas con `cmcourier sync resolve <txn> --prefer-as400|--prefer-local`."
        )
    if not apply_changes and (result.recovered or result.updated):
        click.echo(
            f"dry-run: {len(result.recovered)} fila(s) se insertarían y "
            f"{len(result.updated)} se actualizarían en NIARVILOG. "
            "Re-corré con --apply para ejecutar."
        )
