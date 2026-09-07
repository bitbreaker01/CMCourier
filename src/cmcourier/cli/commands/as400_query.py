"""``cmcourier as400-query "<SQL>"``: query crudo de debug contra AS400.

Solo para debug. Corre el SQL provisto contra la conexion AS400
configurada en el YAML (prefiriendo ``indexing.source.connection``,
con fallback al primer ``metadata.sources[*]`` de kind ``as400``).
Se niega a correr sin credenciales de AS400 en el environment.

Disciplina de PII: las filas resultado se hacen echo tal cual a
stdout, con truncado por celda en 80 caracteres. El operador es
responsable de lo que querea.
"""

from __future__ import annotations

__all__ = ["as400_query_command"]

import logging
import sys
from pathlib import Path

import click

from cmcourier.adapters.sources import As400DataSource
from cmcourier.cli.commands._formatting import render_table, truncate
from cmcourier.config.loader import load_config, load_secrets
from cmcourier.config.schema import As400ConnectionConfig, ConnectionRef, PipelineConfig
from cmcourier.domain.exceptions import ConfigurationError, IndexingError
from cmcourier.observability.setup import configure as configure_observability

_log = logging.getLogger(__name__)


@click.command(name="as400-query")
@click.option(
    "--config",
    "-c",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.argument("sql", type=str)
def as400_query_command(config_path: Path, sql: str) -> None:
    """Corre una query SQL cruda contra AS400 (solo debug)."""
    try:
        config = load_config(config_path)
        secrets = load_secrets(config)
    except ConfigurationError as exc:
        click.echo(f"ConfigurationError: {exc}", err=True)
        sys.exit(2)

    configure_observability(config.observability, "INFO")

    ref = _select_as400_connection(config)
    if ref is None or not isinstance(ref.spec, As400ConnectionConfig):
        click.echo(
            "ConfigurationError: no AS400 connection configured (need indexing.source of "
            "kind=as400 or a metadata.sources[*] of kind=as400)",
            err=True,
        )
        sys.exit(2)
    connection = ref.spec
    credential = secrets.get(ref.alias)
    if credential is None:
        user_var, pass_var = ref.env_vars
        click.echo(
            f"ConfigurationError: {user_var} and {pass_var} must be set in the environment "
            f"(connection {ref.alias!r})",
            err=True,
        )
        sys.exit(2)

    _log.warning(
        "as400-query: raw SQL execution requested; result cells may contain PII",
        extra={"sql_prefix": sql[:80]},
    )

    source = As400DataSource(
        host=connection.host,
        port=connection.port,
        database=connection.database,
        driver=connection.driver,
        username=credential.username,
        password=credential.password,
    )
    try:
        try:
            rows = source.query(sql, [])
        except IndexingError as exc:
            click.echo(f"AS400 error: {exc}", err=True)
            sys.exit(1)
    finally:
        source.close()

    if not rows:
        click.echo("(0 rows)")
        return
    headers = list(rows[0].keys())
    body = [[truncate(str(row.get(h, "")), 80) for h in headers] for row in rows]
    click.echo(render_table(headers, body))
    click.echo(f"({len(rows)} rows)")


def _select_as400_connection(config: PipelineConfig) -> ConnectionRef | None:
    # 048: el source RVABREP puede ser AS400 (``indexing.source.kind: as400``).
    # Ese es el primer lugar donde buscamos una conexion para querear en
    # debug; después, la primera fuente de metadata as400. 129: se resuelve
    # vía `connection_refs()` (inline o alias del registro por igual).
    refs = [ref for ref in config.connection_refs() if ref.kind == "as400"]
    return next((ref for ref in refs if ref.site == "indexing"), refs[0] if refs else None)
