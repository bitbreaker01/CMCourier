"""Entry point del CLI de CMCourier.

Grupo raiz Click ``cmcourier`` con tres sub-grupos de pipeline
(``csv-trigger-pipeline``, ``rvabrep-pipeline``, ``local-scan-pipeline``)
mas el comando de pre-flight de nivel superior ``doctor``. Cada comando
de pipeline espera una config cuyo ``trigger.kind`` haga match; las
diferencias salen con codigo 2.

Codigos de salida segun spec REQ-020:
    0 = exito (todo doc llego a `S5_DONE`)
    1 = el pipeline corrio pero hubo fallas de etapa (`s5_failed > 0`
        o alguna etapa upstream)
    2 = error de configuracion (YAML invalido, env vars faltantes,
        ``kind`` desalineado, etc.)
    3 = excepcion no manejada desde adentro de ``pipeline.run``
"""

from __future__ import annotations

__all__ = ["main"]

import logging
import sys
from pathlib import Path
from typing import Any, Literal

import click

from cmcourier import __version__
from cmcourier.cli.commands.analyze import analyze_group
from cmcourier.cli.commands.as400_query import as400_query_command
from cmcourier.cli.commands.background import background_command
from cmcourier.cli.commands.batch import batch_group
from cmcourier.cli.commands.cache import cache_group
from cmcourier.cli.commands.completion import completion_command
from cmcourier.cli.commands.diagnose import diagnose_command
from cmcourier.cli.commands.inspect import inspect_group
from cmcourier.cli.commands.mock import mock_group
from cmcourier.cli.commands.sync import sync_group
from cmcourier.cli.doctor import DoctorReport, run_doctor
from cmcourier.cli.logging_setup import configure as configure_logging
from cmcourier.config.loader import load_config, load_secrets
from cmcourier.config.schema import CsvTriggerConfig, PipelineConfig
from cmcourier.config.wiring import build_pipeline
from cmcourier.domain.exceptions import ConfigurationError
from cmcourier.observability.setup import configure as configure_observability
from cmcourier.orchestrators.multi_batch import MultiBatchOrchestrator, MultiBatchRunReport
from cmcourier.orchestrators.staged import RunReport, StagedPipeline
from cmcourier.orchestrators.streaming import StreamingOrchestrator
from cmcourier.services.deadline import DeadlineWatchdog
from cmcourier.services.duration import parse_duration
from cmcourier.services.triggers import SingleDocTriggerStrategy

_log = logging.getLogger(__name__)

_LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"]

_TriggerKind = Literal["csv", "rvabrep", "as400", "local_scan", "single_doc"]


# ---------------------------------------------------------------------------
# Grupo raiz + version
# ---------------------------------------------------------------------------


@click.group()
@click.version_option(__version__, prog_name="cmcourier")
def main() -> None:
    """CMCourier: herramienta de migracion de RVI a IBM Content Manager."""


main.add_command(batch_group)
main.add_command(inspect_group)
main.add_command(as400_query_command)
main.add_command(background_command)
main.add_command(analyze_group)
main.add_command(completion_command)
main.add_command(sync_group)
main.add_command(mock_group)
main.add_command(cache_group)
main.add_command(diagnose_command)


# ---------------------------------------------------------------------------
# csv-trigger-pipeline
# ---------------------------------------------------------------------------


@main.group(name="csv-trigger-pipeline")
def csv_trigger_pipeline_group() -> None:
    """Subcomandos de `csv-trigger-pipeline`."""


@csv_trigger_pipeline_group.command(name="run")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Path to the pipeline YAML config file.",
)
@click.option("--batch-id", type=str, default=None)
@click.option("--from-stage", type=click.IntRange(1, 5), default=1)
@click.option("--batch-size", type=click.IntRange(min=1), default=None)
@click.option(
    "--triggers",
    "triggers_override",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Override the config's trigger CSV path (csv kind only).",
)
@click.option(
    "--skip-doctor",
    is_flag=True,
    default=False,
    help="Bypass the automatic doctor pre-flight (dev iteration).",
)
@click.option(
    "--resume",
    is_flag=True,
    default=False,
    help="Auto-detect from-stage by reading batch state. Requires --batch-id.",
)
@click.option(
    "--tui/--no-tui",
    "tui",
    default=True,
    help="Start the live two-tab TUI. Default ON; --no-tui for headless shells.",
)
@click.option(
    "--batches-in-flight",
    "batches_in_flight",
    type=click.IntRange(1, 2),
    default=None,
    help="Override processing.batches_in_flight (1 or 2). Default reads YAML.",
)
@click.option(
    "--total",
    "total",
    type=click.IntRange(min=1),
    default=None,
    help="Process at most N triggers from the source (for validation runs).",
)
@click.option(
    "--max-duration",
    "max_duration",
    type=str,
    default=None,
    help="Stop the run after this wall-clock duration, e.g. 30m, 2h, 1h30m.",
)
@click.option("--log-level", type=click.Choice(_LOG_LEVELS, case_sensitive=False), default="INFO")
def csv_run_command(
    config_path: Path,
    batch_id: str | None,
    from_stage: int,
    batch_size: int | None,
    triggers_override: Path | None,
    skip_doctor: bool,
    resume: bool,
    tui: bool,
    batches_in_flight: int | None,
    total: int | None,
    max_duration: str | None,
    log_level: str,
) -> None:
    """Corre el `csv-trigger pipeline` de punta a punta."""
    _run_pipeline_command(
        config_path,
        expected_kind="csv",
        batch_id=batch_id,
        from_stage=from_stage,
        batch_size=batch_size,
        triggers_override=triggers_override,
        skip_doctor=skip_doctor,
        resume=resume,
        log_level=log_level,
        tui=tui,
        batches_in_flight=batches_in_flight,
        total=total,
        max_duration=max_duration,
    )


# ---------------------------------------------------------------------------
# rvabrep-pipeline
# ---------------------------------------------------------------------------


@main.group(name="rvabrep-pipeline")
def rvabrep_pipeline_group() -> None:
    """Subcomandos de `rvabrep-pipeline`."""


@rvabrep_pipeline_group.command(name="run")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option("--batch-id", type=str, default=None)
@click.option("--from-stage", type=click.IntRange(1, 5), default=1)
@click.option("--batch-size", type=click.IntRange(min=1), default=None)
@click.option("--skip-doctor", is_flag=True, default=False)
@click.option("--resume", is_flag=True, default=False)
@click.option("--tui/--no-tui", "tui", default=True)
@click.option(
    "--batches-in-flight",
    "batches_in_flight",
    type=click.IntRange(1, 2),
    default=None,
)
@click.option(
    "--total",
    "total",
    type=click.IntRange(min=1),
    default=None,
    help="Process at most N triggers from the source (for validation runs).",
)
@click.option(
    "--max-duration",
    "max_duration",
    type=str,
    default=None,
    help="Stop the run after this wall-clock duration, e.g. 30m, 2h, 1h30m.",
)
@click.option("--log-level", type=click.Choice(_LOG_LEVELS, case_sensitive=False), default="INFO")
def rvabrep_run_command(
    config_path: Path,
    batch_id: str | None,
    from_stage: int,
    batch_size: int | None,
    skip_doctor: bool,
    resume: bool,
    tui: bool,
    batches_in_flight: int | None,
    total: int | None,
    max_duration: str | None,
    log_level: str,
) -> None:
    """Corre el `rvabrep-pipeline` de punta a punta."""
    _run_pipeline_command(
        config_path,
        expected_kind="rvabrep",
        batch_id=batch_id,
        from_stage=from_stage,
        batch_size=batch_size,
        triggers_override=None,
        skip_doctor=skip_doctor,
        resume=resume,
        log_level=log_level,
        tui=tui,
        batches_in_flight=batches_in_flight,
        total=total,
        max_duration=max_duration,
    )


# ---------------------------------------------------------------------------
# local-scan-pipeline
# ---------------------------------------------------------------------------


@main.group(name="local-scan-pipeline")
def local_scan_pipeline_group() -> None:
    """Subcomandos de `local-scan-pipeline`."""


@local_scan_pipeline_group.command(name="run")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option("--batch-id", type=str, default=None)
@click.option("--from-stage", type=click.IntRange(1, 5), default=1)
@click.option("--batch-size", type=click.IntRange(min=1), default=None)
@click.option("--skip-doctor", is_flag=True, default=False)
@click.option("--resume", is_flag=True, default=False)
@click.option("--tui/--no-tui", "tui", default=True)
@click.option(
    "--batches-in-flight",
    "batches_in_flight",
    type=click.IntRange(1, 2),
    default=None,
)
@click.option(
    "--total",
    "total",
    type=click.IntRange(min=1),
    default=None,
    help="Process at most N triggers from the source (for validation runs).",
)
@click.option(
    "--max-duration",
    "max_duration",
    type=str,
    default=None,
    help="Stop the run after this wall-clock duration, e.g. 30m, 2h, 1h30m.",
)
@click.option("--log-level", type=click.Choice(_LOG_LEVELS, case_sensitive=False), default="INFO")
def local_scan_run_command(
    config_path: Path,
    batch_id: str | None,
    from_stage: int,
    batch_size: int | None,
    skip_doctor: bool,
    resume: bool,
    tui: bool,
    batches_in_flight: int | None,
    total: int | None,
    max_duration: str | None,
    log_level: str,
) -> None:
    """Corre el `local-scan-pipeline` de punta a punta."""
    _run_pipeline_command(
        config_path,
        expected_kind="local_scan",
        batch_id=batch_id,
        from_stage=from_stage,
        batch_size=batch_size,
        triggers_override=None,
        skip_doctor=skip_doctor,
        resume=resume,
        log_level=log_level,
        tui=tui,
        batches_in_flight=batches_in_flight,
        total=total,
        max_duration=max_duration,
    )


# ---------------------------------------------------------------------------
# single-doc (pipeline diagnostico)
# ---------------------------------------------------------------------------


@main.group(name="single-doc")
def single_doc_group() -> None:
    """Subcomandos de `single-doc` (debug / ad-hoc)."""


@single_doc_group.command(name="run")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Path to the pipeline YAML config file (trigger.kind must be 'single_doc').",
)
@click.option("--shortname", type=str, required=True, help="Target document shortname.")
@click.option("--system", type=str, required=True, help="Source system identifier.")
@click.option("--cif", type=str, default=None, help="Optional CIF (resolved if blank).")
@click.option("--batch-id", type=str, default=None)
@click.option("--from-stage", type=click.IntRange(1, 5), default=1)
@click.option("--batch-size", type=click.IntRange(min=1), default=None)
@click.option("--skip-doctor", is_flag=True, default=False)
@click.option("--resume", is_flag=True, default=False)
@click.option("--tui/--no-tui", "tui", default=True)
@click.option(
    "--batches-in-flight",
    "batches_in_flight",
    type=click.IntRange(1, 2),
    default=None,
)
@click.option(
    "--total",
    "total",
    type=click.IntRange(min=1),
    default=None,
    help="Process at most N triggers from the source (for validation runs).",
)
@click.option(
    "--max-duration",
    "max_duration",
    type=str,
    default=None,
    help="Stop the run after this wall-clock duration, e.g. 30m, 2h, 1h30m.",
)
@click.option("--log-level", type=click.Choice(_LOG_LEVELS, case_sensitive=False), default="INFO")
def single_doc_run_command(
    config_path: Path,
    shortname: str,
    system: str,
    cif: str | None,
    batch_id: str | None,
    from_stage: int,
    batch_size: int | None,
    skip_doctor: bool,
    resume: bool,
    tui: bool,
    batches_in_flight: int | None,
    total: int | None,
    max_duration: str | None,
    log_level: str,
) -> None:
    """Corre un pipeline one-shot para un unico documento."""
    configure_logging(log_level)
    max_duration_s = _parse_max_duration(max_duration)
    try:
        config = load_config(config_path)
        secrets = load_secrets()
    except ConfigurationError as exc:
        click.echo(f"ConfigurationError: {exc}", err=True)
        sys.exit(2)

    actual_kind = getattr(config.trigger, "kind", "<unknown>")
    if actual_kind != "single_doc":
        click.echo(
            f"ConfigurationError: single-doc run expects trigger.kind='single_doc'; "
            f"config has kind={actual_kind!r}",
            err=True,
        )
        sys.exit(2)

    config = _apply_overrides(config, triggers_override=None, batch_size=batch_size)
    configure_observability(config.observability, log_level)

    if not skip_doctor:
        _run_auto_doctor(config, secrets)
    if resume:
        from_stage = _apply_resume(config, batch_id, from_stage)

    strategy = SingleDocTriggerStrategy(
        shortname=shortname,
        system_id=system,
        cif=cif or None,
    )
    try:
        pipeline = build_pipeline(
            config,
            secrets,
            trigger_strategy_override=strategy,
            pipeline_name="single-doc",
        )
    except ConfigurationError as exc:
        click.echo(f"ConfigurationError: {exc}", err=True)
        sys.exit(2)

    pipeline_kwargs = {
        "source_descriptor": "",
        "batch_size": config.batch_size,
        "batch_id": batch_id,
        "from_stage": from_stage,
        "batches_in_flight": batches_in_flight or config.processing.batches_in_flight,
        "resume": resume,
        "total": total,
    }
    report = _run_with_optional_tui(
        pipeline=pipeline,
        config=config,
        pipeline_kwargs=pipeline_kwargs,
        tui=tui,
        log_level=log_level,
        max_duration_s=max_duration_s,
    )
    _emit_outcome(
        report=report,
        expected_kind="single-doc",
        quiet=False,
    )


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


@main.command(name="doctor")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Path to the pipeline YAML config file.",
)
@click.option(
    "--check",
    "selected_check",
    type=click.Choice(["connections", "mapping", "metadata", "cm-types", "cm-targets", "all"]),
    default="all",
    help="Run only the named check group (default: all).",
)
@click.option("--log-level", type=click.Choice(_LOG_LEVELS, case_sensitive=False), default="INFO")
def doctor_command(config_path: Path, selected_check: str, log_level: str) -> None:
    """Corre la validacion pre-flight."""
    configure_logging(log_level)
    try:
        config = load_config(config_path)
        secrets = load_secrets()
    except ConfigurationError as exc:
        click.echo(f"ConfigurationError: {exc}", err=True)
        sys.exit(2)
    configure_observability(config.observability, log_level)
    try:
        report = run_doctor(config, secrets, selected=selected_check)
    except Exception:
        _log.exception("doctor crashed unexpectedly")
        sys.exit(3)
    if selected_check != "all":
        click.echo(f"Selected checks: {selected_check}")
    _emit_doctor_report(report)
    _log.info(
        "doctor_invoked",
        extra={"reason": selected_check},
    )
    sys.exit(1 if report.has_failures else 0)


# ---------------------------------------------------------------------------
# console (123)
# ---------------------------------------------------------------------------


@main.command(name="console")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Path to the pipeline YAML config file.",
)
@click.option(
    "--log-level", type=click.Choice(_LOG_LEVELS, case_sensitive=False), default="WARNING"
)
def console_command(config_path: Path, log_level: str) -> None:
    """Abre la consola de operación interactiva (credenciales, doctor,
    launcher, monitor y batches en una sola TUI)."""
    configure_logging(log_level)
    try:
        config = load_config(config_path)
    except ConfigurationError as exc:
        click.echo(f"ConfigurationError: {exc}", err=True)
        sys.exit(2)
    configure_observability(config.observability, log_level, tui_active=True)
    from cmcourier.cli._tui_runner import tty_available  # noqa: PLC0415

    if not tty_available():
        click.echo("ConfigurationError: console requiere una TTY", err=True)
        sys.exit(2)
    from cmcourier.cli.console.app import ConsoleApp  # noqa: PLC0415 — import pesado, lazy

    ConsoleApp(config=config, config_path=config_path).run()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
# (helpers internos del entry point)


def _run_pipeline_command(
    config_path: Path,
    *,
    expected_kind: _TriggerKind,
    batch_id: str | None,
    from_stage: int,
    batch_size: int | None,
    triggers_override: Path | None,
    skip_doctor: bool,
    resume: bool,
    log_level: str,
    quiet: bool = False,
    tui: bool = False,
    batches_in_flight: int | None = None,
    total: int | None = None,
    max_duration: str | None = None,
) -> None:
    configure_logging(log_level)
    max_duration_s = _parse_max_duration(max_duration)
    try:
        config = load_config(config_path)
        secrets = load_secrets()
    except ConfigurationError as exc:
        click.echo(f"ConfigurationError: {exc}", err=True)
        sys.exit(2)

    actual_kind = getattr(config.trigger, "kind", "<unknown>")
    if actual_kind != expected_kind:
        click.echo(
            f"ConfigurationError: this command expects trigger.kind={expected_kind!r}; "
            f"config has kind={actual_kind!r}",
            err=True,
        )
        sys.exit(2)

    config = _apply_overrides(config, triggers_override, batch_size)
    configure_observability(config.observability, log_level)

    if not skip_doctor:
        _run_auto_doctor(config, secrets)
    if resume:
        from_stage = _apply_resume(config, batch_id, from_stage, quiet=quiet)

    try:
        pipeline = build_pipeline(config, secrets, pipeline_name=f"{expected_kind}-trigger")
    except ConfigurationError as exc:
        click.echo(f"ConfigurationError: {exc}", err=True)
        sys.exit(2)

    source_descriptor = (
        str(config.trigger.csv_path)
        if isinstance(config.trigger, CsvTriggerConfig)
        else ""  # las strategies de rvabrep / as400 ignoran `source_descriptor`
    )
    pipeline_kwargs = {
        "source_descriptor": source_descriptor,
        "batch_size": config.batch_size,
        "batch_id": batch_id,
        "from_stage": from_stage,
        "batches_in_flight": batches_in_flight or config.processing.batches_in_flight,
        "resume": resume,
        "total": total,
    }
    report = _run_with_optional_tui(
        pipeline=pipeline,
        config=config,
        pipeline_kwargs=pipeline_kwargs,
        tui=tui,
        log_level=log_level,
        max_duration_s=max_duration_s,
    )
    _emit_outcome(
        report=report,
        expected_kind=expected_kind,
        quiet=quiet,
    )


def _parse_max_duration(text: str | None) -> float | None:
    """Parsea ``--max-duration`` a segundos; sale con código 2 si es inválido."""
    if text is None:
        return None
    try:
        return parse_duration(text)
    except ValueError as exc:
        click.echo(f"ConfigurationError: --max-duration: {exc}", err=True)
        sys.exit(2)


def _apply_overrides(
    config: PipelineConfig,
    triggers_override: Path | None,
    batch_size: int | None,
) -> PipelineConfig:
    updates: dict[str, object] = {}
    if triggers_override is not None and isinstance(config.trigger, CsvTriggerConfig):
        updates["trigger"] = CsvTriggerConfig(
            kind="csv",
            csv_path=triggers_override,
            shortname_column=config.trigger.shortname_column,
            cif_column=config.trigger.cif_column,
            system_id_column=config.trigger.system_id_column,
        )
    if batch_size is not None:
        updates["batch_size"] = batch_size
    if updates:
        return config.model_copy(update=updates)
    return config


def _run_with_optional_tui(
    *,
    pipeline: StagedPipeline,
    config: PipelineConfig,
    pipeline_kwargs: dict[str, Any],
    tui: bool,
    log_level: str,
    max_duration_s: float | None = None,
) -> MultiBatchRunReport:
    """Rutea la corrida por el multi-batch orchestrator (028 + 030).

    Los codigos de salida (2/3) coinciden con el contrato headless
    previo a 028. Cuando ``tui=True`` y stderr no es TTY, la TUI se
    auto-deshabilita (REQ-034). La TUI esta atada en vivo al recorder
    del chunk activo del orchestrator, asi las corridas con N=2
    renderizan datos coherentes por chunk mas una pestana CHUNKS que
    lista el estado de cada chunk.
    """
    from cmcourier.cli._tui_runner import (  # noqa: PLC0415
        run_orchestrator_with_tui,
        tty_available,
    )
    from cmcourier.tui import TUIDataProvider  # noqa: PLC0415

    if tui and not tty_available():
        ctx = click.get_current_context(silent=True)
        explicit = False
        if ctx is not None:
            source = ctx.get_parameter_source("tui")
            explicit = source is not None and source.name != "DEFAULT"
        if explicit:
            click.echo(
                "ConfigurationError: --tui requires a TTY; use --no-tui for headless runs",
                err=True,
            )
            sys.exit(2)
        tui = False

    # Extraemos los kwargs especificos del orchestrator y los sacamos del dict.
    batches_in_flight = int(pipeline_kwargs.pop("batches_in_flight", 1))
    resume_flag = bool(pipeline_kwargs.pop("resume", False))
    total = pipeline_kwargs.pop("total", None)
    if resume_flag:
        # El resume es inherentemente single-batch: el operador nombro un
        # `batch_id` especifico; el chunking del orchestrator no aplica.
        batches_in_flight = 1
    # 044: cualquier `--batch-id` provisto por el operador es el `batch_id`
    # sobre el que opera esta corrida (resume O fresh-named O replay con
    # `--from-stage`). El orchestrator rutea por ``_run_single`` siempre
    # que haya `batch_id` seteado y el pipeline valida la existencia.
    # Pre-044 esta asignacion solo honraba el `batch_id` cuando ademas se
    # pasaba `--resume`, lo que silenciaba el flag en los caminos de
    # replay con `--from-stage` y producia el
    # ``ValueError("from_stage > 1 requires batch_id")`` mas abajo.
    resume_batch_id = pipeline_kwargs.get("batch_id")
    if resume_batch_id is not None:
        # Los batches con nombre puesto por el operador van por el camino
        # single-batch para que el `batch_id` se honre tal cual (el overlap
        # multi-batch auto-genera ids por chunk y ignoraria el nombre del
        # usuario).
        batches_in_flight = 1

    # 063: selector de modo streaming. Ambos orchestrators exponen la
    # misma firma ``.run(...)`` y devuelven un `MultiBatchRunReport`, asi
    # que el resto de esta funcion (cableado de la TUI, `_emit_outcome`)
    # queda igual.
    orchestrator: MultiBatchOrchestrator | StreamingOrchestrator
    if config.processing.mode == "streaming":
        if int(pipeline_kwargs.get("from_stage", 1)) > 1 or resume_batch_id is not None:
            _log.warning(
                "streaming mode rejects resume args; the orchestrator will "
                "raise ValueError. Re-run with --from-stage 1 and no --batch-id."
            )
        orchestrator = StreamingOrchestrator(
            pipeline=pipeline,
            config=config,
            log_dir=config.observability.log_dir,
        )
    else:
        orchestrator = MultiBatchOrchestrator(
            pipeline=pipeline,
            config=config,
            log_dir=config.observability.log_dir,
        )
    orchestrator_kwargs: dict[str, Any] = {
        "source_descriptor": pipeline_kwargs["source_descriptor"],
        "batch_size": int(pipeline_kwargs["batch_size"]),
        "batches_in_flight": batches_in_flight,
        "from_stage": int(pipeline_kwargs.get("from_stage", 1)),
        "resume_batch_id": resume_batch_id,
        "total": total,
    }

    # 103: watchdog de deadline. Cuando ``--max-duration`` está seteado,
    # prende el `cancel_token` del orchestrator al vencer el plazo — el
    # mismo drain cooperativo de 097, disparado por reloj. Funciona con
    # TUI y headless.
    watchdog = (
        DeadlineWatchdog(orchestrator.cancel_token, max_duration_s)
        if max_duration_s is not None
        else None
    )

    if not tui:
        if watchdog is not None:
            watchdog.start()
        try:
            report = orchestrator.run(**orchestrator_kwargs)
        except Exception:
            _log.exception("pipeline run failed unexpectedly")
            sys.exit(3)
        finally:
            if watchdog is not None:
                watchdog.stop()
        if watchdog is not None and watchdog.fired:
            click.echo("Run detenido por --max-duration; el drain ordenado se completó.")
        return report

    # 041: una vez que estamos comprometidos a lanzar la TUI de Textual,
    # re-instalamos los handlers de observabilidad SIN un `StreamHandler`
    # de stderr para que el frame del dashboard no se rompa con lineas de
    # log. ``configure()`` es idempotente: resetea todos los handlers antes
    # de re-adjuntar, asi el `FileHandler` rotativo sigue logueando cada
    # evento a disco.
    configure_observability(config.observability, log_level, tui_active=True)

    # 064: el modo streaming cablea el snapshot del bucket en la pestana BUCKET.
    bucket_provider = (
        orchestrator.streaming_snapshot if isinstance(orchestrator, StreamingOrchestrator) else None
    )
    data_provider = TUIDataProvider(
        pipeline_name=pipeline.pipeline_name,
        metrics_recorder=pipeline.metrics_recorder,
        pool_stats=pipeline.pool_stats,
        concurrency_limit=pipeline.concurrency_limit,
        cmis_config=config.cmis,
        uploader=pipeline.uploader,
        auto_tune=pipeline.auto_tune_controller,
        # 030: live-binding del recorder del chunk activo + lista de
        # estado por chunk.
        recorder_provider=orchestrator.active_recorder,
        # 042: binding independiente de la pestana UPLOAD para que los
        # flips del lado PREP no pisoteen el display de percentil S5 / MB
        # a mitad del upload.
        upload_recorder_provider=orchestrator.upload_recorder,
        chunks_provider=orchestrator.chunks_snapshot,
        # 036: expone las stats de las heavy/light lane cuando el modo dual
        # esta habilitado.
        lane_controller=pipeline.lane_controller,
        # 052: tracking store para el drill-down por chunk de la pestana DETAIL.
        tracking_store=pipeline.tracking_store,
        # 064: modo de orquestacion + fuente de datos de la pestana BUCKET.
        mode=config.processing.mode,
        bucket_provider=bucket_provider,
    )
    if watchdog is not None:
        watchdog.start()
    try:
        outcome = run_orchestrator_with_tui(
            orchestrator=orchestrator,
            data_provider=data_provider,
            orchestrator_kwargs=orchestrator_kwargs,
        )
    finally:
        if watchdog is not None:
            watchdog.stop()
    if outcome.exception is not None:
        _log.exception(
            "pipeline run failed unexpectedly",
            exc_info=outcome.exception,
        )
        sys.exit(3)
    if watchdog is not None and watchdog.fired:
        click.echo("Run detenido por --max-duration; el drain ordenado se completó.")
    assert outcome.report is not None
    return outcome.report


def _emit_outcome(
    *,
    report: MultiBatchRunReport,
    expected_kind: str,
    quiet: bool,
) -> None:
    """Emite el resumen por chunk + totales y hace ``sys.exit`` con el codigo correcto.

    Para el camino legado de un solo chunk la salida es byte-a-byte
    identica al pre-028. Cuando corrio mas de un chunk, imprime una
    linea por chunk seguida de una linea TOTALS.
    """
    if not quiet:
        if len(report.chunks) <= 1:
            # Salida legada de single-batch preservada al pie de la letra.
            if report.chunks:
                _emit_summary(report.chunks[0])
        else:
            for idx, chunk in enumerate(report.chunks, start=1):
                click.echo(
                    f"chunk {idx}/{len(report.chunks)}  "
                    f"batch_id={chunk.batch_id} "
                    f"total_docs={chunk.total_docs} "
                    f"s1_filtered={chunk.s1_filtered} "
                    f"s5_done={chunk.s5_done} "
                    f"s5_failed={chunk.s5_failed} "
                    f"elapsed_seconds={chunk.elapsed_seconds:.2f}"
                )
            click.echo(
                f"TOTALS batch_count={len(report.chunks)} "
                f"total_docs={report.total_docs} "
                f"s1_filtered={report.s1_filtered} "
                f"s5_done={report.s5_done} "
                f"s5_failed={report.s5_failed} "
                f"failed_chunks={len(report.failed_chunks)} "
                f"elapsed_seconds={report.elapsed_seconds:.2f}"
            )
    elif report.s5_failed > 0 or report.failed_chunks:
        click.echo(
            f"pipeline={expected_kind}-trigger "
            f"batch_count={len(report.chunks)} "
            f"s5_failed={report.s5_failed} "
            f"failed_chunks={len(report.failed_chunks)} exit_code=1",
            err=True,
        )
    exit_code = 1 if (report.s5_failed > 0 or report.failed_chunks) else 0
    sys.exit(exit_code)


def _run_auto_doctor(config: PipelineConfig, secrets) -> None:  # type: ignore[no-untyped-def]
    """Corre los checks pre-flight; aborta al caller (`sys.exit`) ante un FAIL."""
    try:
        report = run_doctor(config, secrets)
    except Exception:
        _log.exception("auto-doctor crashed unexpectedly")
        sys.exit(3)
    _emit_doctor_report(report)
    if report.has_failures:
        _log.error(
            "doctor_fail",
            extra={"failed_count": report.failed_count},
        )
        sys.exit(2)
    _log.info(
        "doctor_pass",
        extra={
            "passed_count": report.passed_count,
            "warn_count": report.warn_count,
            "skip_count": report.skip_count,
        },
    )


def _apply_resume(
    config: PipelineConfig,
    batch_id: str | None,
    explicit_from_stage: int,
    *,
    quiet: bool = False,
) -> int:
    """Resuelve ``--resume`` a un `from_stage` concreto.

    Llama a ``sys.exit`` ante mal uso (sin `batch_id`, batch desconocido,
    batch limpio). Cuando ``explicit_from_stage`` no es el default, GANA
    y emite un WARNING: lo explicito le gana a lo inferido. ``quiet=True``
    suprime el echo a stdout de "Nothing to resume" (igual sale con 0); lo
    usa el runner en background.
    """
    from cmcourier.adapters.tracking import SQLiteTrackingStore  # noqa: PLC0415

    if batch_id is None:
        click.echo(
            "ConfigurationError: --resume requires --batch-id",
            err=True,
        )
        sys.exit(2)
    store = SQLiteTrackingStore(config.tracking.db_path)
    try:
        details = store.get_batch_details(batch_id)
    finally:
        store.close()
    if details is None:
        click.echo(f"Batch not found: {batch_id}", err=True)
        sys.exit(1)

    # 044: el `--from-stage` explicito gana sin importar la deteccion. El
    # operador nombro un punto de replay especifico y proveyo un `batch_id`,
    # honoramoslo incluso si la auto-deteccion diria "clean". Se movio
    # ANTES del clean-exit para que el usuario pueda forzar el replay de un
    # batch aparentemente completo (tipico despues de un cambio de config
    # que quiere re-correr desde S3 en adelante).
    if explicit_from_stage != 1:
        _log.info(
            "resume_explicit_from_stage",
            extra={
                "batch_id": batch_id,
                "explicit_from_stage": explicit_from_stage,
            },
        )
        return explicit_from_stage

    resolved: int | None = None
    for n in (1, 2, 3, 4, 5):
        counts = details.stage_counts.get(f"S{n}", {})
        # FAILED / PENDING en esta etapa tiene prioridad: el camino de
        # retry en la misma etapa es mas conservador que saltear hacia
        # adelante.
        if counts.get("FAILED", 0) + counts.get("PENDING", 0) > 0:
            resolved = n
            break
        # 044: los docs en ``S{N}_DONE`` con N<5 estan completos en la
        # etapa N pero nunca fueron levantados para la etapa N+1. Con un
        # pool de workers mas chico que el batch, la mayoria de los
        # escenarios de kill-mid-S5 dejan el grueso de los docs en
        # `S4_DONE`: pre-044 esto se veia "clean" para ``_apply_resume``
        # porque no estaba seteado ni el marcador FAILED ni el PENDING.
        # Detectamos el hueco y resumimos desde N+1.
        if n < 5 and counts.get("DONE", 0) > 0:
            resolved = n + 1
            break
    if resolved is None:
        if not quiet:
            click.echo(f"Nothing to resume — batch {batch_id} is clean")
        sys.exit(0)
    _log.info(
        "resume_resolved",
        extra={"batch_id": batch_id, "resume_inferred": resolved},
    )
    return resolved


def _emit_summary(report: RunReport) -> None:
    click.echo(
        f"batch_id={report.batch_id} "
        f"total_triggers={report.total_triggers} "
        f"total_docs={report.total_docs} "
        f"s1_filtered={report.s1_filtered} "
        f"s5_done={report.s5_done} "
        f"s5_failed={report.s5_failed} "
        f"elapsed_seconds={report.elapsed_seconds:.2f}"
    )


def _emit_doctor_report(report: DoctorReport) -> None:
    for result in report.results:
        click.echo(f"[{result.status.value}] {result.name} — {result.message}")
        for key in sorted(result.details.keys()):
            click.echo(f"    {key}={result.details[key]}")
    click.echo(
        f"{report.passed_count} passed, "
        f"{report.failed_count} failed, "
        f"{report.warn_count} warnings, "
        f"{report.skip_count} skipped "
        f"in {report.elapsed_seconds:.2f}s"
    )


if __name__ == "__main__":
    main()
