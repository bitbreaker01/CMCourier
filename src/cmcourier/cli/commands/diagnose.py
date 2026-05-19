"""092 — Subcomando ``cmcourier diagnose``.

Lee los logs JSONL emitidos por la `tier 2` de observabilidad
(``metrics-{date}.jsonl``) y reporta:

* Tabla por stage con count, p50, p95, max, total y % del wall.
* Identificación automática del cuello (stage con mayor % del total).
* Sugerencias contextuales según el patrón observado.

Modos de selección de batch:

* ``--batch <id>``: analiza un batch específico.
* ``--latest``: el batch_summary más reciente encontrado en el
  ``log_dir`` de la config.

Diseño:

* **No depende de SQLite** — usa solo los archivos JSONL del
  ``log_dir``. Si el operador perdió la DB pero conserva los logs,
  ``diagnose`` sigue funcionando.
* **Fail-loud por default**: si el ``log_dir`` no existe o no hay
  ``batch_summary`` que matchee, el comando sale con código != 0
  e imprime una explicación clara. Sin silenciar errores con
  ``ErrorAction SilentlyContinue`` como hacen los ad-hoc en
  PowerShell.
"""

from __future__ import annotations

__all__ = ["diagnose_command"]

import json
import sys
from pathlib import Path
from typing import Any

import click

from cmcourier.config.loader import load_config
from cmcourier.domain.exceptions import ConfigurationError

# Identificación de cuellos por umbrales aproximados — ver _diagnose_bottleneck.
_HIGH_LATENCY_S4_MS = 200.0
_HIGH_LATENCY_S5_MS = 500.0
_DOMINANT_STAGE_PCT = 50.0


# ---------------------------------------------------------------------------
# Modelos internos
# ---------------------------------------------------------------------------


class _BatchSummary:
    """Vista parseada de un evento ``batch_summary`` del JSONL."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.pipeline: str = str(payload.get("pipeline", ""))
        self.batch_id: str = str(payload.get("batch_id", ""))
        self.total_docs: int = int(payload.get("total_docs", 0))
        self.elapsed_s: float = float(payload.get("elapsed_s", 0.0))
        self.throughput: float = float(payload.get("throughput_docs_per_s", 0.0))
        self.stages: dict[str, dict[str, float]] = payload.get("stages") or {}
        self.timestamp: str = str(payload.get("asctime") or payload.get("timestamp") or "")


# ---------------------------------------------------------------------------
# Lectura de logs
# ---------------------------------------------------------------------------


def _find_metrics_files(log_dir: Path) -> list[Path]:
    """Devuelve ``metrics-*.jsonl`` ordenados por mtime descendente."""
    if not log_dir.exists():
        raise click.ClickException(
            f"log_dir does not exist: {log_dir.resolve()}. "
            "Check `observability.log_dir` in your YAML; default is './logs'."
        )
    if not log_dir.is_dir():
        raise click.ClickException(f"log_dir is not a directory: {log_dir.resolve()}")
    files = sorted(
        log_dir.glob("metrics-*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not files:
        raise click.ClickException(
            f"No metrics-*.jsonl files found in {log_dir.resolve()}. "
            "Run a batch first, or check `observability.pipeline_metrics: true` "
            "in your YAML."
        )
    return files


def _iter_batch_summaries(files: list[Path]) -> list[_BatchSummary]:
    """Itera todos los eventos ``batch_summary`` de los archivos dados."""
    out: list[_BatchSummary] = []
    for f in files:
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            click.echo(f"warning: could not read {f}: {exc}", err=True)
            continue
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("kind") != "batch_summary":
                continue
            out.append(_BatchSummary(payload))
    return out


def _select_summary(summaries: list[_BatchSummary], batch_id: str | None) -> _BatchSummary:
    if not summaries:
        raise click.ClickException(
            "No batch_summary events found in any metrics-*.jsonl. "
            "Has any batch completed since logs started?"
        )
    if batch_id:
        for s in summaries:
            if s.batch_id == batch_id:
                return s
        raise click.ClickException(
            f"batch_id {batch_id!r} not found in metrics logs. "
            f"Available: {', '.join(s.batch_id for s in summaries[:5])}…"
        )
    # --latest: el último por timestamp implícito en orden de archivos.
    return summaries[0]


# ---------------------------------------------------------------------------
# Análisis
# ---------------------------------------------------------------------------


def _stage_totals(summary: _BatchSummary) -> list[dict[str, Any]]:
    """Convierte ``summary.stages`` en filas listas para imprimir."""
    rows: list[dict[str, Any]] = []
    total_sum_ms = sum(s.get("sum_ms", 0.0) for s in summary.stages.values())
    for stage, bucket in sorted(summary.stages.items()):
        sum_ms = float(bucket.get("sum_ms", 0.0))
        count = int(bucket.get("count", 0))
        rows.append(
            {
                "stage": stage,
                "count": count,
                "p50_ms": float(bucket.get("p50_ms", 0.0)),
                "p95_ms": float(bucket.get("p95_ms", 0.0)),
                "p99_ms": float(bucket.get("p99_ms", 0.0)),
                "sum_ms": sum_ms,
                "pct": (sum_ms / total_sum_ms * 100.0) if total_sum_ms > 0 else 0.0,
                "avg_ms": (sum_ms / count) if count > 0 else 0.0,
            }
        )
    return rows


def _diagnose_bottleneck(rows: list[dict[str, Any]]) -> tuple[str | None, list[str]]:
    """Determina el stage cuello y emite sugerencias por patrón."""
    if not rows:
        return None, []
    sorted_rows = sorted(rows, key=lambda r: r["pct"], reverse=True)
    top = sorted_rows[0]
    suggestions: list[str] = []
    if top["pct"] < _DOMINANT_STAGE_PCT:
        # Sin un cuello claro — el trabajo se reparte de forma pareja.
        return None, [
            "No single stage dominates (each < 50%). "
            "Bottleneck is probably I/O latency or external services."
        ]
    bottleneck = str(top["stage"])
    if bottleneck == "S4":
        if top["avg_ms"] > _HIGH_LATENCY_S4_MS:
            suggestions.extend(
                [
                    "S4 (assembly) is dominant with abnormal latency.",
                    "Likely causes:",
                    "  1. Source disk slow (SMB share, HDD, or under AV scan)",
                    "  2. Process pool overhead exceeds work for small files",
                    "  3. img2pdf recompressing uncommon TIFF compression",
                    "Quick checks:",
                    "  - Get-PhysicalDisk | Select FriendlyName, MediaType",
                    "  - Get-MpPreference | Select-Object -ExpandProperty ExclusionPath",
                    "  - If mostly small PDFs: try processing.s4_use_processes: false",
                ]
            )
        else:
            suggestions.append("S4 dominant but latency reasonable — pipeline is healthy.")
    elif bottleneck == "S5":
        if top["avg_ms"] > _HIGH_LATENCY_S5_MS:
            suggestions.extend(
                [
                    "S5 (upload) is dominant — CMIS server-side processing per doc.",
                    "Likely causes:",
                    "  1. IBM CM server thread pool saturated",
                    "  2. Antivirus / indexing on the CM repository side",
                    "  3. Network latency between Windows and CMIS server",
                    "Quick checks:",
                    "  - Test single-file curl upload speed; if << 100 MB/s the link is the cap",
                    "  - Ask CM admin to monitor server CPU during a batch",
                    "  - Increase workers (current may be too few)",
                ]
            )
        else:
            suggestions.append(
                "S5 dominant but latency reasonable — typical for upload-heavy batches."
            )
    elif bottleneck == "S3":
        suggestions.extend(
            [
                "S3 (metadata resolve) is dominant.",
                "Likely causes:",
                "  1. metadata.cache.enabled: false → re-resolves every time",
                "  2. CSV / AS400 sources slow to query",
                "Quick checks:",
                "  - Enable metadata.cache.enabled: true with ttl_minutes >= 60",
                "  - If using AS400 sources: pre-filter the query",
            ]
        )
    elif bottleneck == "S2":
        suggestions.extend(
            [
                "S2 (mapping) is dominant — very unusual.",
                "Likely causes: the mapping CSV is huge or the lookup is broken.",
            ]
        )
    elif bottleneck == "S1":
        suggestions.extend(
            [
                "S1 (indexing) is dominant.",
                "Likely causes:",
                "  1. RVABREP query is slow (no index on key columns)",
                "  2. AS400 connection latency",
            ]
        )
    return bottleneck, suggestions


# ---------------------------------------------------------------------------
# Renderizado
# ---------------------------------------------------------------------------


def _render(summary: _BatchSummary, rows: list[dict[str, Any]]) -> str:
    bottleneck, suggestions = _diagnose_bottleneck(rows)
    lines: list[str] = []
    lines.append("═" * 72)
    lines.append(f"  Batch {summary.batch_id[:12]}… — diagnose report")
    lines.append("═" * 72)
    lines.append(f"  Pipeline:           {summary.pipeline}")
    lines.append(f"  Total docs:         {summary.total_docs}")
    lines.append(f"  Wall time:          {summary.elapsed_s:.1f} s")
    lines.append(f"  Throughput:         {summary.throughput:.2f} docs/s")
    lines.append("")
    lines.append("  Latencia por stage:")
    lines.append("")
    lines.append(
        f"    {'Stage':<6} {'Count':>6} {'Avg ms':>8} {'P50 ms':>8} "
        f"{'P95 ms':>8} {'Sum s':>8} {'%':>6}"
    )
    lines.append("    " + "─" * 60)
    for r in rows:
        marker = " ←" if str(r["stage"]) == bottleneck else ""
        lines.append(
            f"    {r['stage']:<6} {r['count']:>6} "
            f"{r['avg_ms']:>8.1f} {r['p50_ms']:>8.1f} {r['p95_ms']:>8.1f} "
            f"{r['sum_ms'] / 1000.0:>8.1f} {r['pct']:>5.1f}%{marker}"
        )
    lines.append("    " + "─" * 60)
    lines.append("")
    if bottleneck:
        lines.append(f"  ▶ Bottleneck detectado: {bottleneck}")
    else:
        lines.append("  ▶ Sin cuello claro (ninguna etapa > 50% del total)")
    lines.append("")
    if suggestions:
        lines.append("  ▶ Sugerencias:")
        for s in suggestions:
            lines.append(f"      {s}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.command(name="diagnose")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Path to the pipeline YAML (used to locate observability.log_dir).",
)
@click.option(
    "--batch",
    "batch_id",
    type=str,
    default=None,
    help="Specific batch_id to analyze. Mutually exclusive with --latest.",
)
@click.option(
    "--latest",
    "use_latest",
    is_flag=True,
    default=False,
    help="Analyze the most recent batch_summary in the metrics logs.",
)
@click.option(
    "--list",
    "list_batches",
    is_flag=True,
    default=False,
    help="List available batch_summary events and exit.",
)
def diagnose_command(
    config_path: Path,
    batch_id: str | None,
    use_latest: bool,
    list_batches: bool,
) -> None:
    """Analiza los logs JSONL de un batch y reporta el cuello de botella.

    Lee ``observability.log_dir/metrics-*.jsonl`` y emite una tabla
    por stage con sugerencias automáticas. No depende de SQLite —
    el diagnóstico funciona aunque la tracking DB se haya perdido.
    """
    if batch_id and use_latest:
        raise click.UsageError("--batch and --latest are mutually exclusive")
    if not batch_id and not use_latest and not list_batches:
        raise click.UsageError("must specify one of --batch <id>, --latest, or --list")

    try:
        config = load_config(config_path)
    except ConfigurationError as exc:
        click.echo(f"ConfigurationError: {exc}", err=True)
        sys.exit(2)

    log_dir = config.observability.log_dir
    files = _find_metrics_files(log_dir)
    summaries = _iter_batch_summaries(files)

    if list_batches:
        click.echo(f"Found {len(summaries)} batch_summary events in {log_dir.resolve()}:")
        for s in summaries[:30]:
            click.echo(
                f"  {s.batch_id}  pipeline={s.pipeline}  "
                f"docs={s.total_docs}  elapsed={s.elapsed_s:.1f}s"
            )
        if len(summaries) > 30:
            click.echo(f"  … and {len(summaries) - 30} more")
        return

    summary = _select_summary(summaries, batch_id)
    rows = _stage_totals(summary)
    click.echo(_render(summary, rows))
