"""Jerarquía tipada de excepciones de CMCourier.

Todos los errores del proyecto descienden de :class:`CMCourierError`. Los
errores específicos por etapa (``S0`` … ``S5``) descienden de una clase
base de etapa para que los `handler`s puedan filtrar por etapa sin
enumerar cada subclase concreta:

.. code-block:: python

    try:
        ...
    except MappingError as exc:
        # también captura IDRViNotMappedError
        log.error("mapping failed", **exc.context)

Cada subclase concreta declara sus parámetros de contexto nombrados de
forma explícita para que los `type-checker`s detecten errores de tipeo
en los call sites (``IDRViNotMappedError(id_rvi=...)``,
no ``IDRViNotMappedError(idrvi=...)``).

Este módulo forma parte de la capa de dominio (Principio I de la
Constitución): solo Python standard library. No importar módulos de
terceros aquí.
"""

from __future__ import annotations

__all__ = [
    "AssemblyError",
    "CMCourierError",
    "CMISClientError",
    "CMISServerError",
    "ConfigurationError",
    "DefaultValidationFailedError",
    "IDRViNotMappedError",
    "IdentityResolutionError",
    "IndexingError",
    "MappingError",
    "MetadataError",
    "PDFAssemblyFailedError",
    "RVABREPDeletedError",
    "RVABREPDuplicateError",
    "RVABREPNotFoundError",
    "RetriesExhaustedError",
    "SourceFailedError",
    "SourceFileMissingError",
    "TrackingError",
    "TriggerError",
    "UploadError",
]


# ---------------------------------------------------------------------------
# Raíz
# ---------------------------------------------------------------------------


class CMCourierError(Exception):
    """Raíz de la jerarquía de excepciones de CMCourier.

    Acepta un mensaje humano opcional más contexto arbitrario por keyword.
    El dict de contexto se guarda en la instancia y se refleja en
    ``str(exc)`` para que los `logger`s estructurados extraigan los
    campos sin parsear el mensaje.
    """

    def __init__(self, message: str = "", **context: object) -> None:
        self.context: dict[str, object] = dict(context)
        if context:
            ctx_str = ", ".join(f"{k}={v!r}" for k, v in context.items())
            full = f"{message} [{ctx_str}]" if message else ctx_str
        else:
            full = message
        super().__init__(full)


# ---------------------------------------------------------------------------
# Configuración (se lanza al arranque, no está atada a una etapa)
# ---------------------------------------------------------------------------


class ConfigurationError(CMCourierError):
    """La configuración es inválida o le faltan campos requeridos."""


# ---------------------------------------------------------------------------
# Etapa S0 — Adquisición de triggers
# ---------------------------------------------------------------------------


class TriggerError(CMCourierError):
    """Falla de la etapa S0: fuente de `trigger` inalcanzable, malformada o vacía."""


# ---------------------------------------------------------------------------
# Etapa S1 — Indexing de RVABREP
# ---------------------------------------------------------------------------


class IndexingError(CMCourierError):
    """Error base de la etapa S1."""


class RVABREPNotFoundError(IndexingError):
    """No hay filas de RVABREP para el par (shortname, system_id) dado."""

    def __init__(self, *, shortname: str, system_id: str) -> None:
        super().__init__(
            "RVABREP record not found",
            shortname=shortname,
            system_id=system_id,
        )
        self.shortname = shortname
        self.system_id = system_id


class RVABREPDeletedError(IndexingError):
    """Todas las filas de RVABREP que matchean el `trigger` están marcadas como borradas.

    Es decir, ``ABACST`` no vacío en todas. La lanza la etapa S1 cuando
    ``(shortname, system_id)`` devuelve una o más filas pero todas tienen
    un código de borrado no vacío. ``deleted_count`` es la cantidad de
    filas borradas observadas.
    """

    def __init__(self, *, shortname: str, system_id: str, deleted_count: int) -> None:
        super().__init__(
            "Every RVABREP record for the trigger is marked deleted",
            shortname=shortname,
            system_id=system_id,
            deleted_count=deleted_count,
        )
        self.shortname = shortname
        self.system_id = system_id
        self.deleted_count = deleted_count


class RVABREPDuplicateError(IndexingError):
    """Matchean varias filas de RVABREP cuando se esperaba exactamente una."""

    def __init__(self, *, shortname: str, system_id: str, count: int) -> None:
        super().__init__(
            "Multiple RVABREP records matched",
            shortname=shortname,
            system_id=system_id,
            count=count,
        )
        self.shortname = shortname
        self.system_id = system_id
        self.count = count


# ---------------------------------------------------------------------------
# Etapa S2 — Mapeo de clase de documento
# ---------------------------------------------------------------------------


class MappingError(CMCourierError):
    """Error base de la etapa S2."""


class IDRViNotMappedError(MappingError):
    """El ID RVI no tiene entrada en el Modelo Documental."""

    def __init__(self, *, id_rvi: str, txn_num: str | None = None) -> None:
        super().__init__(
            "ID RVI not mapped in Modelo Documental",
            id_rvi=id_rvi,
            txn_num=txn_num,
        )
        self.id_rvi = id_rvi
        self.txn_num = txn_num


class IdentityResolutionError(MappingError):
    """147 REQ-002: un slot de ``identity:`` con ``on_missing: fail`` no resolvió.

    Desciende de :class:`MappingError` a propósito: la identidad se resuelve al
    INICIO de S2 (147 REQ-003), así que un `handler` que ya filtra por etapa la
    clasifica como ``S2_FAILED`` sin enumerar la subclase.

    ``chain`` es el punto de la excepción: la cadena COMPLETA que se intentó,
    fuente por fuente y en orden, incluyendo los saltos previos de los que
    dependía el campo. Sin eso el operador ve "no resolvió el CIF" y no tiene
    forma de saber cuál de los tres saltos se cortó. Nunca lleva VALORES
    resueltos (Principio VIII: son PII); sólo nombres de campo, de fuente y el
    motivo de cada descarte.
    """

    def __init__(
        self,
        *,
        slot: str,
        field_name: str,
        reason: str,
        chain: tuple[str, ...] = (),
    ) -> None:
        rendered = "; ".join(chain) if chain else "<no sources tried>"
        super().__init__(
            f"identity.{slot} could not be resolved from field {field_name!r}: "
            f"{reason} | chain tried: {rendered}",
            slot=slot,
            field_name=field_name,
            reason=reason,
        )
        self.slot = slot
        self.field_name = field_name
        self.reason = reason
        self.chain = chain


# ---------------------------------------------------------------------------
# Etapa S3 — Resolución de metadata
# ---------------------------------------------------------------------------


class MetadataError(CMCourierError):
    """Error base de la etapa S3."""


class SourceFailedError(MetadataError):
    """Una fuente de metadata lanzó excepción o devolvió sin valor, y no hay fallback."""

    def __init__(self, *, field_name: str, source: str) -> None:
        super().__init__(
            "Metadata source failed",
            field_name=field_name,
            source=source,
        )
        self.field_name = field_name
        self.source = source


class DefaultValidationFailedError(MetadataError):
    """Todas las fuentes fallaron Y el valor por default configurado no pasó la validación."""

    def __init__(self, *, field_name: str, default_value: str) -> None:
        super().__init__(
            "Default value did not pass validation",
            field_name=field_name,
            default_value=default_value,
        )
        self.field_name = field_name
        self.default_value = default_value


# ---------------------------------------------------------------------------
# Etapa S4 — Verificación de archivos y ensamblado
# ---------------------------------------------------------------------------


class AssemblyError(CMCourierError):
    """Error base de la etapa S4."""


class SourceFileMissingError(AssemblyError):
    """El archivo fuente esperado no está presente en el file server."""

    def __init__(self, *, file_path: str) -> None:
        super().__init__(
            "Source file missing on file server",
            file_path=file_path,
        )
        self.file_path = file_path

    def __reduce__(self) -> tuple[object, tuple[object, ...]]:
        # 066: reconstrucción pickle-safe. Sin ``__reduce__``, ``pickle``
        # cae al default ``cls(*args)`` que falla porque ``__init__``
        # es keyword-only. Requerido cuando esta excepción cruza el
        # boundary de un ProcessPoolExecutor (`worker` S4 → main).
        return (_reconstruct_source_file_missing, (self.file_path,))


def _reconstruct_source_file_missing(file_path: str) -> SourceFileMissingError:
    return SourceFileMissingError(file_path=file_path)


class PDFAssemblyFailedError(AssemblyError):
    """El tooling subyacente de ensamblado de PDF lanzó excepción."""

    def __init__(self, *, txn_num: str, reason: str) -> None:
        super().__init__(
            "PDF assembly failed",
            txn_num=txn_num,
            reason=reason,
        )
        self.txn_num = txn_num
        self.reason = reason

    def __reduce__(self) -> tuple[object, tuple[object, ...]]:
        # 066: reconstrucción pickle-safe a través de boundaries de proceso.
        return (_reconstruct_pdf_assembly_failed, (self.txn_num, self.reason))


def _reconstruct_pdf_assembly_failed(txn_num: str, reason: str) -> PDFAssemblyFailedError:
    return PDFAssemblyFailedError(txn_num=txn_num, reason=reason)


# ---------------------------------------------------------------------------
# Etapa S5 — Upload
# ---------------------------------------------------------------------------


class UploadError(CMCourierError):
    """Error base de la etapa S5."""


class CMISClientError(UploadError):
    """HTTP 4xx del server `cmis`. NO hay que hacer `retry` — corregir el request."""

    def __init__(self, *, status_code: int, response_body: str = "") -> None:
        super().__init__(
            "CMIS rejected the request (4xx)",
            status_code=status_code,
            response_body=response_body,
        )
        self.status_code = status_code
        self.response_body = response_body


class CMISServerError(UploadError):
    """HTTP 5xx del server `cmis`. `Retry` con `back-off`."""

    def __init__(self, *, status_code: int, response_body: str = "") -> None:
        super().__init__(
            "CMIS server error (5xx)",
            status_code=status_code,
            response_body=response_body,
        )
        self.status_code = status_code
        self.response_body = response_body


class RetriesExhaustedError(UploadError):
    """Presupuesto de `retry` agotado para el upload de un único documento."""

    def __init__(self, *, txn_num: str, attempts: int) -> None:
        super().__init__(
            "Upload retries exhausted",
            txn_num=txn_num,
            attempts=attempts,
        )
        self.txn_num = txn_num
        self.attempts = attempts


# ---------------------------------------------------------------------------
# Etapa S6 — Tracking (transversal; nunca bloquea el `pipeline`)
# ---------------------------------------------------------------------------


class TrackingError(CMCourierError):
    """Falla de escritura en el tracking store. Se loguea, nunca se propaga a los callers."""
