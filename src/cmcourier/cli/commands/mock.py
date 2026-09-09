"""``cmcourier mock generate``: generador de file-tree RVABREP sintetico (031).

Front-end hacia el operador de :mod:`cmcourier.services.mock`. Lee filas
RVABREP de CSV (:class:`cmcourier.adapters.sources.TabularDataSource`)
o AS400 (:class:`cmcourier.adapters.sources.As400DataSource`), las
traduce en objetos :class:`cmcourier.services.mock.types.FilePlan` via
el planner puro y escribe bytes PDF/TIFF/JPEG validos via
:class:`cmcourier.services.mock.content.MockContentWriter`.

Codigos de salida (matchean al resto del CLI, REQ-032):
    0 = exito
    2 = error de configuracion (sufijo de tamano invalido, banda
        invertida, sin source, ...)
    3 = excepcion no manejada
"""

from __future__ import annotations

__all__ = ["mock_group"]

import logging
import sys
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import click

from cmcourier.adapters.sources import As400DataSource, TabularDataSource
from cmcourier.config.loader import load_config, load_secrets
from cmcourier.config.schema import (
    As400ConnectionConfig,
    As400RvabrepSource,
    IndexingColumnsModel,
    PipelineConfig,
)
from cmcourier.domain.exceptions import ConfigurationError
from cmcourier.domain.ports import IDataSource
from cmcourier.services.mock.content import MockContentWriter
from cmcourier.services.mock.planner import (
    PlannerFilters,
    SizeBounds,
    plan_files,
)
from cmcourier.services.mock.rvabrep_generator import (
    ImageMix,
    RvabrepGenSpec,
    generate_rvabrep,
)
from cmcourier.services.mock.sizing import parse_size
from cmcourier.services.mock.types import FilePlan

_log = logging.getLogger(__name__)


@click.group(name="mock")
def mock_group() -> None:
    """Generador de file-tree sintetico para dry runs y tests de integracion (031)."""


@mock_group.command(name="generate")
@click.option(
    "--rvabrep-csv",
    "rvabrep_csv",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to a CSV file with RVABREP rows.",
)
@click.option(
    "--rvabrep-as400",
    "rvabrep_as400",
    is_flag=True,
    default=False,
    help="Read RVABREP rows from AS400 (requires --config with as400_connection).",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to a pipeline YAML config (required for --rvabrep-as400; "
    "optional for CSV to honor column overrides).",
)
@click.option(
    "--root",
    "root",
    type=click.Path(path_type=Path),
    required=True,
    help="Root directory under which the mock tree is materialized.",
)
@click.option("--pdf-min", "pdf_min", required=True, help="PDF min size, e.g. 10kb.")
@click.option("--pdf-max", "pdf_max", required=True, help="PDF max size, e.g. 2mb.")
@click.option("--img-min", "img_min", required=True, help="Image min size, e.g. 5kb.")
@click.option("--img-max", "img_max", required=True, help="Image max size, e.g. 500kb.")
@click.option("--limit", type=int, default=None, help="Cap on planned files.")
@click.option(
    "--system",
    "systems",
    multiple=True,
    help="Filter on ABAACD (system_code); repeatable.",
)
@click.option(
    "--document-type",
    "document_types",
    multiple=True,
    help="Filter on ABAHCD (id_rvi); repeatable.",
)
@click.option("--seed", type=int, default=None, help="Seed for deterministic randomness.")
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Print the plan; write nothing.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite existing files (default skips when all targets exist).",
)
@click.option(
    "--include-deleted",
    "include_deleted",
    is_flag=True,
    default=False,
    help="Include RVABREP rows with non-empty delete_code (ABACST).",
)
def generate_command(  # noqa: PLR0913 — las opciones de Click dictan la firma
    rvabrep_csv: Path | None,
    rvabrep_as400: bool,
    config_path: Path | None,
    root: Path,
    pdf_min: str,
    pdf_max: str,
    img_min: str,
    img_max: str,
    limit: int | None,
    systems: tuple[str, ...],
    document_types: tuple[str, ...],
    seed: int | None,
    dry_run: bool,
    force: bool,
    include_deleted: bool,
) -> None:
    """Materializa un file tree mock valido desde un source RVABREP."""
    try:
        bounds = _parse_bounds(pdf_min, pdf_max, img_min, img_max)
        config = _load_config_optional(config_path)
        columns = config.indexing.columns if config is not None else IndexingColumnsModel()
        source = _build_source(rvabrep_csv, rvabrep_as400, config)
    except ConfigurationError as exc:
        click.echo(f"ConfigurationError: {exc}", err=True)
        sys.exit(2)
    except ValueError as exc:
        click.echo(f"ConfigurationError: {exc}", err=True)
        sys.exit(2)
    except FileNotFoundError as exc:
        click.echo(f"FileNotFoundError: {exc}", err=True)
        sys.exit(2)

    filters = PlannerFilters(
        systems=tuple(systems),
        document_types=tuple(document_types),
        limit=limit,
    )

    try:
        plans = plan_files(
            source.get_all(),
            columns,
            filters,
            bounds,
            include_deleted=include_deleted,
        )
        if dry_run:
            _emit_plan(plans, root)
            return
        created, skipped, total_bytes = _materialize(plans, root, seed=seed, force=force)
        click.echo(f"wrote {created} files ({skipped} skipped, {total_bytes} total)")
    except ConfigurationError as exc:
        click.echo(f"ConfigurationError: {exc}", err=True)
        sys.exit(2)
    except Exception:  # noqa: BLE001 — handler de CLI de nivel superior
        _log.exception("mock generate failed")
        sys.exit(3)
    finally:
        source.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
# (helpers internos del comando `mock`)


def _parse_bounds(
    pdf_min_text: str, pdf_max_text: str, img_min_text: str, img_max_text: str
) -> SizeBounds:
    pdf_min = parse_size(pdf_min_text)
    pdf_max = parse_size(pdf_max_text)
    img_min = parse_size(img_min_text)
    img_max = parse_size(img_max_text)
    if pdf_min > pdf_max:
        raise ConfigurationError(
            "--pdf-min must be <= --pdf-max",
            pdf_min=pdf_min,
            pdf_max=pdf_max,
        )
    if img_min > img_max:
        raise ConfigurationError(
            "--img-min must be <= --img-max",
            img_min=img_min,
            img_max=img_max,
        )
    return SizeBounds(pdf_min=pdf_min, pdf_max=pdf_max, img_min=img_min, img_max=img_max)


def _load_config_optional(config_path: Path | None) -> PipelineConfig | None:
    if config_path is None:
        return None
    return load_config(config_path)


def _build_source(
    rvabrep_csv: Path | None,
    rvabrep_as400: bool,
    config: PipelineConfig | None,
) -> IDataSource:
    if rvabrep_csv is None and not rvabrep_as400:
        raise ConfigurationError(
            "exactly one of --rvabrep-csv or --rvabrep-as400 is required",
        )
    if rvabrep_csv is not None and rvabrep_as400:
        raise ConfigurationError(
            "--rvabrep-csv and --rvabrep-as400 are mutually exclusive",
        )
    if rvabrep_csv is not None:
        return TabularDataSource(rvabrep_csv)
    # camino `--rvabrep-as400`
    if config is None:
        raise ConfigurationError(
            "--rvabrep-as400 requires --config with an AS400 connection",
        )
    source_cfg = _extract_as400_source(config)
    ref = config.connection_ref("indexing")
    if ref is None or not isinstance(ref.spec, As400ConnectionConfig):  # pragma: no cover
        raise ConfigurationError("indexing.source has no AS400 connection")
    conn = ref.spec
    # 129: la credencial es la del alias de la conexión (inline → `as400`).
    # Sólo se lee el AS400: las env vars de CMIS no hacen falta acá.
    credential = load_secrets(config, require_cmis=False).require(ref.alias)
    # 073: si el operador definió ``source.query`` con WHERE / JOIN /
    # filtros, respetarlo — el adapter lo wrappea como ``(query) AS T``.
    # Pre-073 esto se ignoraba y ``mock generate --rvabrep-as400`` leía
    # la tabla entera siempre.
    query = source_cfg.query.strip() if source_cfg.query else ""
    if query:
        return As400DataSource(
            host=conn.host,
            port=conn.port,
            database=conn.database,
            driver=conn.driver,
            username=credential.username,
            password=credential.password,
            query=query,
        )
    # 073: cuando ni ``source.query`` ni ``connection.table`` están
    # seteados, el fallback prepende el ``database`` como schema
    # (``RVILIB.RVABREP``). Pre-073 el default era ``"RVABREP"`` bare,
    # que fallaba con ``table not found`` cuando la library no estaba
    # en la library list del usuario AS400.
    return As400DataSource(
        host=conn.host,
        port=conn.port,
        database=conn.database,
        driver=conn.driver,
        username=credential.username,
        password=credential.password,
        table=conn.table or f"{conn.database}.RVABREP",
    )


def _extract_as400_source(config: PipelineConfig) -> As400RvabrepSource:
    # 048: la conexion AS400 para el source RVABREP vive ahora bajo
    # ``indexing.source``, no bajo la config del trigger.
    # 073: devolvemos el ``source`` entero (no solo la connection) para
    # que el caller pueda acceder al ``query`` configurado.
    source = config.indexing.source
    if isinstance(source, As400RvabrepSource):
        return source
    raise ConfigurationError(
        "config does not define an AS400 indexing source — set "
        "indexing.source.kind: as400 with a connection block",
        source_kind=getattr(source, "kind", "<unknown>"),
    )


def _emit_plan(plans: Iterator[FilePlan], root: Path) -> None:
    for plan in plans:
        for ext in plan.extensions:
            rel = plan.dir_path / f"{plan.file_code}{ext}"
            click.echo(
                f"[plan] {root / rel}  kind={plan.kind}  pages={plan.pages}  "
                f"size={plan.size_min}..{plan.size_max}"
            )


def _materialize(
    plans: Iterator[FilePlan],
    root: Path,
    *,
    seed: int | None,
    force: bool,
) -> tuple[int, int, int]:
    writer = MockContentWriter(seed=seed)
    created = 0
    skipped = 0
    total_bytes = 0
    for plan in plans:
        target_dir = root / plan.dir_path
        written = writer.write(plan, target_dir, force=force)
        if not written:
            skipped += len(plan.extensions)
            continue
        created += len(written)
        total_bytes += sum(p.stat().st_size for p in written)
    return created, skipped, total_bytes


# ---------------------------------------------------------------------------
# 039 — `cmcourier mock rvabrep` (generador sintetico de CSV RVABREP)
# ---------------------------------------------------------------------------


_DEFAULT_IDRVI_SOURCE = Path("reference-data/csv/MapeoRVI_CM.csv")


@mock_group.command(name="rvabrep")
@click.option(
    "--rows",
    type=click.IntRange(min=1),
    default=50000,
    show_default=True,
    help="Number of RVABREP rows to generate.",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, path_type=Path),
    required=True,
    help="Destination CSV path. Parent directory is created if needed.",
)
@click.option(
    "--seed",
    type=int,
    default=None,
    help="PRNG seed. Defaults to --rows for easy reproducibility.",
)
@click.option(
    "--idrvi-source",
    "idrvi_source",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help=(
        "CSV with an IDRVI column (default: reference-data/csv/MapeoRVI_CM.csv). "
        "Used as the population for the index7 / IDRVI column."
    ),
)
@click.option(
    "--idrvi-top",
    "idrvi_top",
    type=click.IntRange(min=1),
    default=20,
    show_default=True,
    help="Take the top-N distinct IDRVIs from the source, sorted lexicographically.",
)
@click.option(
    "--image-mix",
    "image_mix_text",
    default="tiff:60,pdf:20,jpeg:20",
    show_default=True,
    help="Image-type proportions. Format: tiff:N,pdf:N,jpeg:N (weights, renormalized).",
)
@click.option(
    "--date-from",
    "date_from_text",
    default="2024-01-01",
    show_default=True,
    help="Earliest creation_date (ISO YYYY-MM-DD).",
)
@click.option(
    "--date-to",
    "date_to_text",
    default="2025-12-31",
    show_default=True,
    help="Latest creation_date (ISO YYYY-MM-DD).",
)
@click.option(
    "--clients",
    type=click.IntRange(min=1),
    default=5000,
    show_default=True,
    help="Cardinality of the shortname pool. Average docs/client = rows/clients.",
)
@click.option(
    "--delete-rate",
    "delete_rate",
    type=click.FloatRange(min=0.0, max=1.0),
    default=0.05,
    show_default=True,
    help="Fraction of rows marked deleted (delete_code='D').",
)
@click.option(
    "--cif-rate",
    "cif_rate",
    type=click.FloatRange(min=0.0, max=1.0),
    default=0.95,
    show_default=True,
    help="Fraction of rows that carry a CIF (6-digit index2).",
)
def rvabrep_command(  # noqa: PLR0913 — las opciones de Click dictan la firma
    rows: int,
    output_path: Path,
    seed: int | None,
    idrvi_source: Path | None,
    idrvi_top: int,
    image_mix_text: str,
    date_from_text: str,
    date_to_text: str,
    clients: int,
    delete_rate: float,
    cif_rate: float,
) -> None:
    """Streamea un CSV RVABREP sintetico consumible por ``mock generate``."""
    try:
        image_mix = _parse_image_mix(image_mix_text)
        date_from = _parse_iso_date(date_from_text, flag="--date-from")
        date_to = _parse_iso_date(date_to_text, flag="--date-to")
        source = idrvi_source if idrvi_source is not None else _DEFAULT_IDRVI_SOURCE
        pool = _load_idrvi_pool(source, idrvi_top)
        spec = RvabrepGenSpec(
            rows=rows,
            seed=seed if seed is not None else rows,
            idrvi_pool=pool,
            image_mix=image_mix,
            date_from=date_from,
            date_to=date_to,
            clients=clients,
            delete_rate=delete_rate,
            cif_rate=cif_rate,
        )
    except ConfigurationError as exc:
        click.echo(f"ConfigurationError: {exc}", err=True)
        sys.exit(2)

    try:
        written = generate_rvabrep(spec, output_path)
    except ConfigurationError as exc:
        click.echo(f"ConfigurationError: {exc}", err=True)
        sys.exit(2)
    except Exception:  # noqa: BLE001 — handler de CLI de nivel superior
        _log.exception("mock rvabrep failed")
        sys.exit(3)
    click.echo(
        f"wrote {written} rows to {output_path} "
        f"(image_mix={image_mix_text}, idrvis={len(pool)}, seed={spec.seed})"
    )


def _parse_image_mix(text: str) -> ImageMix:
    parts = [p.strip() for p in text.split(",") if p.strip()]
    weights: dict[str, float] = {"tiff": 0.0, "pdf": 0.0, "jpeg": 0.0}
    seen: set[str] = set()
    for part in parts:
        if ":" not in part:
            raise ConfigurationError(
                "--image-mix entries must be kind:weight",
                entry=part,
            )
        kind, _, weight_text = part.partition(":")
        kind_norm = kind.strip().lower()
        if kind_norm not in weights:
            raise ConfigurationError(
                "--image-mix kind must be one of tiff / pdf / jpeg",
                kind=kind_norm,
            )
        if kind_norm in seen:
            raise ConfigurationError("--image-mix has duplicate kind", kind=kind_norm)
        seen.add(kind_norm)
        try:
            weights[kind_norm] = float(weight_text.strip())
        except ValueError as exc:
            raise ConfigurationError(
                "--image-mix weight must be a number",
                kind=kind_norm,
                weight=weight_text,
            ) from exc
    return ImageMix(tiff=weights["tiff"], pdf=weights["pdf"], jpeg=weights["jpeg"])


def _parse_iso_date(text: str, *, flag: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ConfigurationError(f"{flag} must be ISO YYYY-MM-DD", value=text) from exc


def _load_idrvi_pool(source_path: Path, top_n: int) -> tuple[str, ...]:
    """Lee la columna IDRVI de *source_path*, deduplica, ordena y toma top-N."""
    if not source_path.exists():
        raise ConfigurationError(
            "--idrvi-source path does not exist",
            path=str(source_path),
        )
    source = TabularDataSource(source_path)
    try:
        seen: set[str] = set()
        for row in source.get_all():
            raw = row.get("IDRVI")
            if raw is None:
                continue
            cleaned = str(raw).strip()
            if cleaned:
                seen.add(cleaned)
    finally:
        source.close()
    if not seen:
        raise ConfigurationError(
            "--idrvi-source has no usable IDRVI values",
            path=str(source_path),
        )
    ordered = tuple(sorted(seen))
    if top_n > len(ordered):
        raise ConfigurationError(
            "--idrvi-top exceeds the distinct IDRVI count in the source",
            top_n=top_n,
            distinct=len(ordered),
        )
    return ordered[:top_n]
