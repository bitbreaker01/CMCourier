"""141 REQ-003/REQ-004 — el tiro de prueba: un documento sintético a un
código de Content Manager, con la respuesta CRUDA del servidor.

El operador quiere confirmar, ANTES de una corrida de miles de
documentos, que un código CM está mapeado, que los metadatos que exige
son los que él cree, y — sobre todo — ver qué contesta el servidor
cuando algo falla. El pipeline no sirve para eso: trunca el cuerpo del
error a 1024 caracteres y reintenta.

Este módulo es puro (stdlib + dominio + el generador sintético). El
puerto :class:`~cmcourier.domain.ports.PracticeUploadPort` y la
:class:`~cmcourier.domain.models.RawResponse` viven en el dominio, así
que el adapter CMIS no depende de este módulo (ni arrastra Pillow); acá
se re-exportan por comodidad de la consola y los tests.
"""

from __future__ import annotations

__all__ = [
    "PracticeDraft",
    "PracticeResult",
    "PracticeUploadPort",
    "RawResponse",
    "build_properties",
    "document_name",
    "run_practice_upload",
    "validate_values",
]

import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from cmcourier.domain.models import CMMapping, RawResponse, StagedFile
from cmcourier.domain.ports import PracticeUploadPort
from cmcourier.services.mock.synthetic_file import (
    EXTENSIONS,
    SyntheticFormat,
    build_synthetic_file,
)


@dataclass(frozen=True, slots=True)
class PracticeDraft:
    """Lo que el operador cargó en la pantalla ``[0]``."""

    cm_code: str
    mapping: CMMapping
    values: dict[str, str] = field(default_factory=dict)
    fmt: SyntheticFormat = "pdf"
    size_bytes: int = 200 * 1024

    @property
    def folder(self) -> str:
        """La carpeta destino, resuelta como el `pipeline`."""
        return self.mapping.cmis_folder or self.mapping.cm_folder

    @property
    def object_type(self) -> str:
        """El ``cmis:objectTypeId``, resuelto como el `pipeline`."""
        return self.mapping.cmis_type or self.mapping.cm_object_type


@dataclass(frozen=True, slots=True)
class PracticeResult:
    """El intento completo: qué se mandó y qué contestó el servidor."""

    name: str
    folder: str
    object_type: str
    size_bytes: int
    size_note: str
    response: RawResponse


def validate_values(mapping: CMMapping, values: Mapping[str, str]) -> dict[str, str]:
    """141 REQ-004: errores por campo requerido; dict vacío si está todo bien.

    Un requerido vacío (o sólo whitespace) da ``"requerido"``. Un
    ``Metadato`` que el mapping declara requerido pero no tiene
    ``CMISPropertyId`` cuando el catálogo existe da ``"sin CMISPropertyId
    en el mapping"``: mandarlo con el nombre `friendly` lo rebota el
    servidor con un error mucho más oscuro.

    141 antagonista M2: un catálogo ``{}`` (dict vacío) cuenta como "sin
    catálogo" — igual que ``None`` — consistente con :func:`build_properties`,
    que también trata ambos como "sin catálogo, dejar el nombre `friendly`".

    141 antagonista M3: dos ``Metadato`` requeridos que comparten el mismo
    ``CMISPropertyId`` son un error de CONFIGURACIÓN del mapping, no del
    operador — el servidor recibiría una sola property con el último
    valor pisando en silencio al otro. Se marca en AMBOS campos y tiene
    prioridad sobre "requerido" / "sin CMISPropertyId".
    """
    duplicates = _duplicate_property_errors(mapping)
    errors: dict[str, str] = {}
    catalog = mapping.cmis_property_ids
    for name in mapping.required_metadata_fields:
        if name in duplicates:
            errors[name] = duplicates[name]
        elif not values.get(name, "").strip():
            errors[name] = "requerido"
        elif catalog and not catalog.get(name):
            errors[name] = "sin CMISPropertyId en el mapping"
    return errors


def _duplicate_property_errors(mapping: CMMapping) -> dict[str, str]:
    """141 antagonista M3: agrupa los campos requeridos por ``CMISPropertyId``
    y arma el mensaje de error para cada campo cuyo id colisiona con otro."""
    catalog = mapping.cmis_property_ids
    if not catalog:
        return {}
    names_by_property_id: dict[str, list[str]] = {}
    for name in mapping.required_metadata_fields:
        cmis_id = catalog.get(name)
        if cmis_id:
            names_by_property_id.setdefault(cmis_id, []).append(name)
    errors: dict[str, str] = {}
    for names in names_by_property_id.values():
        if len(names) < 2:
            continue
        for name in names:
            others = ", ".join(other for other in names if other != name)
            errors[name] = f"CMISPropertyId duplicado con {others}"
    return errors


def build_properties(mapping: CMMapping, values: Mapping[str, str]) -> dict[str, str]:
    """141 REQ-004: traduce ``Metadato`` → ``CMISPropertyId``.

    Sin catálogo (modo consolidado) se deja el nombre tal cual, igual
    que hace :class:`~cmcourier.services.metadata.MetadataService`.
    """
    catalog = mapping.cmis_property_ids
    properties: dict[str, str] = {}
    for name in mapping.required_metadata_fields:
        value = values.get(name, "").strip()
        cmis_id = catalog.get(name) if catalog else None
        properties[cmis_id or name] = value
    return properties


def document_name(cm_code: str, fmt: SyntheticFormat, now: datetime) -> str:
    """141 REQ-004: ``PRUEBA-<IDCM>-<YYYYmmdd-HHMMSS>.<ext>``.

    El prefijo ``PRUEBA-`` es a propósito: en el CM se distingue de un
    documento migrado de verdad de un vistazo.
    """
    return f"PRUEBA-{cm_code}-{now:%Y%m%d-%H%M%S}{EXTENSIONS[fmt]}"


def run_practice_upload(
    draft: PracticeDraft,
    uploader: PracticeUploadPort,
    *,
    workdir: Path,
    now: datetime,
) -> PracticeResult:
    """141 REQ-004: genera el archivo, lo sube y lo borra del disco.

    141 antagonista I4 + M4: cada llamada obtiene su PROPIO directorio
    temporal (``tempfile.mkdtemp``) en lugar de escribir directo en
    ``workdir`` — dos operadores (o dos tiros seguidos) con el mismo
    ``now`` generan el mismo ``document_name`` y se pisarían el archivo.
    ``write_bytes`` vive DENTRO del ``try``: si revienta (disco lleno,
    permisos), el ``finally`` borra igual el directorio recién creado en
    lugar de dejarlo huérfano. No pasa por tracking ni por idempotencia —
    este documento no existe para el `pipeline`.
    """
    name = document_name(draft.cm_code, draft.fmt, now)
    workdir.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix="practice-", dir=workdir))
    try:
        synthetic = build_synthetic_file(draft.fmt, draft.size_bytes, name)
        path = tmp_dir / name
        path.write_bytes(synthetic.content)
        response = uploader.upload_raw(
            StagedFile(path=path, size_bytes=len(synthetic.content), page_count=1),
            draft.folder,
            draft.object_type,
            name,
            synthetic.mime_type,
            build_properties(draft.mapping, draft.values),
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return PracticeResult(
        name=name,
        folder=draft.folder,
        object_type=draft.object_type,
        size_bytes=len(synthetic.content),
        size_note=synthetic.size_note,
        response=response,
    )
