# 125 — Consola, Fase 3: monitor en vivo + batches operables

## Por qué

F1 (shell/credenciales/doctor) y F2 (launcher/overrides/lock/auditoría)
dejaron dos placeholders: MONITOR y BATCHES. F3 los completa reusando
la infraestructura de la TUI clásica (`tui/` renderers +
`TUIDataProvider`) y el tracking store real. Alcance según el MoSCoW
del informe UX v2 (Must de F3), declinando explícitamente lo que pide
código nuevo profundo (pausa/re-auth en caliente, techo manual de
workers dentro del AIMD).

## Qué

**REQ-001 — MONITOR en vivo.** Cuando hay una corrida activa
(`run_manager.provider`), el tab MONITOR:
- refresca en un `set_interval` (4 Hz) leyendo `provider.snapshot()`;
- **solo renderiza cuando el tab está activo** (lección spec 110);
- cabecera con progreso: subidos / fallidos (con desglose por tipo vía
  `failures_by_type` del snapshot) / throughput / elapsed / estado;
- reusa los renderers existentes `render_prep`, `render_upload` y
  (en streaming) `render_bucket` dentro de `Static`, en sub-áreas;
- marca el cuello de botella sobre los `stages` del snapshot;
- `x` cancela con drain **quedándose en la consola** (no `exit()`) para
  mostrar el resumen de cierre — a diferencia del `CMCourierTUI` clásico
  que hace `app.exit()`.

**REQ-002 — Resumen de cierre.** Al terminar (`on_run_finished`, ya
existe de F2), el MONITOR muestra una tarjeta: outcome, subidos,
fallidos, salteados, docs/s promedio, y accesos: ver en BATCHES /
reintentar fallidos (si hay).

**REQ-003 — BATCHES operable.** `DataTable` (nunca botones en celda —
Textual no los soporta) alimentada por `list_batches()` con las
columnas de auditoría (operador, entorno, outcome). Navegación:
`↑↓` mueve la fila, `enter` abre el detalle (`get_batch_details`),
`R` reintenta fallidos (`retry_failed` + ruteo al launcher en modo
resume), `E` informa el export. Filtro por texto. Reanudable calculado
por `FAILED + PENDING > 0` (no por `status`).

**REQ-004 — Detalle de batch.** Bajo la tabla: la línea de auditoría
persistida + la lista de `failed_records` (txn, status, error).

### Declinado con motivo (Won't-now del informe)

- Pausa + re-autenticación en caliente: `CancellationToken` es one-way;
  la salida honesta es cancelar (drain) + reanudar. El monitor lo dice.
- Techo manual de workers en caliente: requiere `min(user_cap,
  aimd_cap)` dentro de `AutoTuneController` (código nuevo, no cableado).
- Campos nuevos en `TUISnapshot` (`total_expected`, ventana de docs/s):
  el monitor usa lo que el provider ya expone; ETA por ventana queda
  para una iteración posterior. Se documenta la limitación en la UI.

## Escenarios

**E1 —** Con corrida activa (manager fake con provider), el MONITOR
renderiza la cabecera de progreso y no explota.
**E2 —** `x` en MONITOR con corrida activa abre el confirm de cancelar.
**E3 —** BATCHES lista los batches del tracking con sus columnas de
auditoría; `↑↓` mueve la selección; `enter` abre el detalle.
**E4 —** `R` sobre un batch con fallidos abre el confirm de retry.
**E5 —** Un batch sin FAILED+PENDING no es reanudable (R avisa).
