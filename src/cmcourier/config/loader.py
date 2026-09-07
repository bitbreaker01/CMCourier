"""Loader de configuración YAML + lector de secretos desde env-vars.

Ambas funciones lanzan :class:`ConfigurationError` (de
``cmcourier.domain.exceptions``) ante fallos, con contexto estructurado
para que la CLI pueda exponer detalle diagnóstico al operador.

Principio V de la Constitución: la configuración es la única fuente de
verdad. Principio VIII: las credenciales viven en variables de entorno,
NUNCA en el archivo YAML.
"""

from __future__ import annotations

__all__ = ["Credential", "Secrets", "load_config", "load_secrets"]

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from cmcourier.config.schema import (
    INLINE_CONNECTION_ALIAS,
    PipelineConfig,
    credential_env_vars,
)
from cmcourier.domain.exceptions import ConfigurationError


@dataclass(frozen=True, slots=True)
class Credential:
    """Un par usuario / contraseña de una conexión (o del destino CMIS)."""

    username: str
    password: str


@dataclass(frozen=True, slots=True)
class Secrets:
    """129 — credenciales por alias de conexión, leídas de env vars al arrancar.

    ``cmis`` está SIEMPRE presente (es el destino). El resto de los aliases
    son los que la config referencia (:meth:`PipelineConfig.connection_refs`)
    y se leen de ``<ALIAS>_USERNAME`` / ``<ALIAS>_PASSWORD``; los que
    falten se detectan al construir el adapter con :meth:`require`.
    """

    credentials: Mapping[str, Credential]

    def __post_init__(self) -> None:
        if "cmis" not in self.credentials:
            raise ValueError("Secrets requires the 'cmis' credential")

    @property
    def cmis(self) -> Credential:
        return self.credentials["cmis"]

    @property
    def cmis_username(self) -> str:
        return self.cmis.username

    @property
    def cmis_password(self) -> str:
        return self.cmis.password

    def get(self, alias: str) -> Credential | None:
        """La credencial del alias, o ``None`` si falta o alguna mitad está vacía."""
        credential = self.credentials.get(alias)
        if credential is None or not credential.username or not credential.password:
            return None
        return credential

    def require(self, alias: str) -> Credential:
        """Como :meth:`get` pero levanta :class:`ConfigurationError` nombrando las env vars."""
        credential = self.get(alias)
        if credential is None:
            raise ConfigurationError(
                f"credentials for connection {alias!r} are missing or empty",
                alias=alias,
                missing_vars=list(credential_env_vars(alias)),
            )
        return credential


def load_config(path: Path) -> PipelineConfig:
    """Lee *path* como YAML y devuelve un :class:`PipelineConfig` validado."""
    if not path.is_file():
        raise ConfigurationError("config file not found", config_path=str(path))
    try:
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigurationError("invalid YAML", reason=str(exc)) from exc
    if not isinstance(data, dict):
        raise ConfigurationError(
            "config root must be a mapping",
            actual_type=type(data).__name__,
        )
    _inject_default_kinds(data)
    _reject_removed_kinds(data)
    try:
        return PipelineConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigurationError(
            "config validation failed",
            errors=exc.errors(),
        ) from exc


def _reject_removed_kinds(data: dict[str, object]) -> None:
    """048: ``trigger.kind: as400`` fue removido.

    "AS400" ahora es una elección de *source*, no un `kind` de trigger —
    el pipeline RVABREP es el mismo pipeline independientemente de dónde
    viva su tabla RVABREP. El error de unión discriminada de Pydantic
    ante un ``kind`` desconocido es críptico; exponemos uno directivo
    que apunta a la nueva forma.
    """
    trigger = data.get("trigger")
    if isinstance(trigger, dict) and trigger.get("kind") == "as400":
        raise ConfigurationError(
            "trigger.kind 'as400' was removed in 0.51.0 — AS400 is now a "
            "source choice, not a trigger kind. Use trigger.kind: rvabrep "
            "and set indexing.source.kind: as400 with connection + query.",
            removed_kind="as400",
            migrate_to="trigger.kind: rvabrep + indexing.source.kind: as400",
        )


def _inject_default_kinds(data: dict[str, object]) -> None:
    """Retrocompatibilidad: el discriminador ``kind`` por defecto es ``"csv"``.

    Las uniones discriminadas de Pydantic v2 requieren el campo
    discriminador. Las configuraciones existentes del change 012 omiten
    ``kind`` por completo — los schemas originales tenían formas únicas
    (``TriggerCsvConfig``, ``MetadataSourceConfig`` solo `csv`). Inyectamos
    ``kind: "csv"`` antes de validar para que esos YAMLs sigan cargando.

    Cubre dos superficies de discriminador:
      * ``trigger.kind`` (del change 014).
      * ``metadata.sources[i].kind`` (del change 015).
    """
    trigger = data.get("trigger")
    if isinstance(trigger, dict) and "kind" not in trigger:
        trigger["kind"] = "csv"
    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        sources = metadata.get("sources")
        if isinstance(sources, list):
            for source in sources:
                if isinstance(source, dict) and "kind" not in source:
                    source["kind"] = "csv"


def load_secrets(config: PipelineConfig | None = None, *, require_cmis: bool = True) -> Secrets:
    """Lee ``CMIS_USERNAME`` / ``CMIS_PASSWORD`` (requeridos) + una credencial
    por cada alias que *config* necesita (opcionales).

    Sin *config* lee sólo ``cmis`` + el alias implícito ``as400`` —
    compat para comandos que no cargan YAML. ``require_cmis=False`` es
    para comandos de sólo lectura (``inspect``) que no tocan CMIS pero sí
    pueden necesitar las conexiones de la config.
    """
    cmis = _read_credential("cmis")
    user_var, pass_var = credential_env_vars("cmis")
    missing = [
        var for var, value in ((user_var, cmis.username), (pass_var, cmis.password)) if not value
    ]
    if missing and require_cmis:
        raise ConfigurationError(
            "required environment variables missing or empty",
            missing_vars=missing,
        )
    aliases = config.required_aliases() if config is not None else (INLINE_CONNECTION_ALIAS,)
    credentials = {"cmis": cmis}
    for alias in aliases:
        credentials[alias] = _read_credential(alias)
    return Secrets(credentials)


def _read_credential(alias: str) -> Credential:
    user_var, pass_var = credential_env_vars(alias)
    return Credential(
        username=os.environ.get(user_var, "").strip(),
        password=os.environ.get(pass_var, "").strip(),
    )
