"""Servicio de mapping: cache in-memory + lookup sobre el Modelo Documental.

Al construirse carga cada fila desde cualquier :class:`IDataSource`
y arma un dict ``(sistema, id_rvi) -> CMMapping`` para lookup O(1). Las
llamadas posteriores a ``get_mapping`` pegan en la cache. El servicio no
hace I/O después de la construcción.

145 REQ-001: hay TRES modos de carga. El consolidado (un CSV con todo
inline) y el split (``MapeoRVI_CM`` + ``MetadatosCM``, deprecado)
escriben siempre bajo el sistema comodín ``""``; el modo manifest
(``MapeoRVI_CM`` reducido a ``IDSistema,IDRVI,IDCM`` + el
:class:`CmTypeManifest` que bajó ``types discover``) es el único que
usa la clave completa.

El stage S2 (Document Class Mapping) de cada `pipeline` depende de
este servicio, igual que el chequeo de completitud de mapping del
comando ``doctor``.

Principio I de la Constitución: importa solo ``cmcourier.domain.*`` y
la biblioteca estándar de Python. Sin imports de terceros ni de
adapters.
"""

from __future__ import annotations

__all__ = ["MappingColumnsConfig", "MappingService"]

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from types import MappingProxyType

from cmcourier.domain.cm_types import CmTypeEntry, CmTypeManifest, canonical_name
from cmcourier.domain.exceptions import ConfigurationError, IDRViNotMappedError
from cmcourier.domain.models import CMMapping
from cmcourier.domain.ports import IDataSource

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MappingColumnsConfig:
    """Overrides de nombres de columna tanto para el modo consolidado
    como para el modo split.

    El modo consolidado (familia ``col_*`` sin prefijo ``rvi_cm`` ni
    ``metadatos``) coincide con el layout legacy de las fixtures de
    tests. El modo split (035, familias ``col_rvi_cm_*`` y
    ``col_metadatos_*``) coincide con el par de CSV de producción
    del banco.
    """

    col_clase_id: str = "ID CLASE DOCUMENTAL"
    col_id_rvi: str = "ID RVI"
    col_id_corto: str = "ID Corto"
    col_clase_name: str = "CLASE DOCUMENTAL"
    col_metadata_list: str = "METADATOS"
    col_cmis_type: str = "CMISType"
    col_rvi_cm_id_rvi: str = "IDRVI"
    col_rvi_cm_id_cm: str = "IDCM"
    # 145 REQ-001: el sistema de origen del código RVI. Opcional en modo
    # manifest — columna ausente ≡ todas las filas al comodín.
    col_rvi_cm_id_sistema: str = "IDSistema"
    col_rvi_cm_clase_id: str = "IDClaseDocumental"
    col_rvi_cm_cmis_type: str = "CMISType"
    col_rvi_cm_cmis_folder: str = "CMISFolder"
    col_metadatos_id_corto: str = "IDCorto"
    col_metadatos_metadata: str = "Metadato"
    col_metadatos_required: str = "Requerido"
    col_metadatos_cmis_property_id: str = "CMISPropertyId"
    required_marker: str = "Yes"

    def required_columns(self) -> tuple[str, ...]:
        """Columnas que el `loader` en modo consolidado tiene que
        encontrar en la fuente.

        ``col_cmis_type`` intencionalmente NO es requerida: se lee
        cuando está presente y por defecto queda en "" cuando no
        (la fixture legacy no tiene columna ``CMISType``).
        """
        return (
            self.col_clase_id,
            self.col_id_rvi,
            self.col_id_corto,
            self.col_clase_name,
            self.col_metadata_list,
        )

    def required_columns_rvi_cm(self) -> tuple[str, ...]:
        """Columnas que el `loader` en modo split tiene que encontrar
        en ``MapeoRVI_CM``."""
        return (
            self.col_rvi_cm_id_rvi,
            self.col_rvi_cm_id_cm,
            self.col_rvi_cm_clase_id,
        )

    def required_columns_manifest(self) -> tuple[str, ...]:
        """145 REQ-001: lo único que el modo manifest exige de ``MapeoRVI_CM``.

        ``IDSistema`` NO entra: la columna ausente equivale a todas las
        filas vacías, o sea todas al comodín.
        """
        return (
            self.col_rvi_cm_id_rvi,
            self.col_rvi_cm_id_cm,
        )

    def required_columns_metadatos(self) -> tuple[str, ...]:
        """Columnas que el `loader` en modo split tiene que encontrar
        en ``MetadatosCM``."""
        return (
            self.col_metadatos_id_corto,
            self.col_metadatos_metadata,
            self.col_metadatos_required,
        )


#: 145 REQ-001: el sistema "cualquiera". Una fila sin ``IDSistema`` cae acá.
_WILDCARD_SYSTEM = ""


def _is_blank(value: object) -> bool:
    """Devuelve ``True`` si *value* es ``None``, vacío o solo
    whitespace."""
    return value is None or (isinstance(value, str) and not value.strip())


def _norm_system(value: object) -> str:
    """145 REQ-001: normaliza un sistema a la forma que usa la clave.

    ``strip()`` + ``lower()``: la comparación es case-insensitive y no
    le importan los espacios del CSV. Vacío / ``None`` ⇒ comodín.
    """
    if value is None:
        return _WILDCARD_SYSTEM
    return str(value).strip().lower()


def _mapping_from_entry(entry: CmTypeEntry, id_rvi: str) -> CMMapping:
    """145 REQ-001: proyecta una entrada del manifest a un :class:`CMMapping`.

    Todo lo que antes salía de ``MetadatosCM.csv`` sale ahora del
    servidor: el tipo, la carpeta, y las propiedades que el operador
    marcó ``usar`` — con su nombre canónico como clave (la misma que usa
    ``metadata.field_sources``) y su id de wire como valor.
    """
    props = entry.usable_properties()
    wire_ids = {canonical_name(p.id): p.id for p in props}
    return CMMapping(
        clase_id=entry.local_name,
        id_rvi=id_rvi,
        id_corto=entry.id_corto,
        clase_name=entry.display_name,
        required_metadata_fields=tuple(canonical_name(p.id) for p in props),
        cmis_type=entry.type_id,
        cmis_folder=entry.folder or None,
        cmis_property_ids=MappingProxyType(wire_ids) if wire_ids else None,
    )


def _parse_metadata_list(raw: object) -> tuple[str, ...]:
    """Parsea la celda ``METADATOS`` en una tupla de campos
    trimeados y no vacíos."""
    if _is_blank(raw) or not isinstance(raw, str):
        return ()
    parts = (p.strip() for p in raw.split(","))
    return tuple(p for p in parts if p)


_TRUTHY_REQUIRED_SYNONYMS = frozenset({"yes", "sí", "si", "true", "1", "y", "s"})


def _is_required(value: object, custom_marker: str) -> bool:
    """Decide si una celda ``MetadatosCM.Requerido`` cuenta como
    requerida.

    Cualquier valor que matchee ``custom_marker`` (case-insensitive,
    sin whitespace, tolerante a acentos en el path por defecto "Yes")
    o alguno de los sinónimos `truthy` comunes ("yes", "sí", "true",
    "1") cuenta. Las celdas vacías y "no" / "false" / "0" descartan
    el campo.
    """
    if value is None:
        return False
    text = str(value).strip().lower()
    if not text:
        return False
    if text == custom_marker.strip().lower():
        return True
    return text in _TRUTHY_REQUIRED_SYNONYMS


class MappingService:
    """Cache in-memory + lookup sobre el Modelo Documental.

    La construcción itera toda la fuente una vez, valida las
    columnas requeridas y arma un dict indexado por ``(sistema,
    id_rvi)`` (145 REQ-001; el sistema es ``""`` fuera del modo
    manifest). La primera ocurrencia de una clave duplicada gana; las
    posteriores se descartan con una entrada de log en ``WARNING``.
    Las filas con ``id_rvi`` vacío se saltean silenciosamente, con
    una entrada de log en ``INFO`` que resume la cuenta.

    El servicio no es dueño del ciclo de vida de la fuente; el
    caller la cierra con ``close()``.
    """

    def __init__(
        self,
        source: IDataSource,
        columns: MappingColumnsConfig | None = None,
        metadata_source: IDataSource | None = None,
        type_manifest: CmTypeManifest | None = None,
    ) -> None:
        if metadata_source is not None and type_manifest is not None:
            raise ConfigurationError(
                "MappingService: `metadata_source` (split) y `type_manifest` "
                "son modos mutuamente excluyentes"
            )
        self._columns = columns or MappingColumnsConfig()
        # 145 REQ-001: la clave es ``(sistema normalizado, id_rvi)``. Los
        # modos consolidado y split escriben todo bajo el comodín.
        self._cache: dict[tuple[str, str], CMMapping] = {}
        # Los ``IDRVI`` conocidos, para que ``__contains__`` siga siendo O(1)
        # ahora que la clave del cache es compuesta.
        self._id_rvis: set[str] = set()
        # 141 REQ-001: índice secundario ``IDCM -> [CMMapping, ...]``, poblado
        # en la MISMA pasada que el primario (una fila descartada por clave
        # duplicada o ``IDRVI`` vacío tampoco entra acá).
        self._by_cm_code: dict[str, list[CMMapping]] = {}
        # 145 REQ-001: los ``IDCM`` del CSV que el manifest no conoce.
        self._missing_cm_codes: set[str] = set()
        if type_manifest is not None:
            self._load_manifest(source, type_manifest)
        elif metadata_source is None:
            self._load(source)
        else:
            self._load_split(source, metadata_source)

    def _load_manifest(self, rvi_cm: IDataSource, manifest: CmTypeManifest) -> None:
        """`Loader` en modo manifest (145 REQ-001): ``MapeoRVI_CM`` ↔ manifest.

        El CSV aporta lo único que el servidor no sabe — qué código RVI
        (por sistema) va a qué clase; todo lo demás sale del manifest. Un
        ``IDCM`` que el manifest no tiene descarta la fila con WARNING y
        queda en :attr:`missing_cm_codes` para que ``types check`` y
        ``doctor`` lo muestren.
        """
        skipped = 0
        validated = False
        for row in rvi_cm.get_all():
            if not validated:
                self._validate_manifest_columns(row)
                validated = True

            id_rvi_raw = row.get(self._columns.col_rvi_cm_id_rvi)
            if _is_blank(id_rvi_raw):
                skipped += 1
                continue
            id_rvi = str(id_rvi_raw).strip()

            id_corto_raw = row.get(self._columns.col_rvi_cm_id_cm)
            id_corto = "" if id_corto_raw is None else str(id_corto_raw).strip()
            entry = manifest.types.get(id_corto)
            if entry is None:
                _logger.warning(
                    "IDCM %r (IDRVI %r) is not in the CM type manifest; row dropped",
                    id_corto,
                    id_rvi,
                )
                self._missing_cm_codes.add(id_corto)
                continue

            system = _norm_system(row.get(self._columns.col_rvi_cm_id_sistema))
            mapping = _mapping_from_entry(entry, id_rvi)
            if (system, id_rvi) in self._cache:
                _logger.warning(
                    "duplicate mapping key (system=%r, ID RVI=%r) dropped (first occurrence wins)",
                    system,
                    id_rvi,
                )
                self._remember_cm_code(mapping)
                continue

            self._remember(mapping, system)

        if skipped:
            _logger.info("skipped %d row(s) from MapeoRVI_CM with empty IDRVI", skipped)

    def _validate_manifest_columns(self, row: dict[str, object]) -> None:
        for col in self._columns.required_columns_manifest():
            if col not in row:
                raise ConfigurationError(
                    "MapeoRVI_CM missing required column",
                    missing_column=col,
                )

    @property
    def missing_cm_codes(self) -> tuple[str, ...]:
        """145 REQ-001: los ``IDCM`` del CSV que el manifest no conoce.

        Ordenados y sin repetir. Siempre vacío en los modos consolidado y
        split: ahí no hay manifest contra el cual fallar.
        """
        return tuple(sorted(self._missing_cm_codes))

    def _load_split(self, rvi_cm: IDataSource, metadatos: IDataSource) -> None:
        """`Loader` en modo split (035): join entre ``MapeoRVI_CM`` y
        ``MetadatosCM`` por ``IDCM ↔ IDCorto``."""
        required_index, cmis_property_id_index = self._build_metadatos_index(metadatos)
        skipped = 0
        validated = False
        for row in rvi_cm.get_all():
            if not validated:
                self._validate_rvi_cm_columns(row)
                validated = True

            id_rvi_raw = row.get(self._columns.col_rvi_cm_id_rvi)
            if _is_blank(id_rvi_raw):
                skipped += 1
                continue
            id_rvi = str(id_rvi_raw).strip()

            mapping = self._row_to_mapping_split(
                row, id_rvi, required_index, cmis_property_id_index
            )
            if (_WILDCARD_SYSTEM, id_rvi) in self._cache:
                _logger.warning(
                    "duplicate ID RVI %r dropped from mapping (first occurrence wins)",
                    id_rvi,
                )
                # 141 antagonista I3: la fila se descarta del índice por
                # IDRVI (gana la primera ocurrencia), pero su IDCM sigue
                # siendo una fila legítima del Modelo Documental — no
                # indexarla acá la hace invisible para ``get_by_cm_code``.
                self._remember_cm_code(mapping)
                continue

            self._remember(mapping)

        if skipped:
            _logger.info(
                "skipped %d row(s) from MapeoRVI_CM with empty IDRVI",
                skipped,
            )

    def _build_metadatos_index(
        self, metadatos: IDataSource
    ) -> tuple[dict[str, tuple[str, ...]], dict[str, dict[str, str]]]:
        """Devuelve ``(required_fields_by_id_corto,
        cmis_property_ids_by_id_corto)``.

        Campos requeridos: filas cuyo ``Requerido`` parsea como
        `truthy`. Los nombres de campo se trimean en whitespace y se
        preserva el orden.

        IDs de propiedad `cmis` (038): ``{id_corto: {field:
        cmis_property_id}}`` para filas cuya columna
        ``CMISPropertyId`` es no vacía. La clave es el nombre
        `friendly` del campo. Cuando la columna está ausente en la
        fuente o todas las celdas están vacías, se omite el dict por
        ``id_corto``, lo que le señala "sin catálogo" al servicio de
        metadata.
        """
        fields_index: dict[str, list[str]] = {}
        cmis_ids_index: dict[str, dict[str, str]] = {}
        validated = False
        for row in metadatos.get_all():
            if not validated:
                self._validate_metadatos_columns(row)
                validated = True
            id_corto_raw = row.get(self._columns.col_metadatos_id_corto)
            if _is_blank(id_corto_raw):
                continue
            id_corto = str(id_corto_raw).strip()
            if not _is_required(
                row.get(self._columns.col_metadatos_required),
                self._columns.required_marker,
            ):
                continue
            field_raw = row.get(self._columns.col_metadatos_metadata)
            if _is_blank(field_raw):
                continue
            field = str(field_raw).strip()
            fields_index.setdefault(id_corto, []).append(field)
            cmis_prop_raw = row.get(self._columns.col_metadatos_cmis_property_id)
            if not _is_blank(cmis_prop_raw):
                cmis_ids_index.setdefault(id_corto, {})[field] = str(cmis_prop_raw).strip()
        return (
            {k: tuple(v) for k, v in fields_index.items()},
            cmis_ids_index,
        )

    def _validate_rvi_cm_columns(self, row: dict[str, object]) -> None:
        for col in self._columns.required_columns_rvi_cm():
            if col not in row:
                raise ConfigurationError(
                    "MapeoRVI_CM missing required column",
                    missing_column=col,
                )

    def _validate_metadatos_columns(self, row: dict[str, object]) -> None:
        for col in self._columns.required_columns_metadatos():
            if col not in row:
                raise ConfigurationError(
                    "MetadatosCM missing required column",
                    missing_column=col,
                )

    def _row_to_mapping_split(
        self,
        row: dict[str, object],
        id_rvi: str,
        required_index: dict[str, tuple[str, ...]],
        cmis_property_id_index: dict[str, dict[str, str]],
    ) -> CMMapping:
        clase_id = str(row[self._columns.col_rvi_cm_clase_id]).strip()
        id_corto = str(row[self._columns.col_rvi_cm_id_cm]).strip()
        cmis_type_raw = row.get(self._columns.col_rvi_cm_cmis_type)
        cmis_type = "" if cmis_type_raw is None else str(cmis_type_raw).strip()
        cmis_folder_raw = row.get(self._columns.col_rvi_cm_cmis_folder)
        cmis_folder: str | None = (
            None if _is_blank(cmis_folder_raw) else str(cmis_folder_raw).strip()
        )
        cmis_property_ids_dict = cmis_property_id_index.get(id_corto)
        cmis_property_ids: MappingProxyType[str, str] | None = (
            MappingProxyType(cmis_property_ids_dict) if cmis_property_ids_dict else None
        )
        return CMMapping(
            clase_id=clase_id,
            id_rvi=id_rvi,
            id_corto=id_corto,
            clase_name=clase_id,
            required_metadata_fields=required_index.get(id_corto, ()),
            cmis_type=cmis_type,
            cmis_folder=cmis_folder,
            cmis_property_ids=cmis_property_ids,
        )

    def _load(self, source: IDataSource) -> None:
        skipped = 0
        validated = False
        for row in source.get_all():
            if not validated:
                self._validate_columns(row)
                validated = True

            id_rvi_raw = row.get(self._columns.col_id_rvi)
            if _is_blank(id_rvi_raw):
                skipped += 1
                continue
            id_rvi = str(id_rvi_raw).strip()

            mapping = self._row_to_mapping(row, id_rvi)
            if (_WILDCARD_SYSTEM, id_rvi) in self._cache:
                _logger.warning(
                    "duplicate ID RVI %r dropped from mapping (first occurrence wins)",
                    id_rvi,
                )
                # 141 antagonista I3: mismo criterio que el modo split — el
                # IDCM de la fila descartada sigue siendo indexable.
                self._remember_cm_code(mapping)
                continue

            self._remember(mapping)

        if skipped:
            _logger.info(
                "skipped %d row(s) from Modelo Documental with empty ID RVI",
                skipped,
            )

    def _validate_columns(self, row: dict[str, object]) -> None:
        for col in self._columns.required_columns():
            if col not in row:
                raise ConfigurationError(
                    "Modelo Documental missing required column",
                    missing_column=col,
                )

    def _row_to_mapping(self, row: dict[str, object], id_rvi: str) -> CMMapping:
        cmis_type_raw = row.get(self._columns.col_cmis_type)
        cmis_type = "" if cmis_type_raw is None else str(cmis_type_raw).strip()
        return CMMapping(
            clase_id=str(row[self._columns.col_clase_id]).strip(),
            id_rvi=id_rvi,
            id_corto=str(row[self._columns.col_id_corto]).strip(),
            clase_name=str(row[self._columns.col_clase_name]).strip(),
            required_metadata_fields=_parse_metadata_list(row.get(self._columns.col_metadata_list)),
            cmis_type=cmis_type,
        )

    def _remember(self, mapping: CMMapping, system: str = _WILDCARD_SYSTEM) -> None:
        """141 REQ-001: cachea la fila en los DOS índices a la vez.

        145 REQ-001: el índice primario pasó a estar tecleado por
        ``(sistema, IDRVI)`` — los modos consolidado y split no pasan
        sistema y caen al comodín, o sea se comportan igual que siempre.
        El índice por ``IDCM`` se delega a :meth:`_remember_cm_code`.
        """
        self._cache[(system, mapping.id_rvi)] = mapping
        self._id_rvis.add(mapping.id_rvi)
        self._remember_cm_code(mapping)

    def _remember_cm_code(self, mapping: CMMapping) -> None:
        """141 REQ-001 (antagonista I3): índice por ``IDCM`` en solitario.

        Se llama tanto para filas nuevas (vía :meth:`_remember`) como
        para filas cuyo ``IDRVI`` es un duplicado descartado del índice
        primario — el ``IDCM`` de esa fila sigue siendo una entrada
        legítima del Modelo Documental, y ``get_by_cm_code`` no puede
        perderla solo porque otra fila anterior ya ocupó el mismo
        ``IDRVI``. Acumula todas las filas que apuntan al mismo código,
        en orden de aparición en la fuente. Las filas con ``id_corto``
        vacío no entran — no hay código CM que buscar.
        """
        if mapping.id_corto:
            self._by_cm_code.setdefault(mapping.id_corto, []).append(mapping)

    def get_by_cm_code(self, id_cm: str) -> tuple[CMMapping, ...]:
        """141 REQ-001: todas las filas cuyo ``IDCM`` es *id_cm*.

        Comparación exacta después de ``strip()``; tupla vacía cuando el
        código no está en el mapping. Varias filas ``IDRVI`` pueden
        apuntar al mismo ``IDCM`` con distinto ``CMISType`` /
        ``CMISFolder``: se devuelven todas, en orden de aparición.
        """
        return tuple(self._by_cm_code.get(id_cm.strip(), ()))

    def cm_codes(self) -> tuple[str, ...]:
        """141 REQ-001: los códigos CM conocidos, ordenados y sin repetir."""
        return tuple(sorted(self._by_cm_code))

    def get_mapping(self, id_rvi: str, system_id: str | None = None) -> CMMapping:
        """Devuelve el :class:`CMMapping` para *id_rvi*; lanza si no hay match.

        145 REQ-001: primero busca la fila específica del sistema
        (comparación con ``strip()`` y case-insensitive), después el
        comodín. Los modos consolidado y split guardan todo bajo el
        comodín, así que ahí *system_id* es simplemente ignorado.
        """
        if system_id:
            specific = self._cache.get((_norm_system(system_id), id_rvi))
            if specific is not None:
                return specific
        wildcard = self._cache.get((_WILDCARD_SYSTEM, id_rvi))
        if wildcard is None:
            raise IDRViNotMappedError(id_rvi=id_rvi)
        return wildcard

    def get_all(self) -> Iterator[CMMapping]:
        """Yieldea cada mapping cacheado en el orden en que las filas
        llegaron de la fuente."""
        return iter(self._cache.values())

    def count(self) -> int:
        """Devuelve la cantidad de mappings cacheados (filas, no ``IDRVI``)."""
        return len(self._cache)

    def __contains__(self, id_rvi: object) -> bool:
        """``True`` sii hay AL MENOS una fila con ese ``IDRVI``, sea de
        qué sistema sea (145 REQ-001)."""
        return isinstance(id_rvi, str) and id_rvi in self._id_rvis
