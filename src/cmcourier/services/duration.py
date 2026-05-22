"""Parser de duraciones humanas para ``--max-duration`` (103, REQ-001).

Módulo puro. Devuelve segundos como ``float``. Acepta:

* un número crudo no negativo → se interpreta como **segundos**
  (``"90"`` → ``90.0``);
* uno o más componentes ``<número><unidad>`` con unidad en
  ``d`` / ``h`` / ``m`` / ``s`` (case-insensitive), opcionalmente
  encadenados (``"1h30m"``, ``"1h30m15s"``).

El whitespace se tolera en cualquier posición. Input inválido o una
duración no positiva lanzan :class:`ValueError` — análogo a
:func:`cmcourier.services.mock.sizing.parse_size`.
"""

from __future__ import annotations

__all__ = ["parse_duration"]

import re

_UNIT_SECONDS: dict[str, int] = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
}

_BARE = re.compile(r"\d+(?:\.\d+)?")
_COMPONENTS = re.compile(r"(?:\d+(?:\.\d+)?[dhms])+", re.IGNORECASE)
_ONE_COMPONENT = re.compile(r"(\d+(?:\.\d+)?)([dhms])", re.IGNORECASE)


def parse_duration(text: str) -> float:
    """Devuelve la cantidad de **segundos** codificada por *text*.

    Lanza ``ValueError`` si *text* no matchea la gramática, o si la
    duración resultante no es estrictamente positiva (un
    ``--max-duration`` de cero no tiene sentido operativo).
    """
    compact = re.sub(r"\s+", "", text)
    if not compact:
        raise ValueError(f"invalid duration {text!r}")

    if _BARE.fullmatch(compact):
        seconds = float(compact)
    elif _COMPONENTS.fullmatch(compact):
        seconds = sum(
            float(value) * _UNIT_SECONDS[unit.lower()]
            for value, unit in _ONE_COMPONENT.findall(compact)
        )
    else:
        raise ValueError(f"invalid duration {text!r}")

    if seconds <= 0:
        raise ValueError(f"duration must be positive: {text!r}")
    return seconds
