"""Estado de sesión de la consola (123).

La máquina de estados que el mock v2 + los informes UX congelaron:

* credenciales de sesión en memoria (nunca a disco), con invalidación
  al editar y contador de intentos AS400 (lockout del perfil al 3°);
* resultado del doctor con estado ``stale`` cuando cambian credenciales
  o (F2) los overrides de sesión;
* la secuencia sugerida credenciales → doctor → lanzar.

Sin dependencias de Textual: testeable en unit puro.
"""

from __future__ import annotations

__all__ = ["ConnState", "ConsoleState", "SessionCredentials"]

import os
import time
from dataclasses import dataclass, field, replace

from cmcourier.cli.console.overrides import SessionOverrides, TriggerOverride
from cmcourier.cli.doctor import DoctorReport
from cmcourier.config.loader import Secrets

AS400_MAX_TRIES = 3
# M2 (informe UX v2): una credencial probada caduca — a los 45 min el
# chip degrada a "reprobar" y deja de contar como lista para lanzar.
CRED_TTL_S = 45 * 60


@dataclass
class SessionCredentials:
    """Credenciales mancomunadas que viven SOLO en memoria de proceso."""

    cmis_username: str = ""
    cmis_password: str = ""
    as400_username: str = ""
    as400_password: str = ""

    @classmethod
    def from_env(cls) -> SessionCredentials:
        """Pre-carga desde env si el operador ya exportó las variables."""
        return cls(
            cmis_username=os.environ.get("CMIS_USERNAME", "").strip(),
            cmis_password=os.environ.get("CMIS_PASSWORD", ""),
            as400_username=os.environ.get("AS400_USERNAME", "").strip(),
            as400_password=os.environ.get("AS400_PASSWORD", ""),
        )

    def to_secrets(self) -> Secrets:
        return Secrets(
            cmis_username=self.cmis_username,
            cmis_password=self.cmis_password,
            as400_username=self.as400_username,
            as400_password=self.as400_password,
        )

    def cmis_complete(self) -> bool:
        return bool(self.cmis_username and self.cmis_password)

    def as400_complete(self) -> bool:
        return bool(self.as400_username and self.as400_password)


@dataclass
class ConnState:
    """Estado de una conexión probada: idle | testing | ok | err."""

    status: str = "idle"
    message: str = ""
    tested_at: float = 0.0
    attempts: int = 0  # solo relevante para AS400 (lockout del perfil)

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


@dataclass
class ConsoleState:
    """Estado agregado de la sesión de consola."""

    creds: SessionCredentials = field(default_factory=SessionCredentials.from_env)
    conn: dict[str, ConnState] = field(
        default_factory=lambda: {"cmis": ConnState(), "as400": ConnState()}
    )
    doctor_report: DoctorReport | None = None
    doctor_group: str = "all"
    doctor_stale_reason: str = ""
    # 124: overrides APLICADOS (los que lee el launcher). El draft vive
    # en la pantalla CONFIG y jamás llega a una corrida sin promover.
    overrides: SessionOverrides = field(default_factory=SessionOverrides)

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

    def invalidate_conn(self, which: str) -> None:
        """Editar un campo invalida la prueba y deja el doctor stale.

        El stale aplica aunque la conexión nunca se haya probado: el
        doctor corrió con las credenciales anteriores.
        """
        c = self.conn[which]
        if c.status != "idle" or c.message:
            self.conn[which] = ConnState(attempts=c.attempts)
        self.mark_doctor_stale("cambiaron las credenciales")

    def record_conn_result(self, which: str, *, ok: bool, message: str) -> None:
        c = self.conn[which]
        if ok:
            self.conn[which] = ConnState(status="ok", message=message, tested_at=time.time())
        else:
            self.conn[which] = ConnState(
                status="err",
                message=message,
                attempts=c.attempts + 1 if which == "as400" else 0,
            )
        self.mark_doctor_stale("cambiaron las credenciales")

    def as400_needs_lockout_confirm(self) -> bool:
        """True cuando el PRÓXIMO intento AS400 sería el del lockout."""
        return self.conn["as400"].attempts >= AS400_MAX_TRIES - 1

    def reset_as400_attempts(self) -> None:
        self.conn["as400"].attempts = 0

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

    def creds_ready(self, *, as400_required: bool = True, now: float | None = None) -> bool:
        cmis_ok = self.conn["cmis"].is_fresh(now=now)
        return cmis_ok and (not as400_required or self.conn["as400"].is_fresh(now=now))

    def next_steps(self, *, as400_required: bool = True) -> list[tuple[bool, str]]:
        """(hecho, texto) en el orden credenciales → doctor → lanzar."""
        creds = self.creds_ready(as400_required=as400_required)
        doctor = self.doctor_verdict() == "aprobado"
        return [
            (creds, "Cargar y probar credenciales en [2] CREDENCIALES"),
            (doctor, f"Correr el pre-flight en [4] DOCTOR — {self.doctor_verdict()}"),
            (False, "Lanzar desde [5] CORRER"),
        ]
