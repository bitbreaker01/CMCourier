"""150 — Elegibilidad: sólo los clientes con producto activo.

Directiva de negocio: Content Manager no tiene espacio para todo RVABREP,
así que sólo se migran los documentos de clientes **con producto activo** —
alguna cuenta, algún certificado de depósito, alguna tarjeta o algún
afiliado activo. El banco produce un CSV con el ``Shortname`` y el ``CIF``
de esos clientes, y este servicio lo consulta.

Lo que NO se hace es filtrar a oscuras (que es lo que el sistema hacía antes
del censo de 148): un documento que no se migra por esta directiva aparece
en el censo diciéndolo con todas las letras, como
``ReasonCode.CLIENT_NOT_ACTIVE`` en el balde ``EXCLUIDO``.

Dos responsabilidades, y la primera es la que justifica la spec:

1. **El preflight (REQ-002).** Antes del primer documento se verifica que la
   fuente abra, tenga las columnas declaradas y tenga al menos una fila. Si
   algo falla, la corrida **aborta**. Una lista rota no significa "nadie está
   activo": significa que no podemos responder la pregunta.
2. **El chequeo por documento (REQ-001/003).** ``match_any``: basta con que
   UNO de los criterios matchee. Corre en S2, inmediatamente después de
   resolver la identidad — para saber si el cliente está activo hay que saber
   primero quién es el cliente.

Consecuencia que se documenta explícitamente (REQ-003): **este filtro ahorra
espacio en Content Manager, no tiempo de proceso.** Un documento de un
cliente inactivo paga igual toda la cadena de resolución (afiliado hijo →
padre → shortname → CIF) antes de poder descartarse. Lo hace tolerable el
memo por corrida: el primer documento de un cliente inactivo paga los saltos,
los demás del mismo cliente van gratis.

Principio I de la Constitución: importa solo ``cmcourier.domain.*``,
``cmcourier.services.metadata`` y stdlib. Principio VIII: nunca loguear
VALORES (PII); sólo nombres de campo, de columna y de fuente.
"""

from __future__ import annotations

__all__ = [
    "EligibilityConfig",
    "EligibilityMatch",
    "EligibilityService",
    "EligibilitySnapshot",
]

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from cmcourier.domain.exceptions import (
    ConfigurationError,
    EligibilityListError,
)
from cmcourier.domain.models import RVABREPDocument, Trigger
from cmcourier.domain.ports import IDataSource
from cmcourier.services.metadata import MetadataService

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EligibilityMatch:
    """Espejo en el dominio del servicio de ``EligibilityMatchModel``."""

    field: str
    column: str


@dataclass(frozen=True, slots=True)
class EligibilityConfig:
    """150 REQ-001: la lista de activos y por qué columnas se la consulta.

    ``source_path`` no participa de la decisión: es el dato de AUDITORÍA
    (REQ-005) — la ruta concreta de la foto que se usó, para que dentro de
    seis meses alguien pueda responder *"¿activo según qué lista?"*.
    """

    enabled: bool = False
    source: str = ""
    match_any: tuple[EligibilityMatch, ...] = ()
    source_path: str = ""


@dataclass(frozen=True, slots=True)
class EligibilitySnapshot:
    """150 REQ-005: qué lista se usó, tal como quedó al arrancar la corrida.

    ``modified_at`` queda en ``""`` cuando la fuente no es un archivo (una
    tabla AS400 no tiene fecha de modificación que leer desde acá).
    """

    source: str
    path: str
    modified_at: str
    row_count: int


class EligibilityService:
    """Responde "¿este cliente tiene producto activo?" contra la lista.

    El servicio no conoce documentos ni etapas: recibe lo que la resolución
    de identidad ya resolvió y, para lo que falte, pide el valor al MISMO
    motor de ``field_sources`` (así un ``match_any.field`` no está obligado a
    ser además un slot de ``identity:``).
    """

    def __init__(self, config: EligibilityConfig, metadata_service: MetadataService) -> None:
        self._config = config
        self._metadata = metadata_service

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    # --- REQ-002: el preflight ------------------------------------------

    def verify_list(self) -> EligibilitySnapshot:
        """Verifica que la lista se pueda consultar. Aborta si no.

        Levanta :class:`~cmcourier.domain.exceptions.EligibilityListError`
        con la fuente y el problema concreto en el mensaje. El orden del
        chequeo es *abre → tiene filas → tiene las columnas*: la cantidad de
        filas se sabe sin materializar nada, y la única forma de leer las
        columnas de una fuente genérica es mirando una fila, que una lista
        vacía no tiene.
        """
        source_type = self._config.source
        try:
            source = self._metadata.lookup_source(source_type)
        except ConfigurationError as exc:
            raise self._broken(f"it cannot be opened: {exc}") from exc
        try:
            row_count = source.count()
        except Exception as exc:  # noqa: BLE001 — cualquier adapter, mismo veredicto
            raise self._broken(f"it cannot be read: {type(exc).__name__}: {exc}") from exc
        if row_count == 0:
            raise self._broken(
                "it has zero rows. An empty list does NOT mean 'nobody is active': "
                "it means the question cannot be answered. Fix the list or turn "
                "`eligibility.enabled` off"
            )
        self._require_columns(source)
        _logger.info(
            "eligibility: list verified",
            extra={"source": source_type, "rows": row_count},
        )
        return EligibilitySnapshot(
            source=source_type,
            path=self._config.source_path,
            modified_at=self._modified_at(),
            row_count=row_count,
        )

    def _require_columns(self, source: IDataSource) -> None:
        """Cada ``match_any[].column`` tiene que existir en la fuente."""
        try:
            first = next(iter(source.get_all()), None)
        except Exception as exc:  # noqa: BLE001 — cualquier adapter, mismo veredicto
            raise self._broken(f"it cannot be read: {type(exc).__name__}: {exc}") from exc
        columns = set(first or {})
        missing = [m.column for m in self._config.match_any if m.column not in columns]
        if missing:
            raise self._broken(
                f"it does not have the declared column(s) {', '.join(sorted(missing))} "
                f"(it has: {', '.join(sorted(columns)) or 'none'})"
            )

    def _broken(self, problem: str) -> EligibilityListError:
        """El error del preflight, siempre con la fuente y el problema."""
        return EligibilityListError(
            f"the eligibility list {self._config.source!r} cannot be used: {problem}. "
            "The run is aborted before the first document, on purpose: carrying on "
            "would emit a tidy census claiming every document was excluded for an "
            "inactive client, and that report would be entirely false",
            source=self._config.source,
            path=self._config.source_path,
        )

    def _modified_at(self) -> str:
        path = Path(self._config.source_path) if self._config.source_path else None
        if path is None or not path.is_file():
            return ""
        return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")  # noqa: DTZ006

    # --- REQ-001/003: el chequeo por documento ---------------------------

    def is_active(
        self,
        trigger: Trigger,
        document: RVABREPDocument,
        resolved: Mapping[str, str],
    ) -> bool:
        """``True`` si ALGUNO de los ``match_any`` encuentra al cliente.

        *resolved* son los campos que la resolución de identidad ya resolvió
        (147 REQ-003). Lo que falte se resuelve con el mismo motor y queda
        memoizado adentro del :class:`MetadataService`, así que reclamar un
        campo que la identidad no necesitaba cuesta una vez por cliente y no
        una vez por documento.

        Un valor vacío NUNCA cuenta como match: la lista real trae filas con
        el ``Shortname`` en blanco, y matchear contra un vacío marcaría como
        activo a cualquier documento cuya identidad no resolvió ese campo.
        """
        for match in self._config.match_any:
            value = resolved.get(match.field) or self._resolve(match.field, trigger, document)
            if not value:
                continue
            if self._metadata.lookup_exists(self._config.source, match.column, value):
                return True
        return False

    def _resolve(self, field: str, trigger: Trigger, document: RVABREPDocument) -> str:
        """El valor de *field* por la cadena de ``field_sources``, o ``""``."""
        resolution = self._metadata.resolve_fields([field], trigger, document).get(field)
        return "" if resolution is None or resolution.value is None else resolution.value
