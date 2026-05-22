"""Proveedor de contenido sintético on-the-fly para pruebas de stress (102).

Genera un **PDF de una sola página, estructuralmente válido**, de tamaño
controlado, sin tocar :mod:`img2pdf` ni :mod:`PIL`. El costo de
generación es O(tamaño) — esencialmente un relleno de buffer —, acotado
por el ancho de banda de memoria; órdenes de magnitud por encima de
cualquier tasa de upload por red, así que la generación **nunca** es el
cuello de botella (REQ-004).

El tamaño de cada documento es **determinístico por ``txn_num``**
(REQ-003): se siembra un PRNG con ``(seed, txn_num)``, se sortea una
clase de tamaño contra la distribución configurada y un tamaño exacto
dentro de su banda. La misma ``txn_num`` produce siempre el mismo PDF —
reproducible y a prueba de ``--resume``.

El ``txn_num`` se embebe en el content stream (REQ-006): el contenido no
es byte-idéntico entre documentos, lo que defiende contra una eventual
deduplicación por hash en el destino.

Constitución, Principio I: solo stdlib + ``cmcourier.domain``.
"""

from __future__ import annotations

__all__ = [
    "DEFAULT_SIZE_MIX",
    "SizeBand",
    "SizeMix",
    "SyntheticPdfProvider",
    "build_synthetic_pdf",
]

import random
from dataclasses import dataclass

from cmcourier.domain.exceptions import ConfigurationError

# Comentario binario tras el header — marca el archivo como binario
# (convención PDF para que las herramientas no lo traten como texto).
_BINARY_MARKER = b"%\xe2\xe3\xcf\xd3\n"
_HEADER = b"%PDF-1.4\n" + _BINARY_MARKER


@dataclass(frozen=True, slots=True)
class SizeBand:
    """Una clase de tamaño: nombre, peso relativo y banda de bytes."""

    name: str
    weight: float
    min_bytes: int
    max_bytes: int

    def __post_init__(self) -> None:
        if self.weight < 0:
            raise ConfigurationError(f"SizeBand {self.name!r} weight must be >= 0")
        if self.min_bytes < 0 or self.max_bytes < self.min_bytes:
            raise ConfigurationError(f"SizeBand {self.name!r}: need 0 <= min_bytes <= max_bytes")


@dataclass(frozen=True, slots=True)
class SizeMix:
    """Distribución de tamaños: un conjunto de :class:`SizeBand` ponderadas.

    Los pesos se renormalizan internamente; no necesitan sumar 1.
    """

    bands: tuple[SizeBand, ...]

    def __post_init__(self) -> None:
        if not self.bands:
            raise ConfigurationError("SizeMix needs at least one band")
        if sum(b.weight for b in self.bands) <= 0:
            raise ConfigurationError("SizeMix band weights cannot all be zero")

    def pick(self, rng: random.Random) -> SizeBand:
        """Sortea una banda ponderada por peso."""
        total = sum(b.weight for b in self.bands)
        r = rng.random() * total
        cum = 0.0
        for band in self.bands:
            cum += band.weight
            if r < cum:
                return band
        return self.bands[-1]


# Distribución por defecto, alineada al plan de stress §3.3
# (60% pequeños / 30% medianos / 10% grandes).
DEFAULT_SIZE_MIX = SizeMix(
    bands=(
        SizeBand("small", 60.0, 50 * 1024, 1024 * 1024),
        SizeBand("medium", 30.0, 1024 * 1024, 10 * 1024 * 1024),
        SizeBand("large", 10.0, 10 * 1024 * 1024, 40 * 1024 * 1024),
    )
)


def build_synthetic_pdf(content_bytes: int, marker: str) -> bytes:
    """Construye un PDF de una página válido cuyo tamaño total es
    ``content_bytes`` + un overhead estructural chico y fijo.

    El content stream lleva un comentario con *marker* (el ``txn_num``)
    seguido de relleno de whitespace — ambos legales dentro de un content
    stream PDF. Costo: una concatenación de bytes + un ``b" " * n``.
    """
    marker_line = f"% CMCourier 102 synthetic stress content — txn {marker}\n"
    marker_bytes = marker_line.encode("ascii", "replace")
    pad = max(0, content_bytes - len(marker_line))
    stream_data = marker_bytes + b" " * pad

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << >> >>",
        b"<< /Length "
        + str(len(stream_data)).encode("ascii")
        + b" >>\nstream\n"
        + stream_data
        + b"\nendstream",
        b"<< /Producer (CMCourier 102 synthetic stress content) >>",
    ]

    out = bytearray(_HEADER)
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(index).encode("ascii") + b" 0 obj\n" + body + b"\nendobj\n"

    xref_pos = len(out)
    size = len(objects) + 1
    out += b"xref\n0 " + str(size).encode("ascii") + b"\n"
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        b"trailer\n<< /Size "
        + str(size).encode("ascii")
        + b" /Root 1 0 R /Info 5 0 R >>\n"
        + b"startxref\n"
        + str(xref_pos).encode("ascii")
        + b"\n%%EOF"
    )
    return bytes(out)


class SyntheticPdfProvider:
    """Genera PDFs sintéticos deterministas por ``txn_num`` (102)."""

    __slots__ = ("_size_mix", "_seed")

    def __init__(self, size_mix: SizeMix = DEFAULT_SIZE_MIX, seed: int = 0) -> None:
        self._size_mix = size_mix
        self._seed = seed

    def generate(self, txn_num: str) -> bytes:
        """Devuelve los bytes del PDF sintético para *txn_num*.

        Determinístico: el mismo ``(seed, txn_num)`` produce siempre el
        mismo PDF (REQ-003 — reproducibilidad y resume-safety).
        """
        rng = random.Random(f"{self._seed}:{txn_num}")
        band = self._size_mix.pick(rng)
        content_bytes = rng.randint(band.min_bytes, band.max_bytes)
        return build_synthetic_pdf(content_bytes, txn_num)
