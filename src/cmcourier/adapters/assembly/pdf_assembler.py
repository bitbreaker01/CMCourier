"""Etapa S4 — :class:`PdfAssembler`.

Implementación concreta de :class:`IAssembler` sobre el file server local.
Dos caminos:

* **PDF nativo** — ``shutil.copy2`` del PDF fuente a
  ``temp_dir / "{txn_num}.pdf"``. ``StagedFile.page_count`` se lee de
  ``total_pages`` del documento (confiamos en RVABREP, no parseamos).
* **Documento paginado** — glob de ``FILECODE.*`` en el directorio fuente,
  filtra extensiones numéricas, ordena por ``int(extension)``, y luego
  intenta :func:`img2pdf.convert` como camino rápido. Ante cualquier
  excepción, cae al fallback con Pillow + :class:`PyPDF2.PdfMerger`.

La trampa del directorio temporal en OneDrive se maneja en el constructor:
las rutas configuradas que coincidan con variantes de ``./tmp`` se desvían
al directorio temporal del sistema.

Principio I de la Constitución: este módulo importa ``img2pdf``, ``PIL`` y
``PyPDF2`` — todos declarados en ``pyproject.toml``. Los modelos de dominio
se importan solo como tipos. Principio VIII: los logs identifican claves
operacionales (``txn_num``, ``file_path``, conteos de páginas) pero nunca
contenido de imágenes ni valores de metadatos.
"""

from __future__ import annotations

__all__ = ["AssemblerConfig", "AssemblyTimings", "PdfAssembler"]

import logging
import shutil
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

import img2pdf
from PIL import Image
from PyPDF2 import PdfMerger

from cmcourier.domain.exceptions import (
    PDFAssemblyFailedError,
    SourceFileMissingError,
)
from cmcourier.domain.models import RVABREPDocument, StagedFile
from cmcourier.domain.ports import IAssembler
from cmcourier.services.mock.synthetic_content import SyntheticPdfProvider

_log = logging.getLogger(__name__)


# Variantes de la trampa de OneDrive normalizadas para comparación case-insensitive.
_ONEDRIVE_TRAP_VARIANTS: frozenset[str] = frozenset({"tmp", "./tmp", "tmp/", ".\\tmp"})
_DIVERTED_DIR_NAME = "cmcourier_tmp"


def _default_image_type_map() -> dict[str, str]:
    """Mapeo: código image_type → hint MIME."""
    return {"B": "image/tiff", "O": "application/pdf", "C": "image/jpeg"}


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AssemblerConfig:
    """Configuración para :class:`PdfAssembler`."""

    source_root: Path
    temp_dir: Path
    image_type_map: Mapping[str, str] = field(default_factory=_default_image_type_map)
    # 102: cuando está seteado, S4 genera un PDF sintético on-the-fly en
    # vez de leer el archivo fuente — para pruebas de stress sin
    # materializar TB de corpus. Default None → comportamiento intacto.
    synthetic_provider: SyntheticPdfProvider | None = None


@dataclass(frozen=True, slots=True)
class AssemblyTimings:
    """093: timings sub-stage de un assembly individual.

    Devuelto por :meth:`PdfAssembler.assemble_traced`. Los campos no
    relevantes para el camino tomado quedan en 0.0 (no None — facilita
    el agregado por el orquestador sin manejo especial).

    ``path_kind`` identifica el camino tomado:

    * ``"native_pdf"`` — :meth:`_passthrough_native_pdf_traced`
      (``shutil.copy2`` del PDF fuente)
    * ``"paged_img2pdf"`` — :meth:`_assemble_paged_traced` con el
      fast-path img2pdf
    * ``"paged_fallback"`` — :meth:`_assemble_paged_traced` con el
      fallback Pillow + PyPDF2
    """

    path_kind: str
    # Camino native_pdf:
    source_stat_ms: float = 0.0  # Path.is_file() del fuente
    copy_native_ms: float = 0.0  # shutil.copy2 (usualmente el bulk)
    # Camino paged_*:
    discover_pages_ms: float = 0.0  # glob + sort de las páginas
    encode_pdf_ms: float = 0.0  # img2pdf.convert o el fallback
    # Común a ambos:
    dst_stat_ms: float = 0.0  # stat del staged para size_bytes


# ---------------------------------------------------------------------------
# Assembler
# ---------------------------------------------------------------------------


class PdfAssembler(IAssembler):
    """Implementación concreta de :class:`IAssembler` para la etapa S4."""

    def __init__(self, config: AssemblerConfig) -> None:
        self._cfg = config
        self.temp_dir = self._resolve_temp_dir(config.temp_dir)
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    # ----------------------------------------------------------- API pública

    def assemble(self, document: RVABREPDocument) -> StagedFile:
        """Convierte *document* en un único PDF staged.

        Wrapper sin instrumentación. Para profiling, usar
        :meth:`assemble_traced` (093) que devuelve además los timings
        sub-stage.
        """
        staged, _ = self.assemble_traced(document)
        return staged

    def assemble_traced(self, document: RVABREPDocument) -> tuple[StagedFile, AssemblyTimings]:
        """093: como :meth:`assemble` pero retorna además los timings
        por sub-stage (source_stat, copy/discover, encode, dst_stat).

        Cada sub-stage se mide con ``time.perf_counter()`` y se devuelve
        en ms. Los timings se loguean en el orquestador via
        ``MetricsRecorder.record_stage(stage="S4.<sub>")`` y aparecen
        en el ``batch_summary`` con el resto de las etapas, lo que
        permite que ``cmcourier diagnose`` los muestre.
        """
        if self._cfg.synthetic_provider is not None and not document.is_pdf:
            # 102 REQ-008: el modo sintético exige un RVABREP PDF-only.
            raise PDFAssemblyFailedError(
                txn_num=document.txn_num,
                reason=(
                    "synthetic content mode requires a PDF-only RVABREP; "
                    "regenerate it with `mock rvabrep --image-mix pdf:100`"
                ),
            )
        if document.is_pdf:
            return self._passthrough_native_pdf_traced(document)
        return self._assemble_paged_traced(document)

    # ----------------------------------------------------------- internos

    @staticmethod
    def _resolve_temp_dir(configured: Path) -> Path:
        if str(configured).strip().lower() in _ONEDRIVE_TRAP_VARIANTS:
            return Path(tempfile.gettempdir()) / _DIVERTED_DIR_NAME
        return configured

    def _passthrough_native_pdf_traced(
        self, doc: RVABREPDocument
    ) -> tuple[StagedFile, AssemblyTimings]:
        """093: PDF nativo con timings sub-stage.

        102: con un ``synthetic_provider`` configurado, el PDF se genera
        on-the-fly y no se lee ningún archivo fuente.
        """
        if self._cfg.synthetic_provider is not None:
            return self._generate_synthetic_traced(doc, self._cfg.synthetic_provider)
        src = self._cfg.source_root / doc.image_path / doc.file_name
        t0 = time.perf_counter()
        if not src.is_file():
            raise SourceFileMissingError(file_path=str(src))
        source_stat_ms = (time.perf_counter() - t0) * 1000.0

        dst = self.temp_dir / f"{doc.txn_num}.pdf"
        t1 = time.perf_counter()
        shutil.copy2(src, dst)
        copy_ms = (time.perf_counter() - t1) * 1000.0

        t2 = time.perf_counter()
        size_bytes = dst.stat().st_size
        dst_stat_ms = (time.perf_counter() - t2) * 1000.0

        staged = StagedFile(path=dst, size_bytes=size_bytes, page_count=doc.total_pages)
        timings = AssemblyTimings(
            path_kind="native_pdf",
            source_stat_ms=source_stat_ms,
            copy_native_ms=copy_ms,
            dst_stat_ms=dst_stat_ms,
        )
        return staged, timings

    def _generate_synthetic_traced(
        self, doc: RVABREPDocument, provider: SyntheticPdfProvider
    ) -> tuple[StagedFile, AssemblyTimings]:
        """102: genera un PDF sintético on-the-fly en el dir de staging.

        No lee ningún archivo fuente. El dir de staging puede estar
        montado sobre RAM (tmpfs) para velocidad de memoria: el código
        escribe normal, el operador elige el filesystem (REQ-005)."""
        dst = self.temp_dir / f"{doc.txn_num}.pdf"
        t0 = time.perf_counter()
        dst.write_bytes(provider.generate(doc.txn_num))
        gen_ms = (time.perf_counter() - t0) * 1000.0
        size_bytes = dst.stat().st_size
        staged = StagedFile(path=dst, size_bytes=size_bytes, page_count=doc.total_pages)
        timings = AssemblyTimings(path_kind="synthetic_pdf", copy_native_ms=gen_ms)
        return staged, timings

    def _assemble_paged_traced(self, doc: RVABREPDocument) -> tuple[StagedFile, AssemblyTimings]:
        """093: TIFF/JPEG paginado con timings sub-stage."""
        t0 = time.perf_counter()
        pages = self._discover_pages(doc)
        discover_ms = (time.perf_counter() - t0) * 1000.0

        output = self.temp_dir / f"{doc.txn_num}.pdf"
        used_fallback = False
        t1 = time.perf_counter()
        try:
            self._try_img2pdf(pages, output)
        except Exception as primary:  # noqa: BLE001 — img2pdf levanta una superficie amplia
            _log.info(
                "assembler: img2pdf fast path failed, falling back",
                extra={"txn_num": doc.txn_num, "reason": str(primary)},
            )
            used_fallback = True
            try:
                self._fallback_pillow_pypdf2(pages, output)
            except Exception as secondary:
                raise PDFAssemblyFailedError(
                    txn_num=doc.txn_num,
                    reason=f"img2pdf and fallback both failed: {secondary!r}",
                ) from secondary
        encode_ms = (time.perf_counter() - t1) * 1000.0

        t2 = time.perf_counter()
        size_bytes = output.stat().st_size
        dst_stat_ms = (time.perf_counter() - t2) * 1000.0

        staged = StagedFile(path=output, size_bytes=size_bytes, page_count=len(pages))
        timings = AssemblyTimings(
            path_kind="paged_fallback" if used_fallback else "paged_img2pdf",
            discover_pages_ms=discover_ms,
            encode_pdf_ms=encode_ms,
            dst_stat_ms=dst_stat_ms,
        )
        return staged, timings

    def _discover_pages(self, doc: RVABREPDocument) -> list[Path]:
        source_dir = self._cfg.source_root / doc.image_path
        file_code = doc.file_name.split(".")[0]
        pattern = f"{file_code}.*"
        candidates = [p for p in source_dir.glob(pattern) if _is_numeric_ext(p.suffix.lstrip("."))]
        if not candidates:
            raise SourceFileMissingError(file_path=str(source_dir / pattern))
        candidates.sort(key=lambda p: int(p.suffix.lstrip(".")))
        if len(candidates) != doc.total_pages:
            _log.warning(
                "assembler: page count mismatch",
                extra={
                    "txn_num": doc.txn_num,
                    "expected": doc.total_pages,
                    "discovered": len(candidates),
                },
            )
        return candidates

    @staticmethod
    def _try_img2pdf(pages: list[Path], output: Path) -> None:
        pdf_bytes = img2pdf.convert([str(p) for p in pages])
        if not pdf_bytes:
            raise RuntimeError("img2pdf returned empty bytes")
        output.write_bytes(pdf_bytes)

    @staticmethod
    def _fallback_pillow_pypdf2(pages: list[Path], output: Path) -> None:
        merger = PdfMerger()
        try:
            for page in pages:
                with Image.open(page) as img:
                    rgb = img.convert("RGB") if img.mode != "RGB" else img
                    buf = BytesIO()
                    rgb.save(buf, format="PDF")
                    buf.seek(0)
                    merger.append(buf)
            with output.open("wb") as out:
                merger.write(out)
        finally:
            merger.close()


# ---------------------------------------------------------------------------
# Helpers a nivel de módulo
# ---------------------------------------------------------------------------


def _is_numeric_ext(text: str) -> bool:
    return bool(text) and text.isdigit()
