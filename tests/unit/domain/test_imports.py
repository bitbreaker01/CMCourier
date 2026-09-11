"""Verifica que cada nombre público sea importable directamente desde
``cmcourier.domain``.

Un lector debería poder escribir ``from cmcourier.domain import
IDataSource`` sin saber si el símbolo vive en ``models``, ``ports`` o
``exceptions``. Este test resguarda contra `drift` de re-exports.
"""

from __future__ import annotations


def test_models_importable() -> None:
    from cmcourier.domain import (
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

    # Solo los tocamos para satisfacer linters y demostrar que resolvieron.
    assert all(
        x is not None
        for x in (
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
    )


def test_ports_importable() -> None:
    from cmcourier.domain import (
        IAssembler,
        IDataSource,
        ITrackingStore,
        IUploader,
        S0Strategy,
    )

    ports = (IAssembler, IDataSource, ITrackingStore, IUploader, S0Strategy)
    assert all(x is not None for x in ports)


def test_exceptions_importable() -> None:
    from cmcourier.domain import (
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

    assert all(
        x is not None
        for x in (
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
    )


def test_dunder_all_is_complete() -> None:
    """``__all__`` debe listar cada nombre re-exportado y nada más."""
    import cmcourier.domain as domain

    expected = {
        # Excepciones
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
        # Modelos + helpers
        "CMMapping",
        "ExcludedTrigger",  # 148 REQ-001
        "MigrationRecord",
        "ReasonBucket",
        "ReasonCode",
        "ResolvedMetadata",
        "RVABREPDocument",
        "StageStatus",
        "StagedFile",
        "TriggerRecord",
        "compute_cm_folder",
        "compute_cm_object_type",
        "is_pdf_filename",
        "parse_cymmdd",
        # Puertos
        "IAssembler",
        "IDataSource",
        "ITrackingStore",
        "IUploader",
        "S0Strategy",
    }
    assert set(domain.__all__) == expected
