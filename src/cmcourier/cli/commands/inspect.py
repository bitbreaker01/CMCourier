"""Subcomandos de ``cmcourier inspect ...``.

* ``inspect rvabrep <shortname> <system_id>``: vista previa de los
  `RVABREPDocuments` que la etapa S1 produciria para un trigger.
* ``inspect mapping <id_rvi>``: vista previa del mapping de CM para
  un `ID RVI`.

Ambos comandos arman solo los servicios minimos que necesitan (sin
uploader, sin assembler, sin capa de metadata) asi salen rapido y
evitan tocar CMIS o el file server.
"""

from __future__ import annotations

__all__ = ["inspect_group"]

import sys
from collections.abc import Callable
from itertools import islice
from pathlib import Path

import click

from cmcourier.adapters.sources import TabularDataSource
from cmcourier.cli.commands._formatting import render_table, truncate
from cmcourier.cli.commands._source_descriptor import (
    ParsedDescriptor,
    parse_source_descriptor,
)
from cmcourier.config.loader import Secrets, load_config, load_secrets
from cmcourier.config.schema import PipelineConfig
from cmcourier.config.wiring import (
    _build_rvabrep_source,
    _build_trigger_strategy,
    _indexing_columns_from_schema,
    build_mapping_service,
)
from cmcourier.domain.exceptions import (
    ConfigurationError,
    IDRViNotMappedError,
    RVABREPDeletedError,
    RVABREPNotFoundError,
)
from cmcourier.domain.models import TriggerRecord
from cmcourier.domain.ports import S0Strategy
from cmcourier.observability.setup import configure as configure_observability
from cmcourier.services.indexing import IndexingService
from cmcourier.services.triggers import (
    CsvTriggerStrategy,
    SingleDocTriggerStrategy,
)


@click.group(name="inspect")
def inspect_group() -> None:
    """Vistas previas read-only del estado del pipeline."""


@inspect_group.command(name="rvabrep")
@click.option(
    "--config",
    "-c",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.argument("shortname", type=str)
@click.argument("system_id", type=str)
def inspect_rvabrep_command(config_path: Path, shortname: str, system_id: str) -> None:
    """Imprime las filas RVABREP que S1 produciria para el trigger."""
    config = _load(config_path)
    configure_observability(config.observability, "INFO")
    try:
        secrets = load_secrets()
    except ConfigurationError:
        secrets = Secrets(cmis_username="", cmis_password="")
    rvabrep_src = _build_rvabrep_source(config.indexing, secrets)
    try:
        indexing = IndexingService(
            rvabrep_src, _indexing_columns_from_schema(config.indexing.columns)
        )
        trigger = TriggerRecord(shortname=shortname, cif=None, system_id=system_id)
        try:
            docs = indexing.find_documents(trigger)
        except (RVABREPNotFoundError, RVABREPDeletedError):
            docs = []
    finally:
        rvabrep_src.close()
    if not docs:
        click.echo("No RVABREP records found", err=True)
        return
    rows = [
        [
            doc.txn_num,
            truncate(doc.file_name, 30),
            doc.index7,
            str(doc.total_pages),
            doc.creation_date.isoformat() if doc.creation_date else "-",
        ]
        for doc in docs
    ]
    click.echo(render_table(["TXN_NUM", "FILE_NAME", "ID_RVI", "PAGES", "CREATED"], rows))


@inspect_group.command(name="mapping")
@click.option(
    "--config",
    "-c",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.argument("id_rvi", type=str)
def inspect_mapping_command(config_path: Path, id_rvi: str) -> None:
    """Imprime el mapping de CM (folder, type, fields) para un `ID RVI`."""
    config = _load(config_path)
    configure_observability(config.observability, "INFO")
    mapping_service = build_mapping_service(config.mapping)
    try:
        mapping = mapping_service.get_mapping(id_rvi)
    except IDRViNotMappedError:
        click.echo(f"No mapping found for ID RVI: {id_rvi}", err=True)
        return
    click.echo(f"ID RVI: {mapping.id_rvi}")
    click.echo(f"Document class: {mapping.clase_name}")
    click.echo(f"CM folder: {mapping.cm_folder}")
    click.echo(f"CM object type: {mapping.cm_object_type}")
    fields = ", ".join(mapping.required_metadata_fields) or "(none)"
    click.echo(f"Required metadata fields: {fields}")


def _load(config_path: Path):  # type: ignore[no-untyped-def]
    try:
        return load_config(config_path)
    except ConfigurationError as exc:
        click.echo(f"ConfigurationError: {exc}", err=True)
        sys.exit(2)


# ---------------------------------------------------------------------------
# inspect mapping-stats (023)
# ---------------------------------------------------------------------------


@inspect_group.command(name="mapping-stats")
@click.option(
    "--config",
    "-c",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
def inspect_mapping_stats_command(config_path: Path) -> None:
    """Imprime un resumen estructurado del `Modelo Documental`."""
    config = _load(config_path)
    configure_observability(config.observability, "INFO")
    mapping_service = build_mapping_service(config.mapping)
    total = mapping_service.count()
    classes: dict[str, int] = {}
    folders: set[str] = set()
    types: set[str] = set()
    id_corto_count = 0
    for mapping in mapping_service.get_all():
        classes[mapping.clase_name] = classes.get(mapping.clase_name, 0) + 1
        folders.add(mapping.cm_folder)
        types.add(mapping.cm_object_type)
        if mapping.id_corto:
            id_corto_count += 1
    click.echo(f"Total mappings: {total}")
    click.echo(f"Distinct document classes: {len(classes)}")
    click.echo(f"Mappings with ID Corto: {id_corto_count} / {total}")
    click.echo(f"Distinct CM object types: {len(types)}")
    click.echo(f"Distinct CM folders: {len(folders)}")
    if classes:
        click.echo("")
        click.echo("Top classes by mapping count:")
        ranked = sorted(classes.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
        rows = [[name, str(count)] for name, count in ranked]
        click.echo(render_table(["CLASS", "COUNT"], rows))


# ---------------------------------------------------------------------------
# inspect trigger (023)
# ---------------------------------------------------------------------------


@inspect_group.command(name="trigger")
@click.option(
    "--config",
    "-c",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--source",
    "source_descriptor",
    type=str,
    default=None,
    help="Override trigger source (csv:<path> or single_doc:SHORT,SYS[,CIF]).",
)
@click.option("--limit", type=click.IntRange(min=1), default=10)
def inspect_trigger_command(
    config_path: Path,
    source_descriptor: str | None,
    limit: int,
) -> None:
    """Vista previa de los primeros N triggers desde un source configurado o ad-hoc."""
    config = _load(config_path)
    configure_observability(config.observability, "INFO")

    strategy, cleanup = _strategy_for_inspect(config, source_descriptor)
    try:
        records = list(islice(strategy.acquire(""), limit))
    finally:
        cleanup()

    if not records:
        click.echo("No triggers produced", err=True)
        return
    # 046: los triggers son polimorficos; usamos `audit_row()` para
    # proyectar el triple `(shortname, cif, system_id)` best-effort para
    # mostrar.
    rows = []
    for r in records:
        a = r.audit_row()
        rows.append([a.get("shortname") or "-", a.get("cif") or "-", a.get("system_id") or "-"])
    click.echo(render_table(["SHORTNAME", "CIF", "SYSTEM_ID"], rows))


def _strategy_for_inspect(
    config: PipelineConfig,
    source_descriptor: str | None,
) -> tuple[S0Strategy, Callable[[], None]]:
    """Construye un `S0Strategy` + un callable de cleanup para cerrar los sources abiertos."""
    if source_descriptor is None:
        return _strategy_from_config(config)
    try:
        parsed = parse_source_descriptor(source_descriptor)
    except ConfigurationError as exc:
        click.echo(f"ConfigurationError: {exc}", err=True)
        sys.exit(2)
    return _strategy_from_descriptor(parsed)


def _strategy_from_descriptor(
    parsed: ParsedDescriptor,
) -> tuple[S0Strategy, Callable[[], None]]:
    if parsed.scheme == "csv":
        assert parsed.path is not None
        if not parsed.path.exists():
            click.echo(
                f"ConfigurationError: csv source path does not exist: {parsed.path}",
                err=True,
            )
            sys.exit(2)
        src = TabularDataSource(parsed.path)
        return CsvTriggerStrategy(src), src.close
    if parsed.scheme == "single_doc":
        strategy = SingleDocTriggerStrategy(
            shortname=parsed.shortname,
            system_id=parsed.system_id,
            cif=parsed.cif,
        )
        return strategy, lambda: None
    # `parse_source_descriptor` ya rechazo otros esquemas; defensivo.
    raise AssertionError(f"unhandled scheme: {parsed.scheme!r}")


def _strategy_from_config(
    config: PipelineConfig,
) -> tuple[S0Strategy, Callable[[], None]]:
    """Construye una `strategy` desde ``config.trigger`` usando el helper de wiring existente.

    `inspect` es read-only y NO requiere credenciales de CMIS; solo los
    `trigger kinds` de AS400 necesitan ``AS400_USERNAME`` /
    ``AS400_PASSWORD``. Probamos el loader de secrets completo pero
    caemos a un bundle `Secrets` vacio asi las configs
    csv/single_doc/rvabrep/local_scan Just Work sin env vars.
    """
    try:
        secrets = load_secrets()
    except ConfigurationError:
        secrets = Secrets(cmis_username="", cmis_password="")
    rvabrep_src = _build_rvabrep_source(config.indexing, secrets)
    indexing = IndexingService(rvabrep_src, _indexing_columns_from_schema(config.indexing.columns))
    try:
        strategy = _build_trigger_strategy(config, secrets, rvabrep_src, indexing)
    except ConfigurationError as exc:
        rvabrep_src.close()
        click.echo(f"ConfigurationError: {exc}", err=True)
        sys.exit(2)
    return strategy, rvabrep_src.close
