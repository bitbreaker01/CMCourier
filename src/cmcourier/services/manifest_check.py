"""``types check``: manifest ↔ YAML ↔ CSV (145 REQ-005).

Tres archivos tienen que estar de acuerdo para que un upload no explote
en producción:

* el **manifest** — qué clases publica Content Manager, con qué
  propiedades escribibles y qué decidió el operador sobre cada una;
* el **YAML** — ``metadata.field_sources``, de dónde sale el VALOR de
  cada propiedad;
* ``MapeoRVI_CM.csv`` — qué código RVI (por sistema) va a qué clase.

Este módulo los cruza y devuelve un :class:`CheckReport` agrupado por
severidad. Cero red y cero disco: entran tres estructuras ya cargadas.
Lo consumen ``cmcourier types check`` (exit 1 si hay CRITICAL) y el
check ``cm_manifest`` del ``doctor`` (que reporta sólo los CRITICAL).
"""

from __future__ import annotations

__all__ = ["CheckFinding", "CheckReport", "run_manifest_check"]

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from cmcourier.domain.cm_types import CmPropertyDef, CmTypeEntry, CmTypeManifest, canonical_name
from cmcourier.services.mapping import MappingService
from cmcourier.services.metadata import FieldSourceConfig

Severity = Literal["CRITICAL", "WARNING", "INFO"]

#: El orden en que ``render`` y ``to_json_dict`` agrupan.
_SEVERITIES: tuple[Severity, ...] = ("CRITICAL", "WARNING", "INFO")

#: Tipos `cmis` cuyo valor fijo tiene que parsear antes de viajar al wire.
_TYPED = frozenset({"integer", "boolean", "datetime"})

#: Lo que un `boolean` `cmis` acepta como valor de texto.
_BOOLEANS = frozenset({"true", "false", "1", "0", "yes", "no"})

#: Patrones "simples" de los que SÍ se puede deducir un largo:
#: ``^\d{8}$``, ``^.{1,30}$``, ``^[A-Z]{3}$``. Cualquier otra cosa se
#: saltea — adivinar el largo de un regex arbitrario sería peor que no
#: chequear nada.
_SIMPLE_PATTERN = re.compile(r"^\^(?:\\[dwsDWS]|\.|\[[^\]]+\])\{(\d+)(?:,(\d+))?\}\$$")


@dataclass(frozen=True, slots=True)
class CheckFinding:
    """Un hallazgo del cruce. ``id_corto`` / ``prop_id`` acotan el foco."""

    severity: Severity
    id_corto: str | None
    prop_id: str | None
    message: str

    def render(self) -> str:
        """Una línea legible: ``[DC01/clbNonGroup.BAC_CIF] mensaje``."""
        scope = "/".join(p for p in (self.id_corto, self.prop_id) if p)
        return f"[{scope}] {self.message}" if scope else self.message


@dataclass(frozen=True, slots=True)
class CheckReport:
    """El resultado completo de :func:`run_manifest_check`."""

    findings: tuple[CheckFinding, ...] = field(default=())

    def _of(self, severity: Severity) -> tuple[CheckFinding, ...]:
        return tuple(f for f in self.findings if f.severity == severity)

    @property
    def criticals(self) -> tuple[CheckFinding, ...]:
        """Lo que rompe el upload: sin esto, no se sube."""
        return self._of("CRITICAL")

    @property
    def warnings(self) -> tuple[CheckFinding, ...]:
        """Lo que huele mal pero no aborta."""
        return self._of("WARNING")

    @property
    def infos(self) -> tuple[CheckFinding, ...]:
        """Lo que sobra: ruido en el YAML que nadie usa."""
        return self._of("INFO")

    @property
    def has_critical(self) -> bool:
        """``True`` sii hay al menos un CRITICAL (exit 1 de ``types check``)."""
        return any(f.severity == "CRITICAL" for f in self.findings)

    def render(self) -> str:
        """Texto agrupado por severidad — lo que ve el operador."""
        if not self.findings:
            return "Sin hallazgos: manifest, YAML y CSV están alineados."
        lines: list[str] = []
        for severity in _SEVERITIES:
            bucket = self._of(severity)
            if not bucket:
                continue
            lines.append(f"{severity} ({len(bucket)}):")
            lines.extend(f"  {f.render()}" for f in bucket)
        return "\n".join(lines)

    def to_json_dict(self) -> dict[str, Any]:
        """La misma info, `serializable` — el ``--json`` de ``types check``."""
        return {
            "has_critical": self.has_critical,
            "counts": {s: len(self._of(s)) for s in _SEVERITIES},
            "findings": [
                {
                    "severity": f.severity,
                    "id_corto": f.id_corto,
                    "prop_id": f.prop_id,
                    "message": f.message,
                }
                for f in self.findings
            ],
        }


# ---------------------------------------------------------------------------
# Helpers de las reglas
# ---------------------------------------------------------------------------


def _pattern_max_length(pattern: str | None) -> int | None:
    """Largo máximo que declara un patrón simple, o ``None``."""
    if not pattern:
        return None
    match = _SIMPLE_PATTERN.match(pattern)
    if match is None:
        return None
    return int(match.group(2) or match.group(1))


def _fixed_value(fsc: FieldSourceConfig) -> str | None:
    """El valor fijo del campo: sin cadena de fallback, sólo ``default_value``."""
    return None if fsc.sources else fsc.default_value


def _declared_length(fsc: FieldSourceConfig) -> int | None:
    """Largo que el YAML declara para el campo, o ``None`` si no declara.

    Un valor fijo declara su propio largo; una cadena de lookups declara
    el mayor de los largos que se puedan deducir de sus patrones.
    """
    fixed = _fixed_value(fsc)
    if fixed is not None:
        return len(fixed)
    patterns = (src.validation.allowed_pattern if src.validation else None for src in fsc.sources)
    lengths = [length for p in patterns if (length := _pattern_max_length(p)) is not None]
    return max(lengths) if lengths else None


def _parses(value: str, property_type: str) -> bool:
    """``True`` sii *value* es un literal válido para ese tipo `cmis`."""
    text = value.strip()
    if property_type == "integer":
        try:
            int(text)
        except ValueError:
            return False
        return True
    if property_type == "boolean":
        return text.lower() in _BOOLEANS
    try:
        datetime.fromisoformat(text)
    except ValueError:
        return False
    return True


def _check_used_property(
    entry: CmTypeEntry,
    prop: CmPropertyDef,
    field_sources: Mapping[str, FieldSourceConfig],
) -> list[CheckFinding]:
    """Reglas de una propiedad marcada ``usar``."""
    name = canonical_name(prop.id)
    fsc = field_sources.get(name)
    if fsc is None:
        return [
            CheckFinding(
                "CRITICAL",
                entry.id_corto,
                prop.id,
                f"propiedad 'usar' sin entrada metadata.field_sources.{name}",
            )
        ]
    out: list[CheckFinding] = []
    declared = _declared_length(fsc)
    if prop.max_length is not None and declared is not None and declared > prop.max_length:
        out.append(
            CheckFinding(
                "WARNING",
                entry.id_corto,
                prop.id,
                f"field_sources.{name} declara largo {declared} y el max_length "
                f"de CM es {prop.max_length}",
            )
        )
    fixed = _fixed_value(fsc)
    typed = prop.property_type in _TYPED
    if fixed is not None and typed and not _parses(fixed, prop.property_type):
        out.append(
            CheckFinding(
                "WARNING",
                entry.id_corto,
                prop.id,
                f"field_sources.{name} tiene un valor fijo que no parsea como {prop.property_type}",
            )
        )
    return out


def _check_entry(
    entry: CmTypeEntry, field_sources: Mapping[str, FieldSourceConfig]
) -> list[CheckFinding]:
    """Todas las reglas de UN tipo mapeado."""
    out: list[CheckFinding] = []
    if entry.missing_on_server:
        out.append(
            CheckFinding("CRITICAL", entry.id_corto, None, "el servidor ya no publica este tipo")
        )
    if not entry.reviewed:
        out.append(CheckFinding("WARNING", entry.id_corto, None, "el tipo todavía no fue revisado"))
    if entry.folder_ok is False:
        out.append(
            CheckFinding(
                "WARNING", entry.id_corto, None, f"la carpeta {entry.folder} no existe en CM"
            )
        )
    usable = {p.id for p in entry.usable_properties()}
    for prop in entry.properties:
        if prop.id in usable:
            out.extend(_check_used_property(entry, prop, field_sources))
        elif prop.required and prop.default_value is None:
            out.append(
                CheckFinding(
                    "WARNING",
                    entry.id_corto,
                    prop.id,
                    "propiedad requerida sin default marcada 'omitir'",
                )
            )
    return out


def _used_canonical_names(entry: CmTypeEntry) -> set[str]:
    return {canonical_name(p.id) for p in entry.usable_properties()}


# ---------------------------------------------------------------------------
# Entrada pública
# ---------------------------------------------------------------------------


def run_manifest_check(
    mapping: MappingService,
    manifest: CmTypeManifest,
    field_sources: Mapping[str, FieldSourceConfig],
) -> CheckReport:
    """Cruza manifest, YAML y CSV y devuelve el reporte (145 REQ-005).

    Los tipos que se miran son los que ``MapeoRVI_CM.csv`` referencia —
    el manifest puede tener 362 clases y el banco usar 40, y las otras
    322 no son problema de nadie. El orden de los hallazgos es estable:
    primero los códigos que faltan, después los tipos por ID corto, y al
    final las entradas de ``field_sources`` que sobran.
    """
    findings: list[CheckFinding] = [
        CheckFinding("CRITICAL", code, None, "el IDCM del mapeo no existe en el manifest")
        for code in mapping.missing_cm_codes
    ]
    mapped = sorted({m.id_corto for m in mapping.get_all() if m.id_corto})
    used: set[str] = set()
    for code in mapped:
        entry = manifest.types.get(code)
        if entry is None:  # pragma: no cover — ya lo cubre missing_cm_codes
            findings.append(
                CheckFinding("CRITICAL", code, None, "el IDCM del mapeo no existe en el manifest")
            )
            continue
        used |= _used_canonical_names(entry)
        findings.extend(_check_entry(entry, field_sources))
    findings.extend(
        CheckFinding(
            "INFO", None, None, f"metadata.field_sources.{name} no lo usa ningún tipo mapeado"
        )
        for name in sorted(set(field_sources) - used)
    )
    return CheckReport(findings=tuple(findings))
