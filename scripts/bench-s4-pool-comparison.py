"""POC de benchmark — compara ProcessPool vs ThreadPool vs Serial para S4.

Genera N archivos sintéticos (PDFs nativos chicos, PDFs grandes,
TIFFs paginados), corre el assembler en tres modos y reporta:

- Wall time total
- Throughput (docs/seg, MB/seg)
- Latencia por doc (avg, p50, p95)
- CPU usage promedio (si hay ``psutil``)

Uso::

    python scripts/bench-s4-pool-comparison.py
    python scripts/bench-s4-pool-comparison.py --workers 30 --runs 3
    python scripts/bench-s4-pool-comparison.py --workload small  # solo pdfs chicos
"""

from __future__ import annotations

import argparse
import gc
import shutil
import statistics
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path

try:
    import psutil  # type: ignore[import-not-found]

    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cmcourier.adapters.assembly.pdf_assembler import AssemblerConfig, PdfAssembler
from cmcourier.adapters.assembly.pool import _pool_assemble, _pool_init
from cmcourier.domain.models import RVABREPDocument

# ----------------------------------------------------------------------
# Generación de archivos sintéticos
# ----------------------------------------------------------------------


def _make_synthetic_pdf(target_bytes: int) -> bytes:
    """PDF mínimo válido, pad hasta ``target_bytes`` con un comentario."""
    header = b"%PDF-1.4\n"
    body = b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    body += b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
    body += (
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
        b" /Resources <<>> /Contents 4 0 R >>\nendobj\n"
    )
    body += (
        b"4 0 obj\n<< /Length 44 >>\nstream\n"
        b"BT /F1 12 Tf 100 700 Td (Hello) Tj ET\nendstream\nendobj\n"
    )
    xref = b"xref\n0 5\n0000000000 65535 f \n"
    trailer = b"trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n0\n%%EOF\n"
    base = header + body + xref + trailer
    if len(base) >= target_bytes:
        return base
    padding_needed = target_bytes - len(base) - 4
    padding = b"%" + b"X" * padding_needed + b"\n"
    return header + body + padding + xref + trailer


def _make_synthetic_tiff(target_bytes_per_page: int, page_count: int) -> list[bytes]:
    """Genera N páginas TIFF de aprox ``target_bytes_per_page`` cada una."""
    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError:
        # Fallback: TIFF mínimo si no hay Pillow.
        return [_make_synthetic_pdf(target_bytes_per_page) for _ in range(page_count)]

    pages = []
    # Tamaño aproximado en pixels para llegar al target_bytes.
    side = max(64, int((target_bytes_per_page / 3) ** 0.5))
    for _ in range(page_count):
        img = Image.new("RGB", (side, side), color=(220, 220, 220))
        buf = BytesIO()
        img.save(buf, format="TIFF")
        data = buf.getvalue()
        # Pad si es necesario.
        if len(data) < target_bytes_per_page:
            data += b"\0" * (target_bytes_per_page - len(data))
        pages.append(data)
    return pages


@dataclass(frozen=True)
class Fixture:
    document: RVABREPDocument
    source_files_bytes: int  # tamaño total en disco


def _make_pdf_fixture(
    source_root: Path,
    txn_num: str,
    target_bytes: int,
) -> Fixture:
    aba_icd = "001"
    aba_jcd = "0001"
    rel_dir = Path(aba_icd) / aba_jcd
    abs_dir = source_root / rel_dir
    abs_dir.mkdir(parents=True, exist_ok=True)
    file_name = f"{txn_num}.PDF"
    file_path = abs_dir / file_name
    pdf_bytes = _make_synthetic_pdf(target_bytes)
    file_path.write_bytes(pdf_bytes)
    doc = RVABREPDocument(
        system_code="1",
        txn_num=txn_num,
        index1="JUANPEREZ01",
        index2="123456",
        index3="",
        index4="",
        index5="",
        index6="",
        index7="FF17",
        image_type="O",  # PDF
        image_path=str(rel_dir),
        file_name=file_name,
        creation_date=datetime(2026, 5, 18),
        last_view_date=None,
        total_pages=1,
        delete_code="",
    )
    return Fixture(document=doc, source_files_bytes=len(pdf_bytes))


def _make_tiff_fixture(
    source_root: Path,
    txn_num: str,
    target_bytes_per_page: int,
    page_count: int,
) -> Fixture:
    aba_icd = "001"
    aba_jcd = "0001"
    rel_dir = Path(aba_icd) / aba_jcd
    abs_dir = source_root / rel_dir
    abs_dir.mkdir(parents=True, exist_ok=True)
    pages = _make_synthetic_tiff(target_bytes_per_page, page_count)
    total_bytes = 0
    base = txn_num
    for i, page_data in enumerate(pages, start=1):
        suffix = f".{i:03d}"
        page_path = abs_dir / f"{base}{suffix}"
        page_path.write_bytes(page_data)
        total_bytes += len(page_data)
    file_name = f"{base}.001"
    doc = RVABREPDocument(
        system_code="1",
        txn_num=txn_num,
        index1="JUANPEREZ01",
        index2="123456",
        index3="",
        index4="",
        index5="",
        index6="",
        index7="FF17",
        image_type="B",  # TIFF
        image_path=str(rel_dir),
        file_name=file_name,
        creation_date=datetime(2026, 5, 18),
        last_view_date=None,
        total_pages=page_count,
        delete_code="",
    )
    return Fixture(document=doc, source_files_bytes=total_bytes)


# ----------------------------------------------------------------------
# Workloads
# ----------------------------------------------------------------------


def _build_workload(name: str, source_root: Path, n: int) -> list[Fixture]:
    """Genera N documentos según el perfil pedido."""
    fixtures: list[Fixture] = []
    if name == "small":
        # Todos PDFs nativos chicos (~100 KB) — caso "muchos archivos chicos".
        for i in range(n):
            fixtures.append(_make_pdf_fixture(source_root, f"S{i:06d}", 100 * 1024))
    elif name == "large":
        # Todos PDFs nativos grandes (~5 MB) — caso "archivos pesados".
        for i in range(n):
            fixtures.append(_make_pdf_fixture(source_root, f"L{i:06d}", 5 * 1024 * 1024))
    elif name == "tiff":
        # TIFFs paginados (3 páginas × ~200 KB) — caso CPU-bound puro.
        for i in range(n):
            fixtures.append(_make_tiff_fixture(source_root, f"T{i:06d}", 200 * 1024, 3))
    elif name == "mixed":
        # 70% PDFs chicos, 20% PDFs grandes, 10% TIFFs — caso real productivo.
        for i in range(n):
            mod = i % 10
            if mod < 7:
                fixtures.append(_make_pdf_fixture(source_root, f"M{i:06d}", 100 * 1024))
            elif mod < 9:
                fixtures.append(_make_pdf_fixture(source_root, f"M{i:06d}", 5 * 1024 * 1024))
            else:
                fixtures.append(_make_tiff_fixture(source_root, f"M{i:06d}", 200 * 1024, 3))
    else:
        raise ValueError(f"workload desconocido: {name!r}")
    return fixtures


# ----------------------------------------------------------------------
# Modos de ejecución
# ----------------------------------------------------------------------


@dataclass
class RunResult:
    mode: str
    workers: int
    workload: str
    docs: int
    total_bytes: int
    wall_seconds: float
    per_doc_ms: list[float]

    def throughput_docs_per_sec(self) -> float:
        return self.docs / self.wall_seconds if self.wall_seconds > 0 else 0.0

    def throughput_mb_per_sec(self) -> float:
        if self.wall_seconds <= 0:
            return 0.0
        return (self.total_bytes / (1024 * 1024)) / self.wall_seconds

    def latency_stats(self) -> tuple[float, float, float]:
        if not self.per_doc_ms:
            return (0.0, 0.0, 0.0)
        sorted_ms = sorted(self.per_doc_ms)
        avg = statistics.fmean(sorted_ms)
        p50 = sorted_ms[len(sorted_ms) // 2]
        p95 = sorted_ms[int(len(sorted_ms) * 0.95)] if len(sorted_ms) > 1 else sorted_ms[-1]
        return (avg, p50, p95)


def _run_serial(assembler: PdfAssembler, fixtures: list[Fixture]) -> RunResult:
    per_doc: list[float] = []
    start = time.perf_counter()
    for fx in fixtures:
        t0 = time.perf_counter()
        assembler.assemble(fx.document)
        per_doc.append((time.perf_counter() - t0) * 1000)
    wall = time.perf_counter() - start
    return RunResult(
        mode="serial",
        workers=1,
        workload="",
        docs=len(fixtures),
        total_bytes=sum(fx.source_files_bytes for fx in fixtures),
        wall_seconds=wall,
        per_doc_ms=per_doc,
    )


def _run_thread_pool(assembler: PdfAssembler, fixtures: list[Fixture], workers: int) -> RunResult:
    per_doc: list[float] = []
    lock_start = time.perf_counter()

    def _one(fx: Fixture) -> float:
        t0 = time.perf_counter()
        assembler.assemble(fx.document)
        return (time.perf_counter() - t0) * 1000

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_one, fixtures))
    wall = time.perf_counter() - start
    per_doc.extend(results)
    _ = lock_start  # silenciar linter
    return RunResult(
        mode="threadpool",
        workers=workers,
        workload="",
        docs=len(fixtures),
        total_bytes=sum(fx.source_files_bytes for fx in fixtures),
        wall_seconds=wall,
        per_doc_ms=per_doc,
    )


def _run_process_pool(assembler: PdfAssembler, fixtures: list[Fixture], workers: int) -> RunResult:
    # NOTA: replica el patrón productivo del orchestrator —
    # los workers se inicializan vía ``_pool_init`` con el config
    # del assembler. Cada submit corre ``_pool_assemble(doc)`` en
    # el subprocess, que usa el assembler-singleton del worker.
    per_doc: list[float] = []
    start = time.perf_counter()
    with ProcessPoolExecutor(
        max_workers=workers,
        initializer=_pool_init,
        initargs=(
            AssemblerConfig(
                source_root=assembler._cfg.source_root,
                temp_dir=assembler.temp_dir,
            ),
        ),
    ) as pool:
        futures = [(fx, pool.submit(_pool_assemble, fx.document)) for fx in fixtures]
        for _fx, fut in futures:
            t0 = time.perf_counter()
            fut.result()
            per_doc.append((time.perf_counter() - t0) * 1000)
    wall = time.perf_counter() - start
    return RunResult(
        mode="processpool",
        workers=workers,
        workload="",
        docs=len(fixtures),
        total_bytes=sum(fx.source_files_bytes for fx in fixtures),
        wall_seconds=wall,
        per_doc_ms=per_doc,
    )


# ----------------------------------------------------------------------
# Setup + impresión
# ----------------------------------------------------------------------


def _make_assembler(source_root: Path, temp_dir: Path) -> PdfAssembler:
    return PdfAssembler(AssemblerConfig(source_root=source_root, temp_dir=temp_dir))


def _print_header(workload: str, n: int, workers: int, runs: int) -> None:
    print(f"\n{'=' * 72}")
    print(f"  Workload: {workload}  |  Docs: {n}  |  Workers: {workers}  |  Runs: {runs}")
    if _HAS_PSUTIL:
        print(
            f"  CPU: {psutil.cpu_count(logical=False)} físicos / "
            f"{psutil.cpu_count(logical=True)} lógicos"
        )
    print(f"{'=' * 72}")


def _print_result(label: str, runs: list[RunResult]) -> None:
    walls = [r.wall_seconds for r in runs]
    docs_per_sec = [r.throughput_docs_per_sec() for r in runs]
    mb_per_sec = [r.throughput_mb_per_sec() for r in runs]
    all_per_doc = [ms for r in runs for ms in r.per_doc_ms]
    avg_lat = statistics.fmean(all_per_doc) if all_per_doc else 0.0
    p50 = statistics.median(all_per_doc) if all_per_doc else 0.0
    p95 = sorted(all_per_doc)[int(len(all_per_doc) * 0.95)] if len(all_per_doc) > 1 else 0.0

    print(f"\n  {label}:")
    print(
        f"    Wall avg: {statistics.fmean(walls):7.3f} s  "
        f"(min {min(walls):7.3f}, max {max(walls):7.3f})"
    )
    print(
        f"    Docs/s avg: {statistics.fmean(docs_per_sec):7.1f}  "
        f"MB/s avg: {statistics.fmean(mb_per_sec):6.1f}"
    )
    print(f"    Latency  avg {avg_lat:6.1f} ms  p50 {p50:6.1f} ms  p95 {p95:6.1f} ms")


def _print_comparison(
    serial: list[RunResult], thread: list[RunResult], proc: list[RunResult]
) -> None:
    s_wall = statistics.fmean(r.wall_seconds for r in serial)
    t_wall = statistics.fmean(r.wall_seconds for r in thread)
    p_wall = statistics.fmean(r.wall_seconds for r in proc)
    print("\n  Speedup vs Serial:")
    print(f"    ThreadPool:  {s_wall / t_wall:5.2f}x")
    print(f"    ProcessPool: {s_wall / p_wall:5.2f}x")
    print("  ThreadPool vs ProcessPool:")
    if t_wall < p_wall:
        print(f"    ThreadPool gana — {p_wall / t_wall:5.2f}x más rápido que ProcessPool")
    else:
        print(f"    ProcessPool gana — {t_wall / p_wall:5.2f}x más rápido que ThreadPool")


def _cleanup_dirs(*dirs: Path) -> None:
    for d in dirs:
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="POC: ProcessPool vs ThreadPool para S4")
    parser.add_argument("--docs", type=int, default=100, help="docs por run (default 100)")
    parser.add_argument(
        "--workers", type=int, default=10, help="workers para los pools (default 10)"
    )
    parser.add_argument(
        "--runs", type=int, default=3, help="cuántas veces correr cada modo (default 3)"
    )
    parser.add_argument(
        "--workload",
        choices=["small", "large", "tiff", "mixed", "all"],
        default="all",
        help="perfil de archivos (default 'all' corre los 4 perfiles)",
    )
    args = parser.parse_args()

    workloads = ["small", "large", "tiff", "mixed"] if args.workload == "all" else [args.workload]

    for wl in workloads:
        base = Path(tempfile.mkdtemp(prefix="cmcourier-bench-"))
        source_root = base / "source"
        temp_dir = base / "temp"
        source_root.mkdir()
        temp_dir.mkdir()

        try:
            fixtures = _build_workload(wl, source_root, args.docs)
            total_mb = sum(fx.source_files_bytes for fx in fixtures) / (1024 * 1024)
            assembler = _make_assembler(source_root, temp_dir)

            _print_header(wl, args.docs, args.workers, args.runs)
            print(f"  Total dataset: {total_mb:.1f} MB")

            serial_runs: list[RunResult] = []
            thread_runs: list[RunResult] = []
            proc_runs: list[RunResult] = []

            for _ in range(args.runs):
                # warm-up dir limpia entre runs.
                _cleanup_dirs(temp_dir)
                temp_dir.mkdir(exist_ok=True)
                gc.collect()
                serial_runs.append(_run_serial(assembler, fixtures))

                _cleanup_dirs(temp_dir)
                temp_dir.mkdir(exist_ok=True)
                gc.collect()
                thread_runs.append(_run_thread_pool(assembler, fixtures, args.workers))

                _cleanup_dirs(temp_dir)
                temp_dir.mkdir(exist_ok=True)
                gc.collect()
                proc_runs.append(_run_process_pool(assembler, fixtures, args.workers))

            _print_result("Serial      ", serial_runs)
            _print_result("ThreadPool  ", thread_runs)
            _print_result("ProcessPool ", proc_runs)
            _print_comparison(serial_runs, thread_runs, proc_runs)
        finally:
            _cleanup_dirs(base)

    print("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
