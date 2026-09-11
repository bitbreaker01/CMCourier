"""Capa de dominio — Python puro, cero dependencias externas (Principio I de la Constitución).

Contiene los modelos de dominio (`dataclasses`), los `port`s (interfaces
abstractas) y la jerarquía tipada de excepciones. Sin I/O, sin imports de
terceros — incluso ``pydantic`` está prohibido aquí.

Los nombres públicos de ``models``, ``ports`` y ``exceptions`` se
re-exportan aquí para que los callers escriban
``from cmcourier.domain import IDataSource`` sin tener que saber en qué
submódulo vive cada nombre.
"""

from __future__ import annotations

from cmcourier.domain.exceptions import (
    AssemblyError,
    CMCourierError,
    CMISClientError,
    CMISServerError,
    ConfigurationError,
    DefaultValidationFailedError,
    IdentityResolutionError,
    IDRViNotMappedError,
    IndexingError,
    MappingError,
    MetadataError,
    PDFAssemblyFailedError,
    RetriesExhaustedError,
    RVABREPDeletedError,
    RVABREPDuplicateError,
    RVABREPNotFoundError,
    SourceFailedError,
    SourceFileMissingError,
    TrackingError,
    TriggerError,
    UploadError,
)
from cmcourier.domain.models import (
    CMMapping,
    ExcludedTrigger,
    MigrationRecord,
    ReasonBucket,
    ReasonCode,
    ResolvedMetadata,
    RVABREPDocument,
    StagedFile,
    StageStatus,
    TriggerRecord,
    compute_cm_folder,
    compute_cm_object_type,
    is_pdf_filename,
    parse_cymmdd,
)
from cmcourier.domain.ports import (
    IAssembler,
    IDataSource,
    ITrackingStore,
    IUploader,
    S0Strategy,
)

__all__ = [
    "AssemblyError",
    "CMCourierError",
    "CMISClientError",
    "CMISServerError",
    "CMMapping",
    "ConfigurationError",
    "DefaultValidationFailedError",
    "ExcludedTrigger",
    "IAssembler",
    "IDRViNotMappedError",
    "IdentityResolutionError",
    "IDataSource",
    "ITrackingStore",
    "IUploader",
    "IndexingError",
    "MappingError",
    "MetadataError",
    "MigrationRecord",
    "PDFAssemblyFailedError",
    "ReasonBucket",
    "ReasonCode",
    "RVABREPDeletedError",
    "RVABREPDocument",
    "RVABREPDuplicateError",
    "RVABREPNotFoundError",
    "ResolvedMetadata",
    "RetriesExhaustedError",
    "S0Strategy",
    "SourceFailedError",
    "SourceFileMissingError",
    "StageStatus",
    "StagedFile",
    "TrackingError",
    "TriggerError",
    "TriggerRecord",
    "UploadError",
    "compute_cm_folder",
    "compute_cm_object_type",
    "is_pdf_filename",
    "parse_cymmdd",
]
