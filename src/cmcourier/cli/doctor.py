"""Validacion pre-flight del pipeline CMCourier.

Los checks de :data:`CHECK_NAMES` corren en ese orden, cada uno como
funcion privada que devuelve un :class:`CheckResult`. Las excepciones
dentro de un check se atrapan y se convierten en resultados FAIL:
:func:`run_doctor` NO puede levantar.

Grupos (:data:`_CHECK_GROUPS` / :func:`group_of`):
  * ``connections``: ``log_dir_writable``, ``cmis_connectivity`` (warmup +
    ``repositoryInfo``), ``as400_connectivity`` y ``mssql_connectivity``
    (129/130: una fila por conexion del registro que la config USA),
    ``tracking_openable`` (SQLite en WAL) y ``as400_sync`` (SKIP si
    ``tracking.as400_sync.enabled`` es false).
  * ``mapping``: ``mapping_completeness`` (>=1 fila del Modelo Documental).
  * ``metadata``: ``metadata_sources`` (cada source con alias tiene >=1
    fila) y ``sample_dry_run`` (S1..S4 sobre el primer doc, sin upload;
    SKIP con cero triggers o cero docs).
  * ``cm-types``: ``cm_type_alignment``; ``cm-targets``:
    ``cmis_folders_exist`` y ``cmis_properties_alignment`` (SKIP si CMIS
    no conecta).

Principio VIII de la Constitucion: NINGUN message o details de check
lleva valores de propiedad resueltos. Las claves operativas (`base_url`,
`db_path`, `mapping_count`, tipos faltantes) si estan OK.
"""

from __future__ import annotations

__all__ = [
    "CHECK_NAMES",
    "CheckResult",
    "CheckStatus",
    "DoctorReport",
    "build_uploader",
    "check_cmis",
    "check_connection",
    "group_of",
    "run_doctor",
]

import contextlib
import enum
import logging
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import NamedTuple, TypeVar

from cmcourier.adapters.assembly import PdfAssembler
from cmcourier.adapters.sources import (
    As400DataSource,
    MssqlDataSource,
    OdbcDataSource,
    TabularDataSource,
)
from cmcourier.adapters.tracking import SQLiteTrackingStore
from cmcourier.adapters.upload.cmis_uploader import CmisConfig, CmisUploader
from cmcourier.config.loader import Secrets
from cmcourier.config.schema import (
    AnyConnectionConfig,
    As400ConnectionConfig,
    As400MetadataSourceConfig,
    ConnectionRef,
    CsvMetadataSourceConfig,
    CsvTriggerConfig,
    MetadataSourceConfig,
    MssqlConnectionConfig,
    MssqlMetadataSourceConfig,
    PipelineConfig,
    SingleDocTriggerConfig,
)
from cmcourier.config.wiring import build_mapping_service, build_pipeline
from cmcourier.domain.ports import S0Strategy
from cmcourier.services.indexing import IndexingService
from cmcourier.services.mapping import MappingService
from cmcourier.services.metadata import MetadataService

_log = logging.getLogger(__name__)

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Tipos publicos
# ---------------------------------------------------------------------------


class CheckStatus(enum.StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    SKIP = "SKIP"


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    status: CheckStatus
    message: str
    details: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({}),
    )


@dataclass(frozen=True, slots=True)
class DoctorReport:
    results: tuple[CheckResult, ...]
    elapsed_seconds: float

    def _count(self, status: CheckStatus) -> int:
        return sum(1 for r in self.results if r.status == status)

    @property
    def passed_count(self) -> int:
        return self._count(CheckStatus.PASS)

    @property
    def failed_count(self) -> int:
        return self._count(CheckStatus.FAIL)

    @property
    def warn_count(self) -> int:
        return self._count(CheckStatus.WARN)

    @property
    def skip_count(self) -> int:
        return self._count(CheckStatus.SKIP)

    @property
    def has_failures(self) -> bool:
        return self.failed_count > 0


# ---------------------------------------------------------------------------
# run_doctor
# ---------------------------------------------------------------------------


# Grupo de checks -> nombres de checks. ``all`` es el sentinel.
_CHECK_GROUPS: dict[str, frozenset[str]] = {
    "connections": frozenset(
        {
            "log_dir_writable",
            "cmis_connectivity",
            "as400_connectivity",
            "mssql_connectivity",  # 130
            "tracking_openable",
            # 126: antes no pertenecía a ningún grupo (solo corría con
            # `all`). SKIP cuando el sync está off, así no cambia veredictos.
            "as400_sync",
        }
    ),
    "mapping": frozenset({"mapping_completeness"}),
    "metadata": frozenset({"metadata_sources", "sample_dry_run"}),
    "cm-types": frozenset({"cm_type_alignment"}),
    # 038: `cm-targets` es el paraguas nuevo; `cm-types` queda por back-compat.
    "cm-targets": frozenset(
        {
            "cm_type_alignment",
            "cmis_folders_exist",
            "cmis_properties_alignment",
        }
    ),
    "all": frozenset(),
}


# 126: los checks seleccionables, en el orden en que corren. Es la
# fuente de verdad para el CLI (`--check`) y el selector de la consola.
CHECK_NAMES: tuple[str, ...] = (
    "log_dir_writable",
    "cmis_connectivity",
    "as400_connectivity",
    "mssql_connectivity",  # 130
    "tracking_openable",
    "as400_sync",
    "mapping_completeness",
    "metadata_sources",
    "cm_type_alignment",
    "cmis_folders_exist",
    "cmis_properties_alignment",
    "sample_dry_run",
)


def group_of(name: str) -> str:
    """126: primer grupo (en orden de declaración) que contiene el check."""
    for group, members in _CHECK_GROUPS.items():
        if name in members:
            return group
    return "—"


def _selected(name: str, selected: str) -> bool:
    """True cuando ``name`` es el filtro, o pertenece al grupo de filtro."""
    if selected == "all" or selected == name:
        return True
    return name in _CHECK_GROUPS.get(selected, frozenset())


def check_cmis(config: PipelineConfig, secrets: Secrets) -> CheckResult:
    """123: chequeo puntual de conectividad CMIS para el botón
    "probar conexión" de la consola. Delgado sobre el check existente."""
    return _check_cmis_connectivity(config, secrets)


def check_connection(config: PipelineConfig, secrets: Secrets, alias: str) -> CheckResult:
    """131: chequeo puntual de UNA conexión del registro (129) por alias, para
    el botón "probar" de cada tarjeta de la consola. Mismo probe que el
    check de conectividad de su kind."""
    name = f"connection:{alias}"
    ref = next((r for r in config.connection_refs() if r.alias == alias), None)
    if ref is None:
        # 138: una conexión recién creada desde [2] todavía no tiene sitio —
        # igual se prueba (el registro es la fuente).
        spec = config.connections.get(alias)
        if spec is None:
            return CheckResult(
                name=name,
                status=CheckStatus.FAIL,
                message=f"connection alias {alias!r} is not declared in this config",
            )
        ref = ConnectionRef(alias, spec.kind, spec, "connections")
    probe = _probe_connection(ref, secrets)
    details = _frozen({"phase": probe.phase})
    if probe.outcome != "PASS":
        return CheckResult(
            name=name,
            status=CheckStatus.FAIL,
            message=probe.outcome.removeprefix("FAIL: "),
            details=details,
        )
    return CheckResult(
        name=name,
        status=CheckStatus.PASS,
        message=f"{ref.kind} reachable at {ref.spec.host} ({alias}) · {probe.detail}",
        details=details,
    )


def run_doctor(
    config: PipelineConfig,
    secrets: Secrets,
    *,
    selected: str = "all",
) -> DoctorReport:
    """Corre los checks pre-flight. ``selected`` filtra por grupo de check."""
    start = time.monotonic()
    results: list[CheckResult] = []
    if config.observability.unmask_pii:
        # 038: exponemos el modo `unmask-PII` al arranque para que el
        # operador nunca corra de casualidad un batch de PRD con valores
        # crudos chorreando hacia ``metrics.jsonl``. El estado es WARN, no
        # FAIL: debuguear es un uso legitimo de esta perilla, pero nunca
        # debe pasar desapercibida.
        results.append(
            CheckResult(
                name="unmask_pii_active",
                status=CheckStatus.WARN,
                message=(
                    "observability.unmask_pii=true — upload payload events "
                    "will emit raw PII values. Turn this off before any PRD batch."
                ),
                details=_frozen({"unmask_pii": "true"}),
            )
        )
    if _selected("log_dir_writable", selected):
        results.append(_check_log_dir_writable(config))
    if _selected("cmis_connectivity", selected):
        results.append(_check_cmis_connectivity(config, secrets))
    if _selected("as400_connectivity", selected):
        results.append(_check_as400_connectivity(config, secrets))
    if _selected("mssql_connectivity", selected):
        results.append(_check_mssql_connectivity(config, secrets))
    if _selected("tracking_openable", selected):
        results.append(_check_tracking_openable(config))
    if _selected("as400_sync", selected):
        results.append(_check_as400_sync(config, secrets))
    if _selected("mapping_completeness", selected):
        results.append(_check_mapping_completeness(config))
    if _selected("metadata_sources", selected):
        results.append(_check_metadata_sources(config, secrets))
    if _selected("cm_type_alignment", selected):
        cmis_check = next((r for r in results if r.name == "cmis_connectivity"), None)
        if cmis_check is not None and cmis_check.status != CheckStatus.PASS:
            results.append(_skip("cm_type_alignment", "cmis_connectivity FAILed; skipping"))
        else:
            results.append(_check_cm_type_alignment(config, secrets))
    if _selected("cmis_folders_exist", selected):
        cmis_check = next((r for r in results if r.name == "cmis_connectivity"), None)
        if cmis_check is not None and cmis_check.status != CheckStatus.PASS:
            results.append(_skip("cmis_folders_exist", "cmis_connectivity FAILed; skipping"))
        else:
            results.append(_check_cmis_folders_exist(config, secrets))
    if _selected("cmis_properties_alignment", selected):
        cmis_check = next((r for r in results if r.name == "cmis_connectivity"), None)
        if cmis_check is not None and cmis_check.status != CheckStatus.PASS:
            results.append(_skip("cmis_properties_alignment", "cmis_connectivity FAILed; skipping"))
        else:
            results.append(_check_cmis_properties_alignment(config, secrets))
    if _selected("sample_dry_run", selected):
        results.append(_check_sample_dry_run(config, secrets))
    return DoctorReport(
        results=tuple(results),
        elapsed_seconds=time.monotonic() - start,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _frozen(d: Mapping[str, str]) -> Mapping[str, str]:
    return MappingProxyType(dict(d))


def _fail(name: str, exc: Exception, base: Mapping[str, str] | None = None) -> CheckResult:
    details = dict(base or {})
    details["exc_type"] = type(exc).__name__
    details["error"] = str(exc)[:200]
    return CheckResult(
        name=name,
        status=CheckStatus.FAIL,
        message=f"{type(exc).__name__}: {exc}",
        details=_frozen(details),
    )


def _mapping_path_repr(config: PipelineConfig) -> str:
    """Muestra el o los paths del `Modelo Documental` sin importar el modo (035)."""
    mc = config.mapping
    if mc.csv_path is not None:
        return str(mc.csv_path)
    return f"{mc.rvi_cm_csv_path} + {mc.metadatos_csv_path}"


def _skip(name: str, reason: str) -> CheckResult:
    return CheckResult(
        name=name,
        status=CheckStatus.SKIP,
        message=reason,
        details=_frozen({"reason": reason}),
    )


def _open_metadata_source(
    config: PipelineConfig,
    source_cfg: MetadataSourceConfig,
    secrets: Secrets,
) -> TabularDataSource | OdbcDataSource:
    if isinstance(source_cfg, CsvMetadataSourceConfig):
        return TabularDataSource(source_cfg.csv_path)
    if isinstance(source_cfg, As400MetadataSourceConfig | MssqlMetadataSourceConfig):
        ref = config.connection_ref(f"metadata:{source_cfg.alias}")
        if ref is None:  # pragma: no cover — el schema lo garantiza
            raise RuntimeError(f"metadata source {source_cfg.alias!r} has no connection")
        return _open_odbc(ref, secrets, table=source_cfg.table or "", query=source_cfg.query)
    raise RuntimeError(f"unknown metadata source kind: {source_cfg!r}")


def _open_odbc(
    ref: ConnectionRef,
    secrets: Secrets,
    *,
    table: str = "",
    query: str | None = None,
) -> OdbcDataSource:
    """130: despacha por ``ref.kind`` al adapter ODBC concreto."""
    if ref.kind == "mssql":
        return _open_mssql(ref, secrets, table=table, query=query)
    return _open_as400(ref, secrets, table=table, query=query)


def _open_as400(
    ref: ConnectionRef,
    secrets: Secrets,
    *,
    table: str = "",
    query: str | None = None,
) -> As400DataSource:
    """``As400DataSource`` para *ref* (129); ``require`` nombra las env vars del alias."""
    spec = ref.spec
    if not isinstance(spec, As400ConnectionConfig):  # pragma: no cover — el schema lo garantiza
        raise RuntimeError(f"connection {ref.alias!r} is not as400")
    credential = secrets.require(ref.alias)
    return As400DataSource(
        host=spec.host,
        port=spec.port,
        database=spec.database,
        driver=spec.driver,
        username=credential.username,
        password=credential.password,
        table=table,
        query=query,
    )


def _open_mssql(
    ref: ConnectionRef,
    secrets: Secrets,
    *,
    table: str = "",
    query: str | None = None,
) -> MssqlDataSource:
    """130: ``MssqlDataSource`` para *ref*; ``require`` nombra las env vars del alias."""
    spec = ref.spec
    if not isinstance(spec, MssqlConnectionConfig):  # pragma: no cover — el schema lo garantiza
        raise RuntimeError(f"connection {ref.alias!r} is not mssql")
    credential = secrets.require(ref.alias)
    return MssqlDataSource(
        host=spec.host,
        port=spec.port,
        database=spec.database,
        driver=spec.driver,
        username=credential.username,
        password=credential.password,
        encrypt=spec.encrypt,
        trust_server_certificate=spec.trust_server_certificate,
        table=table,
        query=query,
    )


def build_uploader(config: PipelineConfig, secrets: Secrets) -> CmisUploader:
    """141: público — la consola arma el mismo :class:`CmisUploader` que el
    doctor para el tiro de prueba de ``[0]`` (mismas perillas de `cmis`,
    mismas credenciales de sesión)."""
    return CmisUploader(
        CmisConfig(
            base_url=config.cmis.base_url,
            repo_id=config.cmis.repo_id,
            username=secrets.cmis_username,
            password=secrets.cmis_password,
            timeout_seconds=config.cmis.timeout_seconds,
            verify_ssl=config.cmis.verify_ssl,
            max_bandwidth_mbps=config.cmis.max_bandwidth_mbps,
            retry_max_attempts=config.cmis.retry_max_attempts,
            retry_base_delay_s=config.cmis.retry_base_delay_s,
        )
    )


def _try(stage: str, fn: Callable[[], T]) -> T | CheckResult:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — el doctor atrapa toda excepcion del check
        return _fail("sample_dry_run", exc, {"stage": stage})


# ---------------------------------------------------------------------------
# Checks individuales
# ---------------------------------------------------------------------------


def _check_log_dir_writable(config: PipelineConfig) -> CheckResult:
    log_dir = config.observability.log_dir
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        probe = log_dir / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        return _fail(
            "log_dir_writable",
            exc,
            {"log_dir": str(log_dir)},
        )
    return CheckResult(
        name="log_dir_writable",
        status=CheckStatus.PASS,
        message=f"log_dir is writable at {log_dir}",
        details=_frozen({"log_dir": str(log_dir)}),
    )


def _check_cmis_connectivity(config: PipelineConfig, secrets: Secrets) -> CheckResult:
    try:
        uploader = build_uploader(config, secrets)
        info = uploader.test_connection()
    except Exception as exc:  # noqa: BLE001
        return _fail(
            "cmis_connectivity",
            exc,
            {"base_url": config.cmis.base_url},
        )
    repo_id = info.get("repository_id", "")
    if not repo_id:
        return CheckResult(
            name="cmis_connectivity",
            status=CheckStatus.FAIL,
            message="CMIS returned empty repository_id",
            details=_frozen({"base_url": config.cmis.base_url}),
        )
    return CheckResult(
        name="cmis_connectivity",
        status=CheckStatus.PASS,
        message=f"CMIS reachable at {config.cmis.base_url}",
        details=_frozen({"repository_id": repo_id}),
    )


def _check_as400_connectivity(config: PipelineConfig, secrets: Secrets) -> CheckResult:
    """129: prueba CADA conexión as400 que la config necesita, una fila por alias."""
    return _check_connections("as400_connectivity", config, secrets, kind="as400")


def _check_mssql_connectivity(config: PipelineConfig, secrets: Secrets) -> CheckResult:
    """130: ídem para las conexiones ``mssql`` del registro."""
    return _check_connections("mssql_connectivity", config, secrets, kind="mssql")


def _check_connections(
    name: str, config: PipelineConfig, secrets: Secrets, *, kind: str
) -> CheckResult:
    groups = _group_refs(config, kind)
    if not groups:
        return _skip(name, f"no {kind} connections in config")
    details: dict[str, str] = {}
    failures: list[str] = []
    # Dos objetos inline distintos comparten el alias implícito `as400`:
    # la fila de detalle se desambigua por host para no pisar ninguna.
    alias_counts = Counter(ref.alias for ref, _ in groups)
    for ref, sites in groups:
        probe = _probe_connection(ref, secrets)
        outcome = probe.outcome
        key = ref.alias if alias_counts[ref.alias] == 1 else f"{ref.alias}@{ref.spec.host}"
        detail = f" · {probe.detail}" if probe.detail else ""
        details[key] = f"{outcome} · {' · '.join(sites)}{detail}"
        if outcome != "PASS":
            failures.append(f"{ref.alias} ({', '.join(sites)}): {outcome.removeprefix('FAIL: ')}")
    if failures:
        return CheckResult(
            name=name,
            status=CheckStatus.FAIL,
            message="; ".join(failures),
            details=_frozen(details),
        )
    hosts = ", ".join(f"{ref.alias}@{ref.spec.host}" for ref, _ in groups)
    return CheckResult(
        name=name,
        status=CheckStatus.PASS,
        message=f"{kind} reachable: {hosts}",
        details=_frozen(details),
    )


def _group_refs(config: PipelineConfig, kind: str) -> list[tuple[ConnectionRef, list[str]]]:
    """Refs del *kind* deduplicados por (alias, spec) con la lista de sitios que los usan."""
    groups: dict[tuple[str, AnyConnectionConfig], tuple[ConnectionRef, list[str]]] = {}
    for ref in config.connection_refs():
        if ref.kind != kind:
            continue
        key = (ref.alias, ref.spec)
        if key not in groups:
            groups[key] = (ref, [])
        groups[key][1].append(ref.site)
    return list(groups.values())


class _Probe(NamedTuple):
    """Resultado de ``_probe_connection``: ``outcome`` es ``"PASS"`` o
    ``"FAIL: <motivo>"``; ``phase`` dice DÓNDE falló — ``connect`` (login
    rechazado / host inalcanzable: cuenta como intento de sign-on en el
    iSeries) o ``query`` (el login pasó y falló la consulta de prueba: NO
    es un sign-on fallido y no acerca al perfil al lockout). ``detail``
    acompaña al PASS: qué consulta se corrió y de dónde salió."""

    outcome: str
    phase: str
    detail: str = ""


def _probe_sql(ref: ConnectionRef) -> tuple[str, str] | None:
    """143 — ``(sql, origen)`` de la consulta de prueba, o None si no hay
    ninguna. Orden: ``probe_query`` explícita de la conexión → derivada de
    la tabla/query del sitio que la usa → nada (sólo ping). Nunca una
    pseudo-tabla fija: con SafeNet/i sólo hay permiso sobre los objetos
    whitelisteados para el perfil, y ``SYSIBM.SYSDUMMY1`` no lo está."""
    if ref.spec.probe_query:
        return ref.spec.probe_query.strip(), "probe_query"
    if ref.probe_sql:
        return ref.probe_sql, f"derivada de {ref.site}"
    return None


def _probe_connection(ref: ConnectionRef, secrets: Secrets) -> _Probe:
    if secrets.get(ref.alias) is None:
        user_var, pass_var = ref.env_vars
        return _Probe(
            f"FAIL: credentials missing in environment (set {user_var} / {pass_var})", "config"
        )
    try:
        src = _open_odbc(ref, secrets)
    except Exception as exc:  # noqa: BLE001
        return _Probe(f"FAIL: {_describe_error(exc)}", "config")
    probe = _probe_sql(ref)
    try:
        try:
            src.ping()
        except Exception as exc:  # noqa: BLE001
            return _Probe(f"FAIL: no conectó — {_describe_error(exc)}", "connect")
        if probe is None:
            return _Probe("PASS", "ok", "conectó · sin tabla asignada todavía")
        sql, origin = probe
        try:
            src.query(sql, [])
        except Exception as exc:  # noqa: BLE001
            return _Probe(
                "FAIL: conectó (usuario y contraseña aceptados) pero la consulta de prueba "
                f"{sql!r} ({origin}) falló — {_describe_error(exc)}",
                "query",
            )
    finally:
        src.close()
    return _Probe("PASS", "ok", f"consulta de prueba {sql!r} ({origin}) OK")


def _describe_error(exc: BaseException, *, limit: int = 300) -> str:
    """``Tipo: mensaje ← causa`` — la causa es el error crudo del driver
    (``pyodbc.Error``), que trae el texto REAL (``SQL0332 …``, ``CWBSY0002
    …``); el ``IndexingError`` que lo envuelve sólo conserva el sqlstate."""
    text = f"{type(exc).__name__}: {exc}"
    cause = exc.__cause__
    if cause is not None and str(cause):
        text = f"{text} ← {cause}"
    return text[:limit]


def _check_tracking_openable(config: PipelineConfig) -> CheckResult:
    db_path = config.tracking.db_path
    try:
        store = SQLiteTrackingStore(db_path)
        store.close()
    except Exception as exc:  # noqa: BLE001
        return _fail("tracking_openable", exc, {"db_path": str(db_path)})
    return CheckResult(
        name="tracking_openable",
        status=CheckStatus.PASS,
        message=f"SQLite tracking store openable at {db_path}",
        details=_frozen({"db_path": str(db_path)}),
    )


def _check_as400_sync(config: PipelineConfig, secrets: Secrets) -> CheckResult:
    """034: valida la conexion AS400 NIARVILOG + tabla cuando el sync esta on.

    SKIPea cuando ``tracking.as400_sync.enabled`` es False. Cuando es
    True, conecta al AS400 configurado y corre una probe-query contra la
    tabla NIARVILOG.
    """
    sync_cfg = config.tracking.as400_sync
    if not sync_cfg.enabled:
        return _skip("as400_sync", "disabled (tracking.as400_sync.enabled=false)")
    ref = config.connection_ref("tracking.as400_sync")
    if ref is None:  # pragma: no cover — el schema ya lo protege
        return CheckResult(
            name="as400_sync",
            status=CheckStatus.FAIL,
            message="as400_sync.enabled=true but connection is missing",
            details=_frozen({"reason": "missing_connection"}),
        )
    host = ref.spec.host
    if secrets.get(ref.alias) is None:
        user_var, pass_var = ref.env_vars
        return CheckResult(
            name="as400_sync",
            status=CheckStatus.FAIL,
            message=f"AS400 credentials missing in environment (set {user_var} / {pass_var})",
            details=_frozen({"host": host, "alias": ref.alias}),
        )
    full_table = f"{sync_cfg.library}.{sync_cfg.table}"
    try:
        src = _open_as400(ref, secrets, table=full_table)
        try:
            # `1=0` mantiene la probe barata (cero filas devueltas, solo check de schema).
            src.query(f"SELECT 1 FROM {full_table} WHERE 1=0", [])
        finally:
            src.close()
    except Exception as exc:  # noqa: BLE001
        return _fail("as400_sync", exc, {"host": host, "table": full_table})
    return CheckResult(
        name="as400_sync",
        status=CheckStatus.PASS,
        message=f"AS400 NIARVILOG reachable at {host}/{full_table}",
        details=_frozen({"host": host, "table": full_table}),
    )


def _check_mapping_completeness(config: PipelineConfig) -> CheckResult:
    try:
        mapping = build_mapping_service(config.mapping)
        count = mapping.count()
    except Exception as exc:  # noqa: BLE001
        return _fail(
            "mapping_completeness",
            exc,
            {"csv_path": _mapping_path_repr(config)},
        )
    if count == 0:
        return CheckResult(
            name="mapping_completeness",
            status=CheckStatus.WARN,
            message="Modelo Documental has zero rows",
            details=_frozen({"mapping_count": "0"}),
        )
    return CheckResult(
        name="mapping_completeness",
        status=CheckStatus.PASS,
        message=f"Modelo Documental has {count} mappings",
        details=_frozen({"mapping_count": str(count)}),
    )


def _check_metadata_sources(config: PipelineConfig, secrets: Secrets) -> CheckResult:
    empty_aliases: list[str] = []
    counts: dict[str, str] = {}
    for source_cfg in config.metadata.sources:
        try:
            src = _open_metadata_source(config, source_cfg, secrets)
            try:
                count = src.count()
            finally:
                src.close()
        except Exception as exc:  # noqa: BLE001
            return _fail(
                "metadata_sources",
                exc,
                {"alias": source_cfg.alias, "kind": source_cfg.kind},
            )
        counts[source_cfg.alias] = str(count)
        if count == 0:
            empty_aliases.append(source_cfg.alias)
    if empty_aliases:
        return CheckResult(
            name="metadata_sources",
            status=CheckStatus.WARN,
            message=f"empty sources: {','.join(empty_aliases)}",
            details=_frozen({**counts, "empty_aliases": ",".join(empty_aliases)}),
        )
    return CheckResult(
        name="metadata_sources",
        status=CheckStatus.PASS,
        message=f"{len(config.metadata.sources)} metadata sources, all non-empty",
        details=_frozen(counts),
    )


def _check_cm_type_alignment(config: PipelineConfig, secrets: Secrets) -> CheckResult:
    try:
        mapping = build_mapping_service(config.mapping)
        # 040: respetar el override ``cmis_type`` (035): el upload usa
        # ``m.cmis_type or m.cm_object_type``, asi que el pre-flight tiene
        # que chequear el mismo tipo efectivo que va a viajar en el wire.
        unique_types = sorted({(m.cmis_type or m.cm_object_type) for m in mapping.get_all()})
        uploader = build_uploader(config, secrets)
    except Exception as exc:  # noqa: BLE001
        return _fail("cm_type_alignment", exc)
    missing: list[str] = []
    for type_id in unique_types:
        try:
            uploader.get_type_definition(type_id)
        except Exception:  # noqa: BLE001 — sacamos todos los faltantes en una sola pasada
            missing.append(type_id)
    if missing:
        return CheckResult(
            name="cm_type_alignment",
            status=CheckStatus.FAIL,
            message=f"{len(missing)} cm_object_type(s) missing on CM",
            details=_frozen(
                {
                    "missing_types": ",".join(missing),
                    "checked_count": str(len(unique_types)),
                }
            ),
        )
    return CheckResult(
        name="cm_type_alignment",
        status=CheckStatus.PASS,
        message=f"all {len(unique_types)} cm_object_type(s) resolve on CM",
        details=_frozen({"checked_count": str(len(unique_types))}),
    )


def _check_cmis_folders_exist(config: PipelineConfig, secrets: Secrets) -> CheckResult:
    """038: verifica que exista cada ``CMISFolder`` declarado en MapeoRVI_CM.

    Read-only: nunca crea. SKIP si ninguna fila tiene ``cmis_folder``
    populado (modo de mapping consolidado, o modo split donde la columna
    queda vacia en todas las filas: el check existente
    ``cm_type_alignment`` sigue cubriendo el lado de los tipos).
    """
    try:
        mapping = build_mapping_service(config.mapping)
        unique_folders = sorted({m.cmis_folder for m in mapping.get_all() if m.cmis_folder})
        if not unique_folders:
            return _skip(
                "cmis_folders_exist",
                "no CMISFolder populated in mapping; nothing to verify",
            )
        uploader = build_uploader(config, secrets)
    except Exception as exc:  # noqa: BLE001
        return _fail("cmis_folders_exist", exc)
    missing: list[str] = []
    for folder in unique_folders:
        try:
            if not uploader.verify_folder_exists(folder):
                missing.append(folder)
        except Exception:  # noqa: BLE001 — sacamos todos los faltantes en una sola pasada
            missing.append(folder)
    if missing:
        return CheckResult(
            name="cmis_folders_exist",
            status=CheckStatus.FAIL,
            message=(
                f"{len(missing)} CMIS folder(s) missing on the server. "
                f"Create them in CMIS before running the pipeline."
            ),
            details=_frozen(
                {
                    "missing_folders": ",".join(missing),
                    "checked_count": str(len(unique_folders)),
                }
            ),
        )
    return CheckResult(
        name="cmis_folders_exist",
        status=CheckStatus.PASS,
        message=f"all {len(unique_folders)} CMIS folder(s) exist on the server",
        details=_frozen({"checked_count": str(len(unique_folders))}),
    )


def _check_cmis_properties_alignment(config: PipelineConfig, secrets: Secrets) -> CheckResult:
    """038: verifica que cada par ``(CMISType, CMISPropertyId)`` declarado
    en el join `mapping x MetadatosCM` exista en las
    ``propertyDefinitions`` del tipo CMIS.

    SKIP si ninguna fila del mapping lleva un catalogo
    ``cmis_property_ids`` (columna ausente o todas las celdas en blanco).
    """
    try:
        mapping = build_mapping_service(config.mapping)
        pairs: list[tuple[str, str]] = []
        for m in mapping.get_all():
            if not m.cmis_property_ids:
                continue
            type_id = m.cmis_type or m.cm_object_type
            for cmis_prop in m.cmis_property_ids.values():
                pairs.append((type_id, cmis_prop))
        unique_pairs = sorted(set(pairs))
        if not unique_pairs:
            return _skip(
                "cmis_properties_alignment",
                "no CMISPropertyId populated in MetadatosCM; nothing to verify",
            )
        uploader = build_uploader(config, secrets)
    except Exception as exc:  # noqa: BLE001
        return _fail("cmis_properties_alignment", exc)
    type_defs: dict[str, Mapping[str, object]] = {}
    missing_by_type: dict[str, list[str]] = {}
    for type_id, cmis_prop in unique_pairs:
        if type_id not in type_defs:
            try:
                type_defs[type_id] = uploader.get_type_definition(type_id)
            except Exception:  # noqa: BLE001
                missing_by_type.setdefault(type_id, []).append("<type not found>")
                continue
        props = type_defs[type_id].get("propertyDefinitions") or {}
        if not isinstance(props, Mapping) or cmis_prop not in props:
            missing_by_type.setdefault(type_id, []).append(cmis_prop)
    if missing_by_type:
        summary = "; ".join(
            f"{t} missing {len(v)}: {', '.join(v)}" for t, v in sorted(missing_by_type.items())
        )
        return CheckResult(
            name="cmis_properties_alignment",
            status=CheckStatus.FAIL,
            message=f"{sum(len(v) for v in missing_by_type.values())} property gap(s): {summary}",
            details=_frozen(
                {
                    "missing": summary,
                    "checked_pairs": str(len(unique_pairs)),
                }
            ),
        )
    return CheckResult(
        name="cmis_properties_alignment",
        status=CheckStatus.PASS,
        message=f"all {len(unique_pairs)} (type, property) pair(s) align with CMIS",
        details=_frozen({"checked_pairs": str(len(unique_pairs))}),
    )


def _check_sample_dry_run(config: PipelineConfig, secrets: Secrets) -> CheckResult:
    if isinstance(config.trigger, SingleDocTriggerConfig):
        return _skip("sample_dry_run", "trigger_kind_single_doc_requires_cli_args")
    # Sin process pool: el dry-run camina S4 in-process y el pool de
    # ``build_pipeline`` sólo se apaga en ``atexit`` — en la consola, cada
    # corrida del doctor dejaría N procesos vivos (antagonista 128).
    no_pool = config.model_copy(
        update={"processing": config.processing.model_copy(update={"s4_use_processes": False})}
    )
    try:
        pipeline = build_pipeline(no_pool, secrets)
    except Exception as exc:  # noqa: BLE001
        return _fail("sample_dry_run", exc, {"stage": "construction"})
    # Re-extraemos los colaboradores que necesitamos (el orchestrator los esconde).
    # El doctor camina S1..S4 a mano para no tocar el tracking store.
    services = _DryRunServices(
        trigger_strategy=pipeline._trigger_strategy,
        indexing=pipeline._indexing_service,
        mapping=pipeline._mapping_service,
        metadata=pipeline._metadata_service,
        assembler=pipeline._assembler,
    )
    descriptor = (
        str(config.trigger.csv_path) if isinstance(config.trigger, CsvTriggerConfig) else ""
    )
    try:
        return _dry_run_first_doc(services, source_descriptor=descriptor)
    finally:
        pipeline.tracking_store.close()


# ---------------------------------------------------------------------------
# Plumbing del dry-run
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _DryRunServices:
    trigger_strategy: S0Strategy
    indexing: IndexingService
    mapping: MappingService
    metadata: MetadataService
    assembler: PdfAssembler


def _dry_run_first_doc(services: _DryRunServices, *, source_descriptor: str) -> CheckResult:
    triggers_iter = _try("S0", lambda: list(services.trigger_strategy.acquire(source_descriptor)))
    if isinstance(triggers_iter, CheckResult):
        return triggers_iter
    if not triggers_iter:
        return _skip("sample_dry_run", "no_triggers")
    trigger = triggers_iter[0]
    docs = _try("S1", lambda: services.indexing.enrich(trigger))
    if isinstance(docs, CheckResult):
        return docs
    if not docs:
        return CheckResult(
            name="sample_dry_run",
            status=CheckStatus.SKIP,
            message="first trigger resolved to no documents",
            details=_frozen(
                {
                    "reason": "no_docs",
                    "shortname": trigger.audit_row().get("shortname") or "<unknown>",
                }
            ),
        )
    doc = docs[0]
    mapping = _try("S2", lambda: services.mapping.get_mapping(doc.index7))
    if isinstance(mapping, CheckResult):
        return mapping
    resolution = _try("S3", lambda: services.metadata.resolve(trigger, doc, mapping))
    if isinstance(resolution, CheckResult):
        return resolution
    staged = _try("S4", lambda: services.assembler.assemble(doc))
    if isinstance(staged, CheckResult):
        return staged
    # Limpieza best-effort para que el doctor no deje artifacts.
    with contextlib.suppress(OSError):
        staged.path.unlink(missing_ok=True)
    return CheckResult(
        name="sample_dry_run",
        status=CheckStatus.PASS,
        message=f"S1..S4 dry-run OK for {doc.txn_num}",
        details=_frozen({"txn_num": doc.txn_num, "stages": "S1,S2,S3,S4"}),
    )
