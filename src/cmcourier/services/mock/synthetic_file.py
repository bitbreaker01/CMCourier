"""141 REQ-002 — archivo sintético multi-formato para el tiro de prueba.

Módulo puro (sin I/O): entra ``(formato, tamaño, marker)``, salen los
bytes de un archivo VÁLIDO de ese formato, determinístico por *marker*.

``pdf`` reusa el generador de 102 (:func:`build_synthetic_pdf`), que da
tamaño EXACTO: el content stream se rellena con whitespace, así que se
ajusta el relleno hasta que el total coincide al byte.

``tiff`` / ``jpeg`` / ``png`` se generan con Pillow: una imagen con el
*marker* dibujado sobre ruido determinístico (semilla = *marker*). El
ruido es prácticamente incompresible, así que el tamaño crece con la
cantidad de píxeles; se itera escalando el lado de la imagen hasta caer
dentro de ±15 % del objetivo. Si el formato no llega (un JPEG chico no
puede pesar lo que se le pide), se devuelve el mejor intento y
:attr:`SyntheticFile.size_note` lo dice.
"""

from __future__ import annotations

__all__ = [
    "EXTENSIONS",
    "MAX_SIZE_BYTES",
    "MIME_TYPES",
    "MIN_SIZE_BYTES",
    "SyntheticFile",
    "SyntheticFormat",
    "build_synthetic_file",
]

import math
import random
from dataclasses import dataclass
from io import BytesIO
from typing import Literal, get_args

from PIL import Image, ImageDraw

from cmcourier.services.mock.synthetic_content import build_synthetic_pdf

SyntheticFormat = Literal["pdf", "tiff", "jpeg", "png"]

MIN_SIZE_BYTES = 1024
MAX_SIZE_BYTES = 50 * 1024 * 1024

# ±15 % — la banda que la spec 141 acepta para los formatos de imagen.
_TOLERANCE = 0.15
# Cuántas veces se reescala la imagen antes de rendirse con ``size_note``.
_MAX_ATTEMPTS = 12
_MIN_SIDE = 8
_JPEG_QUALITY = 95

MIME_TYPES: dict[str, str] = {
    "pdf": "application/pdf",
    "tiff": "image/tiff",
    "jpeg": "image/jpeg",
    "png": "image/png",
}
EXTENSIONS: dict[str, str] = {
    "pdf": ".pdf",
    "tiff": ".tif",
    "jpeg": ".jpg",
    "png": ".png",
}


@dataclass(frozen=True, slots=True)
class SyntheticFile:
    """Los bytes generados más cómo mandarlos por el cable.

    ``size_note`` queda en ``""`` cuando el tamaño cayó dentro de la
    tolerancia; en caso contrario explica por qué no llegó, para que la
    pantalla lo muestre como aviso.
    """

    content: bytes
    mime_type: str
    extension: str
    size_note: str = ""

    @property
    def size_bytes(self) -> int:
        return len(self.content)


def build_synthetic_file(fmt: SyntheticFormat, size_bytes: int, marker: str) -> SyntheticFile:
    """141 REQ-002: construye el archivo sintético pedido.

    *size_bytes* tiene que estar entre 1 KB y 50 MB; fuera de rango
    lanza ``ValueError``, igual que un formato desconocido.
    """
    if fmt not in get_args(SyntheticFormat):
        raise ValueError(f"formato sintético desconocido: {fmt!r}")
    if not MIN_SIZE_BYTES <= size_bytes <= MAX_SIZE_BYTES:
        raise ValueError(
            f"size_bytes fuera de rango: {size_bytes} (mínimo {MIN_SIZE_BYTES}, "
            f"máximo {MAX_SIZE_BYTES})"
        )
    if fmt == "pdf":
        content, note = _build_pdf(size_bytes, marker)
    else:
        content, note = _build_image(fmt, size_bytes, marker)
    return SyntheticFile(
        content=content,
        mime_type=MIME_TYPES[fmt],
        extension=EXTENSIONS[fmt],
        size_note=note,
    )


def _build_pdf(size_bytes: int, marker: str) -> tuple[bytes, str]:
    """Ajusta el relleno del content stream hasta pegarle EXACTO al total.

    El overhead estructural (offsets del xref, dígitos de ``/Length``)
    cambia con el tamaño del stream, así que se corrige el pedido con la
    diferencia observada; converge en dos o tres vueltas.
    """
    content_bytes = size_bytes
    best = build_synthetic_pdf(content_bytes, marker)
    for _ in range(_MAX_ATTEMPTS):
        delta = len(best) - size_bytes
        if delta == 0:
            return best, ""
        content_bytes -= delta
        if content_bytes < 0:
            break
        best = build_synthetic_pdf(content_bytes, marker)
    return best, _note(len(best), size_bytes, "pdf")


def _build_image(fmt: str, size_bytes: int, marker: str) -> tuple[bytes, str]:
    """Escala la imagen hasta que el archivo codificado cae en la banda."""
    # Punto de partida: ruido RGB casi incompresible ⇒ 3 bytes por píxel.
    pixels = max(_MIN_SIDE * _MIN_SIDE, size_bytes // 3)
    best: bytes = b""
    for _ in range(_MAX_ATTEMPTS):
        side = max(_MIN_SIDE, math.isqrt(pixels))
        data = _encode(fmt, side, marker)
        if best == b"" or abs(len(data) - size_bytes) < abs(len(best) - size_bytes):
            best = data
        if abs(len(data) - size_bytes) <= size_bytes * _TOLERANCE:
            return data, ""
        scaled = int(pixels * (size_bytes / len(data)))
        pixels = max(_MIN_SIDE * _MIN_SIDE, scaled)
        if math.isqrt(pixels) == side:
            break  # ya no se puede achicar/agrandar más: el formato tocó su piso
    return best, _note(len(best), size_bytes, fmt)


def _encode(fmt: str, side: int, marker: str) -> bytes:
    """Renderiza y codifica una imagen cuadrada de lado *side*.

    La semilla es el *marker*, así que el mismo marker devuelve siempre
    los mismos bytes (se re-siembra en cada intento, no se arrastra el
    estado del PRNG entre escalas).
    """
    rng = random.Random(marker)
    image = Image.frombytes("RGB", (side, side), rng.randbytes(side * side * 3))
    ImageDraw.Draw(image).text((2, 2), marker, fill=(0, 0, 0))
    buffer = BytesIO()
    if fmt == "tiff":
        image.save(buffer, format="TIFF", compression="tiff_lzw")
    elif fmt == "jpeg":
        image.save(buffer, format="JPEG", quality=_JPEG_QUALITY)
    else:
        image.save(buffer, format="PNG", compress_level=1)
    return buffer.getvalue()


def _note(actual: int, target: int, fmt: str) -> str:
    """Aviso amarillo para la pantalla cuando no se llegó al tamaño."""
    return (
        f"{fmt}: no se pudo aproximar {target} bytes — el archivo quedó en "
        f"{actual} bytes (lo más cerca que llega este formato)"
    )
