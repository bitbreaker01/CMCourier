"""Instala loggers, handlers, formatter y filter de `PII`.

Idempotente: cada llamada remueve los handlers existentes antes de
instalar los nuevos (los tests re-invocan entre casos sin que se filtre
estado).

Jerarquía de loggers:

* ``cmcourier`` — eventos de la app. stderr + rotating file handler.
* ``cmcourier.metrics.pipeline`` — resúmenes por batch (`JSONL`).
* ``cmcourier.metrics.network`` — timing por request (`JSONL`).
* ``cmcourier.metrics.slow_ops`` — `slow ops` por batch (lo maneja el
  archivo por batch de :class:`MetricsRecorder`; este logger existe
  por simetría del namespace).

Si ``stderr_only=True`` o ``config.enabled=False``, no se instalan file
handlers — solo el handler de stderr. El `early fail path` del doctor
usa esto cuando todavía no se parseó ningún config.
"""

from __future__ import annotations

__all__ = ["configure"]

import contextlib
import datetime as _dt
import logging
import sys
from logging.handlers import RotatingFileHandler

from cmcourier.config.schema import ObservabilityConfig
from cmcourier.observability.formatter import JsonFormatter
from cmcourier.observability.pii import PiiMaskingFilter

_TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

_METRICS_LOGGERS: tuple[str, ...] = (
    "cmcourier.metrics.pipeline",
    "cmcourier.metrics.network",
    "cmcourier.metrics.slow_ops",
    "cmcourier.metrics.reconcile",
)


def configure(
    config: ObservabilityConfig | None = None,
    log_level: str = "INFO",
    *,
    stderr_only: bool = False,
    tui_active: bool = False,
) -> None:
    """Instala loggers, handlers, formatter y filter de `PII`.

    Parameters
    ----------
    config:
        ObservabilityConfig del config de pipeline parseado. Si es
        ``None`` o ``stderr_only=True``, solo se instala el handler
        de stderr.
    log_level:
        Nivel para el handler de stderr. Los file handlers siempre
        loguean a INFO+ (DEBUG haría explotar la rotación).
    stderr_only:
        Fuerza el `legacy path` — sin file handlers. Lo usa el manejo
        de fallas de `early-load` del doctor.
    tui_active:
        Cuando es ``True`` (041), el StreamHandler de stderr NO se
        ataca a ``cmcourier`` para que el frame de Textual no quede
        pisoteado por llamadas a ``log.info(...)`` durante la corrida.
        Solo el rotating FileHandler recibe records. Se ignora cuando
        ``stderr_only=True`` (el `early-fail path` del doctor sigue
        necesitando stderr sí o sí).
    """
    _reset_all_handlers()

    level = getattr(logging, log_level.upper(), logging.INFO)
    pii_filter = PiiMaskingFilter()
    text_formatter = logging.Formatter(_TEXT_FORMAT)
    json_formatter = JsonFormatter()

    # Logger del paquete ``cmcourier``: handler de stderr salvo que la TUI
    # esté activa; file handler cuando está habilitado. La propagación
    # queda en el default (True) para que ``caplog`` de pytest (que se
    # engancha en el root logger) siga capturando records.
    root = logging.getLogger("cmcourier")
    root.setLevel(min(level, logging.INFO))

    # 041: saltea el handler de stderr cuando una TUI de Textual está
    # renderizando la terminal — si no, cada línea emitida rompería el
    # frame. ``stderr_only=True`` lo pisa porque ese path es el
    # diagnóstico de `early-fail` del doctor, que necesita imprimir EN
    # ALGÚN LADO sí o sí.
    if stderr_only or not tui_active:
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(text_formatter)
        stderr_handler.setLevel(level)
        stderr_handler.addFilter(pii_filter)
        root.addHandler(stderr_handler)

    if stderr_only or config is None or not config.enabled:
        # Los loggers de métricas existen pero sin handlers atacheados →
        # las emisiones son no-ops (Python cae al lastResort solo en el
        # root logger, al cual no tocamos).
        for name in _METRICS_LOGGERS:
            mlog = logging.getLogger(name)
            mlog.propagate = False
            mlog.setLevel(logging.CRITICAL + 1)  # silencioso
        return

    log_dir = config.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)

    date_stamp = _dt.date.today().isoformat()
    app_formatter = json_formatter if config.log_format == "json" else text_formatter
    rotation_bytes = int(config.rotation_mb) * 1024 * 1024

    # Handler del log de la app (`tier` 1).
    app_handler = RotatingFileHandler(
        log_dir / f"app-{date_stamp}.log",
        maxBytes=rotation_bytes,
        backupCount=5,
        encoding="utf-8",
    )
    app_handler.setFormatter(app_formatter)
    app_handler.setLevel(logging.INFO)
    app_handler.addFilter(pii_filter)
    root.addHandler(app_handler)

    # Handler de métricas de pipeline (`tier` 2).
    pipeline_log = logging.getLogger("cmcourier.metrics.pipeline")
    pipeline_log.propagate = False
    pipeline_log.setLevel(logging.INFO if config.pipeline_metrics else logging.CRITICAL + 1)
    if config.pipeline_metrics:
        pipeline_handler = RotatingFileHandler(
            log_dir / f"metrics-{date_stamp}.jsonl",
            maxBytes=rotation_bytes,
            backupCount=5,
            encoding="utf-8",
        )
        pipeline_handler.setFormatter(json_formatter)
        pipeline_handler.setLevel(logging.INFO)
        pipeline_handler.addFilter(pii_filter)
        pipeline_log.addHandler(pipeline_handler)

    # Handler de métricas de red (`tier` 3).
    network_log = logging.getLogger("cmcourier.metrics.network")
    network_log.propagate = False
    network_log.setLevel(logging.INFO if config.network_metrics else logging.CRITICAL + 1)
    if config.network_metrics:
        network_handler = RotatingFileHandler(
            log_dir / f"network-{date_stamp}.jsonl",
            maxBytes=rotation_bytes,
            backupCount=5,
            encoding="utf-8",
        )
        network_handler.setFormatter(json_formatter)
        network_handler.setLevel(logging.INFO)
        network_handler.addFilter(pii_filter)
        network_log.addHandler(network_handler)

    # 096: handler de reconciliación AS400. Siempre activo — el log de
    # conflictos es operacionalmente crítico (le dice al operador qué
    # resolver a mano) y es de bajo volumen (una pasada cada X minutos).
    reconcile_log = logging.getLogger("cmcourier.metrics.reconcile")
    reconcile_log.propagate = False
    reconcile_log.setLevel(logging.INFO)
    reconcile_handler = RotatingFileHandler(
        log_dir / f"reconcile-{date_stamp}.jsonl",
        maxBytes=rotation_bytes,
        backupCount=5,
        encoding="utf-8",
    )
    reconcile_handler.setFormatter(json_formatter)
    reconcile_handler.setLevel(logging.INFO)
    reconcile_handler.addFilter(pii_filter)
    reconcile_log.addHandler(reconcile_handler)

    # Logger de `slow ops`: el archivo por batch lo posee MetricsRecorder;
    # este logger existe solo por simetría de namespace.
    slow_ops_log = logging.getLogger("cmcourier.metrics.slow_ops")
    slow_ops_log.propagate = False
    slow_ops_log.setLevel(logging.CRITICAL + 1)


def _reset_all_handlers() -> None:
    for name in ("cmcourier", *_METRICS_LOGGERS):
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            with contextlib.suppress(Exception):
                handler.close()
        # Resetea la propagación al default (True). Un configure() previo
        # puede haberla desactivado en los loggers de métricas; sin reset,
        # los tests/corridas posteriores heredan ese estado y caplog deja
        # de funcionar.
        logger.propagate = True
        # Resetea el nivel para que una corrida previa "silenciada" no
        # suprima records en la próxima invocación.
        logger.setLevel(logging.NOTSET)
