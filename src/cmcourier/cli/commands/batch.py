"""Subcomandos de ``cmcourier batch ...``.

* ``batch list``: enumera los batches con estado + contadores.
* ``batch show <id>``: contadores por etapa, el censo del origen (148) y
  records fallados.
* ``batch retry-failed --batch <id> [--stage Sn]``: resetea las
  fallas. 150: nunca toca el balde ``EXCLUIDO`` — el discriminador es el
  balde, no el ``status``.
* ``batch export-report --batch <id> --format csv|json [--output <path>]``:
  vuelca el estado completo del batch —censo incluido— para analisis
  offline.

148 REQ-006: ``show`` y ``export-report`` comparten la misma proyeccion
del censo (``_census_summary`` / ``_census_rows``), asi que la pantalla y
el archivo no pueden discrepar.

Todos los comandos abren el tracking store via la capa de wiring
(asi las cuestiones especificas de SQLite quedan detras de
``ITrackingStore``).
"""

from __future__ import annotations

__all__ = ["batch_group"]

import csv
import io
import json
import logging
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, NamedTuple

import click

from cmcourier.adapters.tracking import SQLiteTrackingStore
from cmcourier.cli.commands._formatting import render_table, truncate
from cmcourier.config.loader import load_config
from cmcourier.domain.exceptions import ConfigurationError
from cmcourier.domain.models import BatchDetails, ReasonCount, StageStatus
from cmcourier.observability.setup import configure as configure_observability

_log = logging.getLogger(__name__)

_STAGES_FOR_RETRY = ("S1", "S2", "S3", "S4", "S5")
_STAGES_FOR_TABLE = ("S0", "S1", "S2", "S3", "S4", "S5")
# Las tres salidas históricas, usadas sólo como fallback cuando el `batch`
# no tiene ninguna fila: desde 148 el juego de salidas lo dicta el pivot.
_FALLBACK_OUTCOMES = ("DONE", "FAILED", "PENDING")
# 148 REQ-006: el orden del censo es SEMÁNTICO, no alfabético — va de
# "nadie tiene que hacer nada" a "andá a arreglarlo" a "investigá".
_BUCKET_ORDER = ("EXCLUIDO", "BLOQUEADO", "FALLO")
# Etiqueta de un `reason_code` persistido que ya no está en la taxonomía
# (fila legacy o editada a mano). Se muestra igual: un documento que el
# censo no sabe clasificar tiene que verse, no desaparecer.
_UNKNOWN_BUCKET = "(sin balde)"


@click.group(name="batch")
def batch_group() -> None:
    """Comandos del ciclo de vida de los batches."""


# ---------------------------------------------------------------------------
# batch list
# ---------------------------------------------------------------------------


@batch_group.command(name="list")
@click.option(
    "--config",
    "-c",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Path to the pipeline YAML config file.",
)
@click.option(
    "--status",
    type=click.Choice(["in_progress", "completed"]),
    default=None,
    help="Filter by batch lifecycle state.",
)
def batch_list_command(config_path: Path, status: str | None) -> None:
    """Enumera los batches con estado + contadores (los mas nuevos primero)."""
    config = _load(config_path)
    configure_observability(config.observability, "INFO")
    store = SQLiteTrackingStore(config.tracking.db_path)
    try:
        batches = store.list_batches(status=status)  # type: ignore[arg-type]
    finally:
        store.close()
    if not batches:
        click.echo("No batches recorded.")
        return
    rows = [
        [
            b.batch_id,
            b.status,
            b.started_at.isoformat(timespec="seconds"),
            b.completed_at.isoformat(timespec="seconds") if b.completed_at else "-",
            str(b.total_records),
        ]
        for b in batches
    ]
    click.echo(render_table(["BATCH_ID", "STATUS", "STARTED", "COMPLETED", "TOTAL"], rows))


# ---------------------------------------------------------------------------
# batch show
# ---------------------------------------------------------------------------


@batch_group.command(name="show")
@click.option(
    "--config",
    "-c",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.argument("batch_id", type=str)
def batch_show_command(config_path: Path, batch_id: str) -> None:
    """Estado detallado por etapa, censo del origen y records fallados."""
    config = _load(config_path)
    configure_observability(config.observability, "INFO")
    store = SQLiteTrackingStore(config.tracking.db_path)
    try:
        details = store.get_batch_details(batch_id)
        # 150 REQ-005: "¿activo según qué lista?" — la respuesta vive en las
        # columnas de auditoría del batch, no en el censo.
        audit = store.batch_audit(batch_id)
    finally:
        store.close()
    if details is None:
        click.echo(f"Batch not found: {batch_id}", err=True)
        sys.exit(1)
    info = details.info
    click.echo(f"Batch: {info.batch_id}")
    click.echo(f"Status: {info.status}")
    click.echo(f"Started: {info.started_at.isoformat(timespec='seconds')}")
    completed_str = (
        info.completed_at.isoformat(timespec="seconds") if info.completed_at is not None else "-"
    )
    click.echo(f"Completed: {completed_str}")
    # 148 REQ-005/006: el denominador es el conteo REAL de documentos del
    # origen, y los migrados son el numerador contra el que tiene que sumar
    # todo lo demás.
    click.echo(f"Total en origen: {info.total_records}")
    click.echo(f"Migrados: {_census_summary(details).migrated}")
    click.echo("")
    _echo_stage_table(details)
    _echo_census(details)
    _echo_eligibility(audit)
    _echo_failures(details)


def _echo_eligibility(audit: Mapping[str, str]) -> None:
    """150 REQ-005: qué lista de clientes activos decidió las exclusiones.

    El CSV de activos es una foto de un momento: sin la ruta, la fecha y el
    conteo de filas, un ``CLIENT_NOT_ACTIVE`` leído dentro de seis meses no
    se puede auditar. Sin corrida con elegibilidad no se imprime nada — una
    config pre-150 no gana ninguna línea.
    """
    path = audit.get("eligibility_source_path")
    if not path:
        return
    click.echo("")
    click.echo("Lista de activos (150): " + path)
    click.echo(
        f"  modificada: {audit.get('eligibility_modified_at') or '-'} · "
        f"filas: {audit.get('eligibility_rows') or '-'}"
    )


def _echo_stage_table(details: BatchDetails) -> None:
    """Pivot ``S0..S5`` × salidas. 148: las columnas salen de los datos.

    Hasta 148 se hardcodeaban ``DONE / FAILED / PENDING`` y ``S1_FILTERED``
    / ``S1_SKIPPED`` se caían de la tabla, con lo cual las columnas no
    sumaban el total y nadie avisaba.
    """
    outcomes = _stage_outcomes(details.stage_counts)
    stage_rows = [
        [stage, *(str(details.stage_counts.get(stage, {}).get(o, 0)) for o in outcomes)]
        for stage in _STAGES_FOR_TABLE
    ]
    click.echo(render_table(["STAGE", *outcomes], stage_rows))


def _echo_census(details: BatchDetails) -> None:
    """148 REQ-006: balde → razón → ``id_rvi``, y el cuadre al final."""
    rows = _census_rows(details.reason_counts)
    click.echo("")
    click.echo("CENSO — por qué no se subió cada documento")
    if not rows:
        click.echo("Sin razones registradas: ningún documento de este batch dejó razón.")
    else:
        totals = _bucket_totals(details.reason_counts)
        click.echo("Baldes: " + " · ".join(f"{b} {n}" for b, n in totals.items()))
        click.echo("")
        click.echo(
            render_table(
                ["BALDE", "RAZON", "ID_RVI", "DOCS"],
                [
                    [r.bucket or _UNKNOWN_BUCKET, r.reason_code, r.id_rvi or "-", str(r.count)]
                    for r in rows
                ],
            )
        )
    click.echo("")
    for line in _reconciliation_lines(_census_summary(details), has_reasons=bool(rows)):
        click.echo(line)


def _echo_failures(details: BatchDetails) -> None:
    """Los ``*_FAILED`` con su razón (148) y el mensaje de error."""
    if not details.failed_records:
        return
    click.echo("")
    click.echo("FAILED records:")
    failure_rows = [
        [f.txn_num, f.status, f.reason_code or "-", truncate(f.error_message, 80)]
        for f in details.failed_records
    ]
    click.echo(render_table(["TXN_NUM", "STAGE", "RAZON", "ERROR"], failure_rows))


# ---------------------------------------------------------------------------
# batch retry-failed
# ---------------------------------------------------------------------------


@batch_group.command(name="retry-failed")
@click.option(
    "--config",
    "-c",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--batch",
    "batch_id",
    type=str,
    required=True,
    help="Batch ID to scan for failed records.",
)
@click.option(
    "--stage",
    type=click.Choice(_STAGES_FOR_RETRY),
    default=None,
    help="If given, only reset failures in this stage.",
)
def batch_retry_failed_command(config_path: Path, batch_id: str, stage: str | None) -> None:
    """Resetea las filas ``*_FAILED`` a ``*_PENDING`` para reintento.

    150: las filas del balde ``EXCLUIDO`` quedan intactas — reintentar una
    decisión de negocio (``CLIENT_NOT_ACTIVE``) no la cambia de opinión.
    """
    config = _load(config_path)
    configure_observability(config.observability, "INFO")
    stage_status = StageStatus(f"{stage}_FAILED") if stage is not None else None
    store = SQLiteTrackingStore(config.tracking.db_path)
    try:
        reset = store.retry_failed(batch_id, stage=stage_status)
    finally:
        store.close()
    click.echo(f"Reset {reset} FAILED rows to PENDING (batch={batch_id}, stage={stage or 'all'})")


# ---------------------------------------------------------------------------
# Helper interno
# ---------------------------------------------------------------------------


def _load(config_path: Path):  # type: ignore[no-untyped-def]
    try:
        return load_config(config_path)
    except ConfigurationError as exc:
        click.echo(f"ConfigurationError: {exc}", err=True)
        sys.exit(2)


# ---------------------------------------------------------------------------
# 148 REQ-006 — el censo, compartido por `show` y `export-report`
# ---------------------------------------------------------------------------


class _CensusSummary(NamedTuple):
    """Los tres números que tienen que cerrar, y su diferencia.

    ``source_total`` es el conteo real del origen (REQ-005),
    ``migrated`` los ``S5_DONE`` y ``accounted`` los documentos que el
    censo sabe explicar. ``delta`` positivo = documentos que nadie
    explica; negativo = documentos contados de más.
    """

    source_total: int
    migrated: int
    accounted: int

    @property
    def delta(self) -> int:
        return self.source_total - (self.migrated + self.accounted)

    @property
    def reconciles(self) -> bool:
        return self.delta == 0


def _census_summary(details: BatchDetails) -> _CensusSummary:
    return _CensusSummary(
        source_total=details.info.total_records,
        migrated=details.stage_counts.get("S5", {}).get("DONE", 0),
        accounted=sum(rc.count for rc in details.reason_counts),
    )


def _census_rows(counts: Iterable[ReasonCount]) -> list[ReasonCount]:
    """Ordena el censo: balde semántico → código por conteo → ``id_rvi``.

    Los baldes van ``EXCLUIDO``, ``BLOQUEADO``, ``FALLO`` porque ése es
    el orden en que el operador actúa. Dentro de cada balde, el código
    que más documentos se llevó va primero (es el que conviene atacar), y
    dentro de un código, el ``id_rvi`` más numeroso. Un balde vacío —
    ``reason_code`` fuera de la taxonomía vigente — va último, pero va.
    """
    rows = list(counts)
    totals: dict[tuple[str, str], int] = {}
    for rc in rows:
        key = (rc.bucket, rc.reason_code)
        totals[key] = totals.get(key, 0) + rc.count

    def sort_key(rc: ReasonCount) -> tuple[Any, ...]:
        rank = _BUCKET_ORDER.index(rc.bucket) if rc.bucket in _BUCKET_ORDER else len(_BUCKET_ORDER)
        return (
            rank,
            rc.bucket,
            -totals[(rc.bucket, rc.reason_code)],
            rc.reason_code,
            -rc.count,
            rc.id_rvi,
        )

    return sorted(rows, key=sort_key)


def _bucket_totals(counts: Iterable[ReasonCount]) -> dict[str, int]:
    """Subtotal por balde, en el orden semántico de :func:`_census_rows`."""
    out: dict[str, int] = {}
    for rc in _census_rows(counts):
        label = rc.bucket or _UNKNOWN_BUCKET
        out[label] = out.get(label, 0) + rc.count
    return out


def _stage_outcomes(stage_counts: Mapping[str, Mapping[str, int]]) -> list[str]:
    """Las columnas de la tabla de etapas, tal como vienen del pivot.

    El pivot es rectangular (misma clave en toda etapa), así que recorrer
    las etapas en orden preserva el orden de las salidas: las tres
    históricas primero y las nuevas detrás.
    """
    outcomes: list[str] = []
    for stage in _STAGES_FOR_TABLE:
        for outcome in stage_counts.get(stage, {}):
            if outcome not in outcomes:
                outcomes.append(outcome)
    return outcomes or list(_FALLBACK_OUTCOMES)


def _docs(n: int) -> str:
    """``1 documento`` / ``N documentos`` — el descuadre de un solo doc existe."""
    return "1 documento" if n == 1 else f"{n} documentos"


def _reconciliation_lines(summary: _CensusSummary, *, has_reasons: bool) -> list[str]:
    """El cuadre, o el DESCUADRE con los dos números y la diferencia.

    148 REQ-006: un reporte que no cuadra y no avisa es peor que no tener
    reporte. La línea nombra AMBOS números y el delta — "no cuadra" a
    secas no le sirve a nadie.
    """
    suma = summary.migrated + summary.accounted
    partes = f"migrados {summary.migrated} + censados {summary.accounted} = {suma}"
    if summary.reconciles:
        return [f"Cuadre OK: {partes}, igual al total en origen ({summary.source_total})."]
    if summary.delta > 0:
        detalle = f"{_docs(summary.delta)} sin explicar"
    else:
        detalle = f"{_docs(-summary.delta)} contados de más"
    lines = [
        f"!! DESCUADRE: {partes}, pero el total en origen es {summary.source_total} — {detalle}."
    ]
    if not has_reasons:
        lines.append(
            "   El batch no registró NINGUNA razón: probablemente es anterior a 148, "
            "cuando el total en origen era el batch_size configurado y no el conteo real."
        )
    return lines


# ---------------------------------------------------------------------------
# batch export-report (023)
# ---------------------------------------------------------------------------


@batch_group.command(name="export-report")
@click.option(
    "--config",
    "-c",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option("--batch", "batch_id", type=str, required=True)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["csv", "json"]),
    required=True,
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write the report to a file (default: stdout).",
)
def batch_export_report_command(
    config_path: Path,
    batch_id: str,
    output_format: str,
    output_path: Path | None,
) -> None:
    """Vuelca el estado completo del batch a CSV o JSON para analisis offline."""
    config = _load(config_path)
    configure_observability(config.observability, "INFO")
    store = SQLiteTrackingStore(config.tracking.db_path)
    try:
        details = store.get_batch_details(batch_id)
    finally:
        store.close()
    if details is None:
        click.echo(f"Batch not found: {batch_id}", err=True)
        sys.exit(1)

    body = _render_csv(details) if output_format == "csv" else _render_json(details)

    if output_path is None:
        click.echo(body, nl=False)
        return
    try:
        output_path.write_text(body, encoding="utf-8")
    except OSError as exc:
        click.echo(f"ConfigurationError: cannot write {output_path}: {exc}", err=True)
        sys.exit(2)
    click.echo(f"Report written to {output_path}")


def _render_csv(details: BatchDetails) -> str:
    """Tres bloques rectangulares separados por una línea en blanco.

    El primero es el de siempre (etapas), byte-compatible con lo que ya
    parsea cualquiera salvo por las columnas de salidas nuevas que 148
    dejó de esconder. Los otros dos son el censo: el cuadre y el desglose.
    Cada bloque lleva su propio header, así que se abre en una planilla y
    se entiende sin leer la doc.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    _write_stage_block(writer, details)
    buf.write("\n")
    _write_reconciliation_block(writer, details)
    buf.write("\n")
    _write_census_block(writer, details)
    return buf.getvalue()


def _write_stage_block(writer: Any, details: BatchDetails) -> None:
    info = details.info
    outcomes = _stage_outcomes(details.stage_counts)
    writer.writerow(
        [
            "batch_id",
            "status",
            "started_at",
            "completed_at",
            "total_records",
            "stage",
            *(o.lower() for o in outcomes),
        ]
    )
    completed = info.completed_at.isoformat() if info.completed_at else ""
    for stage in _STAGES_FOR_TABLE:
        counts = details.stage_counts.get(stage, {})
        writer.writerow(
            [
                info.batch_id,
                info.status,
                info.started_at.isoformat(),
                completed,
                info.total_records,
                stage,
                *(counts.get(o, 0) for o in outcomes),
            ]
        )


def _write_reconciliation_block(writer: Any, details: BatchDetails) -> None:
    """El cuadre como pares ``metric,value``: los números que importan."""
    summary = _census_summary(details)
    writer.writerow(["metric", "value"])
    writer.writerow(["total_en_origen", summary.source_total])
    writer.writerow(["migrados", summary.migrated])
    writer.writerow(["censados", summary.accounted])
    writer.writerow(["sin_explicar", summary.delta])
    writer.writerow(["cuadra", "si" if summary.reconciles else "no"])


def _write_census_block(writer: Any, details: BatchDetails) -> None:
    writer.writerow(["bucket", "reason_code", "id_rvi", "count"])
    for rc in _census_rows(details.reason_counts):
        writer.writerow([rc.bucket or _UNKNOWN_BUCKET, rc.reason_code, rc.id_rvi, rc.count])


def _render_json(details: BatchDetails) -> str:
    info = details.info
    payload: dict[str, Any] = {
        "batch_id": info.batch_id,
        "status": info.status,
        "started_at": info.started_at.isoformat(),
        "completed_at": info.completed_at.isoformat() if info.completed_at else None,
        "total_records": info.total_records,
        "stage_counts": {stage: dict(counts) for stage, counts in details.stage_counts.items()},
        "census": _census_payload(details),
        "failed_records": [
            {
                "txn_num": f.txn_num,
                "status": f.status,
                "reason_code": f.reason_code,
                "error_message": f.error_message,
            }
            for f in details.failed_records
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def _census_payload(details: BatchDetails) -> dict[str, Any]:
    """148 REQ-006: el mismo censo que rinde ``batch show``, en JSON.

    ``unexplained`` va SIEMPRE, aunque sea 0: quien consuma el reporte
    tiene que poder chequear el cuadre sin recalcularlo.
    """
    summary = _census_summary(details)
    return {
        "source_total": summary.source_total,
        "migrated": summary.migrated,
        "accounted": summary.accounted,
        "unexplained": summary.delta,
        "reconciles": summary.reconciles,
        "by_bucket": _bucket_totals(details.reason_counts),
        "reasons": [
            {
                "bucket": rc.bucket,
                "reason_code": rc.reason_code,
                "id_rvi": rc.id_rvi,
                "count": rc.count,
            }
            for rc in _census_rows(details.reason_counts)
        ],
    }
