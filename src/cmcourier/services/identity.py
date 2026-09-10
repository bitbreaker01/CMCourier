"""147 REQ-002 — Resolución de la identidad del cliente.

RVABREP no siempre trae la identidad. A veces viene sólo el shortname, a
veces sólo el CIF, a veces ninguno de los dos y lo único disponible es un
número de tarjeta o un afiliado hijo. Este servicio resuelve los tres slots
(``shortname`` / ``cif`` / ``system_id``) con el MISMO motor de
``field_sources`` que la metadata, así que un slot puede llegar después de
tres saltos encadenados (hijo → padre → shortname → CIF).

Un slot no declarado cae a ``trigger.audit_row()``: comportamiento
byte-idéntico al pre-147.

Principio I de la Constitución: importa solo ``cmcourier.domain.*``,
``cmcourier.services.metadata`` y stdlib. Principio VIII: nunca loguear
VALORES resueltos (PII); sólo nombres de slot, de campo y de fuente.
"""

from __future__ import annotations

__all__ = [
    "IdentityConfig",
    "IdentityOutcome",
    "IdentityResolver",
    "IdentitySlotConfig",
    "ResolvedIdentity",
    "ctenum_for",
]

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from cmcourier.domain.exceptions import IdentityResolutionError
from cmcourier.domain.models import RVABREPDocument, Trigger
from cmcourier.services.metadata import FieldResolution, MetadataService

_logger = logging.getLogger(__name__)

OnMissing = Literal["fail", "warn", "default"]

# Los tres slots, en orden fijo. Cada uno se proyecta desde ``audit_row()``
# con esta misma clave cuando no está declarado en el YAML.
_SLOTS: tuple[str, ...] = ("shortname", "cif", "system_id")


@dataclass(frozen=True, slots=True)
class IdentitySlotConfig:
    """Espejo en el dominio del servicio de ``IdentitySlotModel``."""

    field: str
    on_missing: OnMissing = "fail"
    max_digits: int | None = None
    default_value: str | None = None


@dataclass(frozen=True, slots=True)
class IdentityConfig:
    """Los tres slots, todos opcionales. ``IdentityConfig()`` (todo en
    ``None``) es exactamente el comportamiento pre-147."""

    shortname: IdentitySlotConfig | None = None
    cif: IdentitySlotConfig | None = None
    system_id: IdentitySlotConfig | None = None

    def slot(self, name: str) -> IdentitySlotConfig | None:
        return getattr(self, name)  # type: ignore[no-any-return]

    def declared_fields(self) -> tuple[str, ...]:
        """Los nombres canónicos declarados, deduplicados y en orden de slot."""
        names: list[str] = []
        for name in _SLOTS:
            slot = self.slot(name)
            if slot is not None and slot.field not in names:
                names.append(slot.field)
        return tuple(names)


@dataclass(frozen=True, slots=True)
class ResolvedIdentity:
    """147 REQ-003/004: la identidad del cliente para ESTE documento.

    Un único origen para las tres escrituras que hoy pueden discrepar
    (``migration_log``, ``CTECIF``/``CTENUM`` de RVIMGLOG y la clave del
    mapeo). Los tres campos son ``str``: un slot con ``on_missing: warn``
    que no resolvió queda en ``""``, nunca en ``None``.
    """

    shortname: str
    cif: str
    system_id: str


def ctenum_for(
    cif: str,
    slot: IdentitySlotConfig | None,
    *,
    chain: tuple[str, ...] = (),
) -> int | None:
    """147 REQ-004: el valor de ``CTENUM`` para un CIF **sin trigger vivo**.

    Es el gemelo offline de :meth:`IdentityResolver._value_for`, para los
    consumidores que leen el CIF de ``migration_log`` en vez de resolverlo
    (``services.recovery``). La cadena NO se re-corre — no hay documento ni
    trigger que la alimente — pero la política sí se re-aplica: ``max_digits``
    y ``on_missing`` son datos del YAML, no del runtime, y validarlos acá
    cuesta cero red y evita mandar de nuevo el valor que rompió con ``22003``.

    Sin *slot* declarado queda la única regla que nunca debió faltar: un valor
    no numérico devuelve ``None`` (⇒ ``NULL``), NUNCA un ``0`` inventado.
    """
    reason = _ctenum_violation(cif, slot)
    if reason is None:
        return int(cif)
    if slot is None:
        _logger.warning("CTENUM queda en NULL: el CIF de tracking %s", reason)
        return None
    if slot.on_missing == "fail":
        raise IdentityResolutionError(slot="cif", field_name=slot.field, reason=reason, chain=chain)
    if slot.on_missing == "warn":
        _logger.warning(
            "identity slot cif unusable for CTENUM from field=%s (%s)", slot.field, reason
        )
        return None
    default = slot.default_value or ""
    return int(default) if default.isdigit() else None


def _ctenum_violation(cif: str, slot: IdentitySlotConfig | None) -> str | None:
    """Por qué *cif* no sirve como ``CTENUM``, o ``None`` si sirve."""
    if not cif:
        return "no value"
    if not cif.isdigit():
        return f"value is not numeric ({len(cif)} characters)"
    if slot is not None and slot.max_digits is not None and len(cif) > slot.max_digits:
        return f"value is {len(cif)} digits long, max_digits is {slot.max_digits}"
    return None


@dataclass(frozen=True, slots=True)
class IdentityOutcome:
    """147 REQ-003: la identidad MÁS los campos que costó resolverla.

    ``fields`` va indexado por nombre canónico (``BAC_CIF``, …) y lleva sólo
    lo que REALMENTE resolvió. Es la semilla que S2 le pasa a S3
    (``MetadataService.resolve(..., seed=...)``) para que la cadena no se
    recorra dos veces sobre el mismo documento. Un campo que no resolvió no
    entra: sembrar el ``default_value`` de un slot le mentiría a S3 sobre lo
    que la cadena dio.
    """

    identity: ResolvedIdentity
    fields: Mapping[str, str] = field(default_factory=dict)


class IdentityResolver:
    """Resuelve un :class:`ResolvedIdentity` para un par (trigger, documento).

    Aplica ``on_missing`` por slot: ``fail`` levanta
    :class:`~cmcourier.domain.exceptions.IdentityResolutionError` con la
    cadena COMPLETA que se intentó, ``warn`` deja el valor vacío y sigue,
    ``default`` usa ``default_value``.
    """

    def __init__(self, config: IdentityConfig, metadata_service: MetadataService) -> None:
        self._config = config
        self._metadata = metadata_service

    def declares(self, slot_name: str) -> bool:
        """147 REQ-003: ¿el YAML declaró este slot?

        S2 lo pregunta por ``system_id``: con el slot declarado la clave del
        mapping sale de la identidad resuelta; sin declarar sigue saliendo de
        ``trigger_system_id(trigger)``, byte-idéntico al pre-147.
        """
        return self._config.slot(slot_name) is not None

    def resolve(self, trigger: Trigger, document: RVABREPDocument) -> ResolvedIdentity:
        return self.resolve_outcome(trigger, document).identity

    def resolve_outcome(self, trigger: Trigger, document: RVABREPDocument) -> IdentityOutcome:
        """La identidad más la semilla de campos ya resueltos (REQ-003)."""
        audit = trigger.audit_row()
        declared = self._config.declared_fields()
        # Una sola pasada por el motor para los tres slots: si `cif` depende
        # de `shortname`, el salto se comparte en vez de repetirse.
        resolutions = self._metadata.resolve_fields(declared, trigger, document) if declared else {}
        values = {name: self._value_for(name, audit.get(name), resolutions) for name in _SLOTS}
        return IdentityOutcome(
            identity=ResolvedIdentity(
                shortname=values["shortname"],
                cif=values["cif"],
                system_id=values["system_id"],
            ),
            fields={name: res.value for name, res in resolutions.items() if res.value is not None},
        )

    def _value_for(
        self,
        slot_name: str,
        audit_value: str | None,
        resolutions: dict[str, FieldResolution],
    ) -> str:
        slot = self._config.slot(slot_name)
        if slot is None:
            # Slot ausente ⇒ comportamiento pre-147: se lee del trigger.
            return audit_value or ""
        resolution = resolutions[slot.field]
        value = resolution.value or ""
        if value == "":
            return self._apply_on_missing(slot_name, slot, resolution, "no source resolved a value")
        reason = self._magnitude_violation(slot, value)
        if reason is not None:
            return self._apply_on_missing(slot_name, slot, resolution, reason)
        return value

    @staticmethod
    def _magnitude_violation(slot: IdentitySlotConfig, value: str) -> str | None:
        """147 REQ-002: ``max_digits`` valida la MAGNITUD, no el tipo.

        ``int(cif) if cif.isdigit() else 0`` validaba el tipo y nunca el
        largo: un CIF más largo que la precisión de ``CTENUM`` rompía con
        ``22003``, y uno no numérico escribía ``0`` en el log del banco sin
        decir nada. Acá el valor NUNCA se convierte ni se reemplaza en
        silencio — o pasa tal cual, o se aplica ``on_missing``.
        """
        if slot.max_digits is None or len(value) <= slot.max_digits:
            return None
        return f"value is {len(value)} characters long, max_digits is {slot.max_digits}"

    @staticmethod
    def _apply_on_missing(
        slot_name: str,
        slot: IdentitySlotConfig,
        resolution: FieldResolution,
        reason: str,
    ) -> str:
        if slot.on_missing == "fail":
            raise IdentityResolutionError(
                slot=slot_name,
                field_name=slot.field,
                reason=reason,
                chain=resolution.chain,
            )
        if slot.on_missing == "warn":
            _logger.warning(
                "identity slot %s unresolved from field=%s (%s); chain: %s",
                slot_name,
                slot.field,
                reason,
                " | ".join(resolution.chain) or "<no sources tried>",
            )
            return ""
        # `default` — el schema garantiza que default_value no es None.
        return slot.default_value or ""
