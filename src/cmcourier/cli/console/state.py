"""Estado de sesión de la consola (123, por alias desde 131).

La máquina de estados que el mock v2 + los informes UX congelaron:

* credenciales de sesión en memoria (nunca a disco), una por ALIAS de
  conexión del registro (129) más ``cmis``, con invalidación al editar y
  contador de intentos sólo en conexiones ``as400`` (lockout del perfil
  al 3°);
* resultado del doctor con estado ``stale`` cuando cambian credenciales
  o (F2) los overrides de sesión;
* la secuencia sugerida credenciales → doctor → lanzar.

Sin dependencias de Textual: testeable en unit puro.
"""

from __future__ import annotations

__all__ = [
    "CMIS_ALIAS",
    "ConnInfo",
    "ConnState",
    "ConsoleState",
    "SessionCredentials",
    "connection_infos",
]

import os
import time
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Protocol

from cmcourier.cli.console.overrides import SessionOverrides, TriggerOverride
from cmcourier.cli.doctor import DoctorReport
from cmcourier.config.loader import Credential, Secrets
from cmcourier.config.schema import ConnectionRef, credential_env_vars

AS400_MAX_TRIES = 3
# M2 (informe UX v2): una credencial probada caduca — a los 45 min el
# chip degrada a "reprobar" y deja de contar como lista para lanzar.
CRED_TTL_S = 45 * 60
# El destino CMIS no es una conexión del registro: siempre existe y sus
# credenciales van en ``CMIS_USERNAME`` / ``CMIS_PASSWORD``.
CMIS_ALIAS = "cmis"
_EMPTY = Credential("", "")


class _HasConnectionRefs(Protocol):
    def connection_refs(self) -> tuple[ConnectionRef, ...]: ...


@dataclass
class SessionCredentials:
    """Credenciales mancomunadas que viven SOLO en memoria de proceso, por alias."""

    credentials: dict[str, Credential] = field(default_factory=dict)

    @classmethod
    def from_env(cls, aliases: Iterable[str] = ()) -> SessionCredentials:
        """Pre-carga ``cmis`` + los alias pedidos si el operador ya exportó las vars."""
        creds = cls()
        for alias in dict.fromkeys((CMIS_ALIAS, *aliases)):
            user_var, pass_var = credential_env_vars(alias)
            creds.set(alias, os.environ.get(user_var, ""), os.environ.get(pass_var, ""))
        return creds

    def set(self, alias: str, username: str, password: str) -> None:
        # Mismo strip que `load_secrets` (CLI): consola y CLI mandan la
        # MISMA credencial al AS400 — una divergencia gasta intentos de lockout.
        self.credentials[alias] = Credential(username.strip(), password.strip())

    def prefill_missing(self, aliases: Iterable[str]) -> None:
        """Prefill de entorno para alias que aparecen después del arranque
        (``rebuild_conn``); los ya cargados no se pisan."""
        fresh = self.from_env(alias for alias in aliases if alias not in self.credentials)
        for alias, cred in fresh.credentials.items():
            self.credentials.setdefault(alias, cred)

    def get(self, alias: str) -> Credential:
        return self.credentials.get(alias, _EMPTY)

    def complete(self, alias: str) -> bool:
        cred = self.get(alias)
        return bool(cred.username and cred.password)

    def to_secrets(self) -> Secrets:
        return Secrets({CMIS_ALIAS: _EMPTY, **self.credentials})


@dataclass(frozen=True)
class ConnInfo:
    """Una tarjeta de credenciales: alias del registro + dónde se usa."""

    alias: str
    kind: str
    host: str
    sites: tuple[str, ...]

    @property
    def title(self) -> str:
        return f"{self.alias} · {self.kind} · {self.host}"


def _site_label(site: str) -> str:
    return "tracking" if site == "tracking.as400_sync" else site


def connection_infos(config: _HasConnectionRefs) -> list[ConnInfo]:
    """Una entrada por alias (orden de config) con los sitios que lo usan.

    Fuente única para las tarjetas de [2] y para "qué aliases hacen falta":
    ``config.connection_refs()`` (129) ya excluye conexiones declaradas pero
    sin uso.
    """
    grouped: dict[str, tuple[ConnectionRef, list[str]]] = {}
    for ref in config.connection_refs():
        grouped.setdefault(ref.alias, (ref, []))[1].append(_site_label(ref.site))
    return [
        ConnInfo(alias=alias, kind=ref.kind, host=ref.spec.host, sites=tuple(sites))
        for alias, (ref, sites) in grouped.items()
    ]


@dataclass
class ConnState:
    """Estado de una conexión probada: idle | testing | ok | err."""

    status: str = "idle"
    message: str = ""
    tested_at: float = 0.0
    attempts: int = 0  # solo cuenta en kind as400 (lockout del perfil)
    kind: str = ""

    def is_fresh(self, *, now: float | None = None) -> bool:
        if self.status != "ok":
            return False
        return ((now if now is not None else time.time()) - self.tested_at) < CRED_TTL_S

    def age_label(self, *, now: float | None = None) -> str:
        if self.status != "ok" or not self.tested_at:
            return ""
        current = now if now is not None else time.time()
        minutes = int((current - self.tested_at) // 60)
        if not self.is_fresh(now=current):
            return f"probada hace {minutes} min · vencida, reprobar"
        return "recién probada" if minutes < 1 else f"probada hace {minutes} min"


def _conn_map(infos: Iterable[ConnInfo]) -> dict[str, ConnState]:
    conn = {CMIS_ALIAS: ConnState(kind=CMIS_ALIAS)}
    conn.update({info.alias: ConnState(kind=info.kind) for info in infos})
    return conn


@dataclass
class ConsoleState:
    """Estado agregado de la sesión de consola."""

    creds: SessionCredentials = field(default_factory=SessionCredentials.from_env)
    conn: dict[str, ConnState] = field(default_factory=lambda: _conn_map(()))
    doctor_report: DoctorReport | None = None
    doctor_group: str = "all"
    doctor_stale_reason: str = ""
    # 124: overrides APLICADOS (los que lee el launcher). El draft vive
    # en la pantalla CONFIG y jamás llega a una corrida sin promover.
    overrides: SessionOverrides = field(default_factory=SessionOverrides)

    @classmethod
    def for_config(
        cls, config: _HasConnectionRefs, *, creds: SessionCredentials | None = None
    ) -> ConsoleState:
        """131: ``conn`` = ``cmis`` + un slot por alias que la config usa."""
        infos = connection_infos(config)
        if creds is None:
            creds = SessionCredentials.from_env(info.alias for info in infos)
        return cls(creds=creds, conn=_conn_map(infos))

    def rebuild_conn(self, config: _HasConnectionRefs) -> bool:
        """Recompone ``conn`` cuando los overrides cambian las conexiones.

        Conserva el estado de los alias que sobreviven (misma kind) y
        devuelve True si el conjunto de tarjetas cambió.
        """
        fresh = _conn_map(connection_infos(config))
        for alias, state in fresh.items():
            previous = self.conn.get(alias)
            if previous is not None and previous.kind == state.kind:
                fresh[alias] = previous
        changed = list(fresh) != list(self.conn)
        self.conn = fresh
        self.creds.prefill_missing(fresh)
        return changed

    def promote_overrides(self, draft: SessionOverrides) -> None:
        """Valida y promueve el draft. El doctor queda stale."""
        self.overrides = draft.validated()
        self.mark_doctor_stale("cambiaron los overrides de sesión")

    def set_trigger_override(self, trigger: TriggerOverride | None) -> bool:
        """127: el pipeline elegido en [5] (``None`` = el del YAML).
        Devuelve True si cambió — y en ese caso el doctor queda stale."""
        if trigger == self.overrides.trigger:
            return False
        self.overrides = replace(self.overrides, trigger=trigger)
        self.mark_doctor_stale("cambió el pipeline a correr")
        return True

    # ------------------------------------------------- credenciales

    # Todos los métodos por alias son no-op si la tarjeta ya no existe: el
    # callback del worker puede llegar DESPUÉS de un `rebuild_conn`.

    def invalidate_conn(self, alias: str) -> None:
        """Editar un campo invalida la prueba y deja el doctor stale.

        El stale aplica aunque la conexión nunca se haya probado: el
        doctor corrió con las credenciales anteriores.
        """
        c = self.conn.get(alias)
        if c is None:
            return
        if c.status != "idle" or c.message:
            self.conn[alias] = ConnState(attempts=c.attempts, kind=c.kind)
        self.mark_doctor_stale("cambiaron las credenciales")

    def record_conn_result(self, alias: str, *, ok: bool, message: str) -> None:
        c = self.conn.get(alias)
        if c is None:
            return
        if ok:
            self.conn[alias] = ConnState(
                status="ok", message=message, tested_at=time.time(), kind=c.kind
            )
        else:
            counts = c.kind == "as400"
            self.conn[alias] = ConnState(
                status="err",
                message=message,
                attempts=c.attempts + 1 if counts else 0,
                kind=c.kind,
            )
        self.mark_doctor_stale("cambiaron las credenciales")

    def as400_needs_lockout_confirm(self, alias: str) -> bool:
        """True cuando el PRÓXIMO intento sobre una conexión as400 sería el del lockout."""
        c = self.conn.get(alias)
        return c is not None and c.kind == "as400" and c.attempts >= AS400_MAX_TRIES - 1

    def reset_attempts(self, alias: str) -> None:
        c = self.conn.get(alias)
        if c is not None:
            c.attempts = 0

    # ------------------------------------------------------ doctor

    def set_doctor_report(self, report: DoctorReport, *, group: str) -> None:
        self.doctor_report = report
        self.doctor_group = group
        self.doctor_stale_reason = ""

    def mark_doctor_stale(self, reason: str) -> None:
        if self.doctor_report is not None:
            self.doctor_stale_reason = reason

    @property
    def doctor_stale(self) -> bool:
        return bool(self.doctor_stale_reason)

    def doctor_verdict(self) -> str:
        """Para el guard de lanzamiento y la pantalla INICIO."""
        if self.doctor_report is None:
            return "sin correr"
        if self.doctor_stale:
            return "desactualizado"
        if self.doctor_group != "all":
            return f"parcial ({self.doctor_group})"
        return "con fallas" if self.doctor_report.has_failures else "aprobado"

    # ---------------------------------------------- máquina de estados

    def creds_ready(self, *, required: Iterable[str], now: float | None = None) -> bool:
        """cmis + cada alias requerido probados y frescos.

        Un alias requerido sin slot (la config cambió y ``rebuild_conn`` no
        corrió todavía) cuenta como NO listo.
        """
        for alias in (CMIS_ALIAS, *required):
            state = self.conn.get(alias)
            if state is None or not state.is_fresh(now=now):
                return False
        return True

    def next_steps(self, *, required: Iterable[str]) -> list[tuple[bool, str]]:
        """(hecho, texto) en el orden credenciales → doctor → lanzar."""
        creds = self.creds_ready(required=required)
        doctor = self.doctor_verdict() == "aprobado"
        return [
            (creds, "Cargar y probar credenciales en [2] CREDENCIALES"),
            (doctor, f"Correr el pre-flight en [4] DOCTOR — {self.doctor_verdict()}"),
            (False, "Lanzar desde [5] CORRER"),
        ]
