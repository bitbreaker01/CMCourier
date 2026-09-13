"""158 — la lógica pura de "probar TODOS los tipos configurados".

Extiende el tiro único de 141: en vez de UN código, sube UN documento por
cada tipo y devuelve el resultado tipo por tipo. Reusa la maquinaria de
141 (`run_practice_upload`, `build_properties`, `document_name`, el
borrado) — acá sólo se agrega lo específico de "todos":

* la **unión de campos distintos** (REQ-002): se le pide al operador un
  valor por campo canónico distinto, no uno por documento;
* el **único PDF marcado** (REQ-003): se genera una vez y se reusa como
  cuerpo de todos los uploads;
* el **loop por tipo** que yielda un resultado por vez (REQ-004), para que
  la pantalla lo pinte en vivo, con un `stop` cooperativo;
* la **limpieza en lote** (REQ-005): borra los objectId que quedaron, y un
  fallo individual no aborta el resto.

Módulo puro: stdlib + dominio + los servicios de mapping / mock / 141. No
toca tracking ni idempotencia — este documento no existe para el pipeline.
"""

from __future__ import annotations

__all__ = [
    "DeleteOutcome",
    "FieldPrompt",
    "PracticeType",
    "TypeOutcome",
    "build_marked_pdf",
    "build_practice_types",
    "delete_practice_objects",
    "distinct_fields",
    "run_practice_all",
]

import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from cmcourier.domain.cm_types import CmTypeManifest
from cmcourier.domain.models import CMMapping, RawResponse
from cmcourier.domain.ports import PracticeUploadPort
from cmcourier.services.mapping import MappingService, mapping_from_entry
from cmcourier.services.mock.synthetic_content import build_text_pdf
from cmcourier.services.mock.synthetic_file import SyntheticFile
from cmcourier.services.practice_upload import PracticeDraft, run_practice_upload

_BODY_MESSAGE_KEYS = ("message", "exception", "error")
_MAX_REASON = 200


@dataclass(frozen=True, slots=True)
class PracticeType:
    """Un tipo a probar. Envuelve un :class:`CMMapping` — de ahí salen el
    ``id_corto``, los campos canónicos, los wire ids, el object type y la
    carpeta — así se reusan ``build_properties`` y ``run_practice_upload``
    tal cual. ``mapped`` distingue los referenciados por el CSV de los que
    entran sólo con el toggle "incluir tipos no mapeados"."""

    mapping: CMMapping
    mapped: bool

    @property
    def id_corto(self) -> str:
        return self.mapping.id_corto

    @property
    def display_name(self) -> str:
        return self.mapping.clase_name

    @property
    def fields(self) -> tuple[str, ...]:
        return self.mapping.required_metadata_fields


@dataclass(frozen=True, slots=True)
class FieldPrompt:
    """Un campo canónico distinto y en cuántos tipos se usa (REQ-002)."""

    name: str
    type_count: int


@dataclass(frozen=True, slots=True)
class TypeOutcome:
    """El resultado de subir UN tipo (REQ-004)."""

    id_corto: str
    display_name: str
    ok: bool
    reason: str
    object_id: str | None = None
    name: str = ""


@dataclass(frozen=True, slots=True)
class DeleteOutcome:
    """El resultado de borrar UN objectId de la prueba (REQ-005)."""

    object_id: str
    ok: bool
    reason: str


def build_practice_types(
    mapping_service: MappingService,
    manifest: CmTypeManifest | None = None,
    *,
    include_unmapped: bool = False,
) -> tuple[PracticeType, ...]:
    """158 REQ-001: los tipos a probar.

    Por defecto, un tipo por cada ``IDCM`` distinto que el mapping resuelve
    — lo que una migración real tocaría. Varias filas ``IDRVI`` pueden
    apuntar al mismo ``IDCM``: gana la primera. Se itera por ``cm_codes``
    (índice por ``IDCM``), no por ``get_all`` (índice por ``IDRVI``): un
    ``IDCM`` cuyo ``IDRVI`` colisionó con otra fila sigue siendo un tipo a
    probar y ``get_all`` no lo vería. Con *include_unmapped* y un manifest
    presente, se suman los tipos del manifest que el mapping NO referencia
    y que tienen al menos una propiedad ``usar`` (probar tipos sin
    metadatos que nadie migra es basura en CM).
    """
    types: list[PracticeType] = []
    seen: set[str] = set()
    for code in mapping_service.cm_codes():
        rows = mapping_service.get_by_cm_code(code)
        if not rows:
            continue
        seen.add(code)
        types.append(PracticeType(mapping=rows[0], mapped=True))
    if include_unmapped and manifest is not None:
        for code, entry in manifest.types.items():
            if code in seen or not entry.usable_properties():
                continue
            seen.add(code)
            types.append(PracticeType(mapping=mapping_from_entry(entry), mapped=False))
    return tuple(types)


def distinct_fields(types: Sequence[PracticeType]) -> tuple[FieldPrompt, ...]:
    """158 REQ-002: la unión de los campos canónicos de todos los tipos.

    Un campo compartido por varios tipos aparece UNA vez, con la cuenta de
    en cuántos tipos se usa. El orden es el de primera aparición. Un campo
    repetido dentro de un mismo tipo (dos wire ids que canonizan igual) no
    cuenta doble a ese tipo.
    """
    order: list[str] = []
    counts: dict[str, int] = {}
    for ptype in types:
        for name in dict.fromkeys(ptype.fields):
            if name not in counts:
                order.append(name)
                counts[name] = 0
            counts[name] += 1
    return tuple(FieldPrompt(name=name, type_count=counts[name]) for name in order)


def build_marked_pdf(
    *, operator: str, station: str, environment: str, now: datetime
) -> SyntheticFile:
    """158 REQ-003: el ÚNICO PDF sintético, con la marca de la prueba
    LEGIBLE adentro. Se genera una vez y se reusa como cuerpo de cada
    upload — el tipo puntual va en el nombre y los metadatos, no acá."""
    lines = [
        "PRUEBA DE CARGA — CMCourier",
        "No es un documento de migración real.",
        f"Generado: {now.isoformat(timespec='seconds')}",
        f"Operador:  {operator}",
        f"Estación:  {station}",
        f"Entorno:   {environment}",
    ]
    return SyntheticFile(
        content=build_text_pdf(lines), mime_type="application/pdf", extension=".pdf"
    )


def run_practice_all(
    types: Sequence[PracticeType],
    values: Mapping[str, str],
    uploader: PracticeUploadPort,
    *,
    workdir: Path,
    now: datetime,
    content: SyntheticFile,
    should_stop: Callable[[], bool] | None = None,
) -> Iterator[TypeOutcome]:
    """158 REQ-004: sube UN documento por tipo y yielda el resultado de
    cada uno a medida que responde — los MISMOS bytes (*content*) para
    todos. Un *should_stop* que devuelve ``True`` corta cooperativamente
    antes del próximo upload. Cada valor de *values* se reusa en los tipos
    que usan ese campo (``build_properties`` filtra el subconjunto)."""
    for ptype in types:
        if should_stop is not None and should_stop():
            return
        yield _upload_one(ptype, values, uploader, workdir=workdir, now=now, content=content)


def _upload_one(
    ptype: PracticeType,
    values: Mapping[str, str],
    uploader: PracticeUploadPort,
    *,
    workdir: Path,
    now: datetime,
    content: SyntheticFile,
) -> TypeOutcome:
    draft = PracticeDraft(cm_code=ptype.id_corto, mapping=ptype.mapping, values=dict(values))
    try:
        result = run_practice_upload(draft, uploader, workdir=workdir, now=now, content=content)
    except Exception as exc:  # noqa: BLE001 — un tipo que revienta no corta el resto
        return TypeOutcome(
            id_corto=ptype.id_corto,
            display_name=ptype.display_name,
            ok=False,
            reason=f"{type(exc).__name__}: {exc}",
        )
    response = result.response
    return TypeOutcome(
        id_corto=ptype.id_corto,
        display_name=ptype.display_name,
        ok=response.ok,
        reason=_reason(response),
        object_id=response.object_id,
        name=result.name,
    )


def delete_practice_objects(
    object_ids: Sequence[str],
    uploader: PracticeUploadPort,
    *,
    should_stop: Callable[[], bool] | None = None,
) -> Iterator[DeleteOutcome]:
    """158 REQ-005: borra en lote los objectId que dejó la prueba. Un fallo
    individual (o una excepción de red) NO aborta el resto: se yielda
    ``ok=False`` y se sigue con el próximo, para que la pantalla liste los
    que quedaron para limpiar a mano."""
    for object_id in object_ids:
        if should_stop is not None and should_stop():
            return
        yield _delete_one(object_id, uploader)


def _delete_one(object_id: str, uploader: PracticeUploadPort) -> DeleteOutcome:
    try:
        response = uploader.delete_object(object_id)
    except Exception as exc:  # noqa: BLE001 — un borrado que revienta no corta el resto
        return DeleteOutcome(object_id=object_id, ok=False, reason=f"{type(exc).__name__}: {exc}")
    return DeleteOutcome(object_id=object_id, ok=response.ok, reason=_reason(response))


def _reason(response: RawResponse) -> str:
    """La razón CRUDA de CM: ``status reason`` y, para las fallas, el
    mensaje del body — exactamente el dato que permite arreglar la config
    (igual que el tiro único de 141)."""
    head = f"{response.status_code} {response.reason}".strip()
    if response.ok:
        return head
    body = _body_message(response.body)
    return f"{head} · {body}" if body else head


def _body_message(body: str) -> str:
    """Un mensaje corto del cuerpo del error: el campo ``message`` del JSON
    de CMIS si parsea, si no la primera línea cruda."""
    text = body.strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return text.splitlines()[0][:_MAX_REASON]
    if isinstance(data, dict):
        for key in _BODY_MESSAGE_KEYS:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:_MAX_REASON]
    return text.splitlines()[0][:_MAX_REASON]
