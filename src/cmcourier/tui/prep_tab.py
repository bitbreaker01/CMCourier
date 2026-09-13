"""Renderer del tab PREP (025 fase 3)."""

from __future__ import annotations

__all__ = ["render_prep"]

from cmcourier.tui.data_provider import PREP_STAGES, TUISnapshot

_STAGE_LABELS: dict[str, str] = {
    "S0": "TRIGGER",
    "S1": "INDEXING",
    "S2": "MAPPING",
    "S3": "METADATA",
    "S4": "ASSEMBLY",
}


def render_prep(snap: TUISnapshot, *, width: int = 76) -> str:
    """Construye el cuerpo del tab PREP como un string multi-línea."""
    lines: list[str] = []
    for stage in PREP_STAGES:
        info = snap.stages.get(stage, {})
        count = int(info.get("count", 0))
        p50 = float(info.get("p50_ms", 0.0))
        p95 = float(info.get("p95_ms", 0.0))
        bar = _bar(count, _stage_target(snap, stage), width=28)
        lines.append(
            f"  {stage} {_STAGE_LABELS.get(stage, stage):8}  {bar}  "
            f"{count:>6}  p50 {p50:>7.1f} ms  p95 {p95:>7.1f} ms"
        )
    lines.append("")
    # 051 lo creó para UNA causa (fila RVABREP con código de baja) y la
    # etiqueta decía "deleted at source". 148 metió en el mismo balde a
    # EXCLUDED_BY_FILTER, SOURCE_ROW_INCOMPLETE y OUT_OF_SCOPE_RESUME sin
    # tocar el cartel, y el operador leyó que sus 22618 documentos estaban
    # borrados en el origen cuando en realidad su propia lista de códigos
    # los dejaba afuera. El contador vive en memoria y NO tiene desglose
    # por razón: la etiqueta no puede afirmar una causa que no conoce, así
    # que nombra el balde y manda al censo, que sí la tiene.
    lines.append(f"  EXCLUIDOS (S1 · la razón, en `batch show`)  {snap.s1_filtered:>6}")
    lines.append("")
    lines.append(" SLOW OPS (PREP, top 5)")
    prep_slow = [
        op
        for op in snap.slow_ops_all
        if str(op.get("stage", "")).startswith(("S1", "S2", "S3", "S4"))
    ]
    if not prep_slow:
        lines.append("    (none yet)")
    else:
        for op in prep_slow[:5]:
            stage = str(op.get("stage", "?"))
            txn = str(op.get("txn_num", "?"))
            dms_val = op.get("duration_ms", 0.0)
            rank_val = op.get("rank", 0)
            dms = float(dms_val) if isinstance(dms_val, (int, float)) else 0.0
            rank = int(rank_val) if isinstance(rank_val, (int, float)) else 0
            lines.append(f"  {rank}  {stage:12}  {txn:<14}  {dms:>10,.0f} ms")
    return "\n".join(lines)


def _bar(value: int, target: int, *, width: int) -> str:
    if target <= 0:
        return " " * width
    ratio = max(0.0, min(1.0, value / target))
    filled = int(round(ratio * width))
    return "█" * filled + "░" * (width - filled)


def _stage_target(snap: TUISnapshot, stage: str) -> int:
    """Cota superior best-effort para la barra de progreso — el conteo
    más grande visto entre S0..S5 (ya que S0 es el total de triggers)."""
    counts = [int(s.get("count", 0)) for s in snap.stages.values()]
    return max(counts, default=0)
