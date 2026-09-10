"""Modelos del manifest de tipos CM (145 REQ-002).

El manifest es la foto que CMCourier guarda de lo que Content Manager ya
publica en ``typeDescendants``: qué clases documentales existen, con qué
propiedades escribibles, y qué decidió el operador sobre cada una. Es lo
que jubila a ``MetadatosCM.csv`` — nadie mantiene a mano lo que el
servidor ya sabe.

Dominio puro: sólo `stdlib`. Cada `dataclass` es ``frozen=True,
slots=True``; los diccionarios salen envueltos en
:class:`~types.MappingProxyType` para que la inmutabilidad no sea sólo
una promesa. Toda mutación se hace con ``dataclasses.replace`` en
:mod:`cmcourier.services.type_manifest`.
"""

from __future__ import annotations

__all__ = [
    "DECISION_OMIT",
    "DECISION_USE",
    "FOLDER_DERIVED",
    "FOLDER_MANUAL",
    "CmPropertyDef",
    "CmTypeEntry",
    "CmTypeManifest",
    "canonical_name",
    "derive_folder",
]

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

#: Decisión del operador: la propiedad viaja al wire en cada upload.
DECISION_USE = "usar"
#: Decisión del operador: la propiedad no se manda (el servidor pone su default).
DECISION_OMIT = "omitir"

#: ``folder_source``: la carpeta salió de ``derive_folder(local_name)``.
FOLDER_DERIVED = "derivada"
#: ``folder_source``: la carpeta la escribió el operador a mano.
FOLDER_MANUAL = "manual"

_EMPTY_DECISIONS: Mapping[str, str] = MappingProxyType({})
_EMPTY_TYPES: Mapping[str, CmTypeEntry] = MappingProxyType({})


def canonical_name(prop_id: str) -> str:
    """Nombre canónico de una propiedad: el id de wire sin su prefijo.

    Se corta hasta el último ``.`` o ``:``, lo que venga más a la
    derecha — ``clbNonGroup.BAC_CIF`` y ``cmcourier:BAC_CIF`` dan los dos
    ``BAC_CIF``, que es exactamente la clave que usa
    ``metadata.field_sources`` en el YAML.
    """
    cut = max(prop_id.rfind("."), prop_id.rfind(":"))
    return prop_id[cut + 1 :] if cut >= 0 else prop_id


def derive_folder(local_name: str) -> str:
    """Carpeta `cmis` derivada de un ``localName`` — ``/$type/<localName>``.

    Mismo fallback histórico que ``compute_cm_folder``, pero partiendo
    del nombre que publica el servidor en vez de reconstruirlo desde el
    ``clase_id`` del CSV.
    """
    return f"/$type/{local_name}"


@dataclass(frozen=True, slots=True)
class CmPropertyDef:
    """Una propiedad escribible de un tipo CM, tal cual la publica el server.

    Sólo se guardan las escribibles (``updatability`` ``readwrite`` /
    ``oncreate``); las readonly no le sirven a nadie aguas abajo.
    ``default_value`` ya viene normalizado a un string (o ``None``): el
    servidor lo manda escalar o como lista de un elemento según el tipo.
    """

    id: str
    display_name: str
    property_type: str
    cardinality: str
    updatability: str
    required: bool
    max_length: int | None
    default_value: str | None
    inherited: bool
    choices: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CmTypeEntry:
    """Una clase documental del manifest, indexada por su ID corto.

    ``decisions`` mapea id de wire → ``"usar"``/``"omitir"``.
    ``changes`` son las líneas legibles del último ``update`` que tocó
    este tipo (se limpian al marcarlo revisado). ``missing_on_server``
    marca los tipos que el servidor dejó de publicar: no se borran, se
    marcan, para que el operador decida.
    """

    id_corto: str
    type_id: str
    local_name: str
    display_name: str
    folder: str
    folder_source: str = FOLDER_DERIVED
    folder_ok: bool | None = None
    properties: tuple[CmPropertyDef, ...] = ()
    decisions: Mapping[str, str] = field(default=_EMPTY_DECISIONS)
    reviewed: bool = False
    changes: tuple[str, ...] = ()
    missing_on_server: bool = False

    def __post_init__(self) -> None:
        # Copia + proxy: si el caller muta después el dict que nos pasó,
        # la entrada no se entera. Frozen ⇒ hay que ir por object.
        object.__setattr__(self, "decisions", MappingProxyType(dict(self.decisions)))

    def usable_properties(self) -> tuple[CmPropertyDef, ...]:
        """Las propiedades con decisión ``usar``, en el orden del servidor."""
        return tuple(p for p in self.properties if self.decisions.get(p.id) == DECISION_USE)


@dataclass(frozen=True, slots=True)
class CmTypeManifest:
    """El manifest completo: metadata del repo + tipos indexados por ID corto.

    ``without_code`` lista los tipos creables que el servidor publica
    pero que no tienen ID corto (ni por ``BAC_ID_Corto`` ni por el
    prefijo del ``displayName``): pares ``(type_id, display_name)`` para
    que el operador los vea y decida si le importan.
    """

    service_url: str
    repository_id: str
    discovered_at: str
    types: Mapping[str, CmTypeEntry] = field(default=_EMPTY_TYPES)
    without_code: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "types", MappingProxyType(dict(self.types)))
