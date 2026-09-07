# 134 — Tasa por ventana deslizante y ETA de corrida en el monitor

## Por qué

`docs/s` en la cabecera de `[6]` es `completados / elapsed` desde el
inicio de la corrida. Después de una pausa (132), un cambio de techo
(133) o un ajuste del AIMD, ese promedio tarda minutos en reflejar la
realidad: el operador baja workers y el número casi no se mueve. Y no
hay ETA de corrida — sólo la del `chunk` activo (041).

## Qué

**REQ-001 — Ventana deslizante.** `TUIDataProvider` guarda muestras
`(monotonic, procesados)` en cada `snapshot()` y expone
`throughput_window_docs_per_s: float | None` sobre las muestras de los
últimos `window_s` (default 60 s). `None` mientras no haya dos muestras
separadas por ≥ 1 s. "Procesados" = resultados terminales sumados sobre
`chunks_state` (`s5_done + s5_failed + upload_skipped + prep_failed +
prep_filtered + prep_skipped`); sin `chunks_state` (path monolítico de
resume) cae a `pool.completed + pool.failed`. El acumulado
`throughput_docs_per_s` se mantiene como está.

**REQ-002 — ETA de corrida.** `TUIDataProvider(planned_total=...)` recibe
el `total` de `[5]` (o `--total`). `eta_run_s: float | None` =
`(planned_total - procesados) / tasa_ventana` cuando hay total, tasa
> 0 y quedan docs; si no, `None`. `planned_total` se expone en el
snapshot para la cabecera. Sin total, la ETA es imposible por diseño:
los triggers se traen por olas y la fuente puede tener 20M filas.

**REQ-003 — Cabecera de `[6]`.** La línea de conteo pasa a
`<acum> docs/s · <ventana> docs/s (60 s) · ETA <h:mm:ss> de <total>`.
Sin ventana todavía: `— docs/s (60 s)`. Sin total: `ETA — (sin total)`.
Con corrida completa: sin ETA. La guía documenta el comportamiento en
pausa (la ventana decae a 0 en ≤ 60 s y la ETA pasa a `—`).

## Escenarios

**E1 —** Provider con reloj inyectable: muestras a t=0 (0 docs), t=10
(20), t=70 (80) → ventana 60 s: entre t=10 y t=70 → 1.0 docs/s; acumulado
80/70 ≈ 1.14.

**E2 —** Con `planned_total=200`, procesados 80, ventana 1.0 docs/s →
`eta_run_s == 120`. Con `planned_total=None` → `None`. Con procesados ≥
total → `None`.

**E3 —** Una sola muestra → `throughput_window_docs_per_s is None`;
dos muestras separadas 0.2 s → sigue `None`.

**E4 —** Cabecera del monitor con snapshot sintético: contiene `docs/s
(60 s)` y `ETA 0:02:00 de 200`; sin total contiene `ETA — (sin total)`.

## Notas de implementación

- Las muestras se toman en `snapshot()`, que en la consola corre cada
  0.25 s sólo con `[6]` visible. Si el operador está en otra pestaña la
  ventana tiene huecos: la tasa se calcula entre la muestra más vieja
  dentro de la ventana y la última, así que sigue siendo `Δdocs/Δt`
  real, sólo que sobre menos puntos.
- Se inyecta `clock: Callable[[], float] = time.monotonic` para que los
  tests no duerman.
- La TUI clásica (`cli/app.py`) también recibe `planned_total` desde
  `--total`; su footer no cambia en esta spec.
