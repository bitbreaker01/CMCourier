# 108 — Métricas de stage acotadas: ventana deslizante, cache de summary y fix del race

## Por qué

El hallazgo dominante de la auditoría de rendimiento, en
`observability/metrics.py`:

### A. `_StageBucket` guarda TODAS las muestras y las ordena completas en cada summary

`durations_ms` (`metrics.py:74`) solo hace `append` — nunca se poda.
`summary()` (`:81-99`) copia la lista entera bajo el lock del bucket y
la ordena. Consumidores por tick del TUI (0.25 s):

* `stages_snapshot()` ×2 (`data_provider.py:253,261`) → `summary()` de
  ~10 buckets (S0..S5 + sub-stages de S4).
* `current_stage_p95` (`data_provider.py:302`) → tercera ordenación de
  S5.

= ~13 pasadas O(n·log n) por segundo. En modo **batched** n queda
acotado por chunk (`start_batch` resetea los buckets). En modo
**streaming** el recorder es único para toda la corrida
(`streaming.py:386`): con 200k docs son ~200k floats por bucket,
ordenados 13 veces por segundo — segundos de CPU por tick, la copia se
hace **sosteniendo el lock que `record_stage` necesita** (los workers de
S5 se bloquean detrás del TUI), y la memoria crece sin techo
(O(total_docs × n_stages)).

### B. El p95 que pilotea el AIMD es acumulativo

`current_stage_p95_with_count` (`:537-550`) resume TODAS las muestras
desde el inicio. Tras miles de uploads el p95 histórico es
estadísticamente inmóvil: una degradación real de CMIS tarda una
eternidad en mover la aguja y el controller queda clavado en `noop`. El
AIMD necesita ver el comportamiento **reciente**, no el promedio de la
corrida.

### C. Race: los snapshots iteran `_stage_buckets` sin lock

`stages_snapshot` (`:656`) y `_build_summary` (`:601`) iteran el dict
desde el thread del TUI / orchestrator mientras `record_stage` (`:527`)
hace `setdefault` desde N workers. El primer `S4.encode_pdf` de la
corrida durante una iteración → `RuntimeError: dictionary changed size
during iteration`.

## Qué

### Requisitos

**REQ-001 — Ventana deslizante para percentiles.** `_StageBucket` pasa a
`collections.deque(maxlen=2048)`: p50/p95/p99 se calculan sobre las
últimas 2048 muestras. Memoria por bucket acotada (~16 KiB) e
independiente del tamaño de la corrida; el costo de ordenar queda fijo.
2048 muestras dan un p95 con precisión de sobra para el TUI y el AIMD.

**REQ-002 — `count` y `sum_ms` siguen siendo acumulativos.** Contadores
`count`/`sum_ms` corren aparte de la ventana: el `batch_summary`, el
`analyze` y el `diagnose` (que calculan fracciones de tiempo total por
stage sobre `sum_ms`) no cambian de semántica. Solo los percentiles son
ventana.

**REQ-003 — Cache del summary.** `summary()` cachea su resultado y solo
recomputa si entraron muestras nuevas desde el último cálculo. Las 3
lecturas por tick del TUI sobre un bucket quieto no ordenan nada.

**REQ-004 — El dict de buckets se protege.** `record_stage` (setdefault)
y todos los snapshots (`stages_snapshot`, `_build_summary`,
`current_stage_p95*`, `start_batch`) sincronizan el acceso a
`_stage_buckets` con un lock propio del recorder; la iteración se hace
sobre una copia tomada bajo ese lock.

**REQ-005 — El AIMD ve p95 reciente.** Sin cambios en `auto_tune.py`:
`current_stage_p95_with_count` devuelve el p95 de la ventana (reciente
por construcción) y el `count` acumulativo (la señal de
`insufficient_data` no cambia).

### Fuera de alcance

* Renderizar solo el tab activo del TUI y la cadencia del DETAIL — va
  en un cambio de TUI separado.
* El logging por documento del hot path (hallazgo E) y el threshold de
  progress events (F) — cambios separados.

## Escenarios

**E1 — Memoria acotada en streaming.**
Dado un bucket que recibe 100 000 muestras,
cuando se consulta su summary,
entonces retiene a lo sumo 2048 muestras, `count == 100000`, `sum_ms`
es la suma total, y los percentiles reflejan las muestras recientes.

**E2 — El p95 sigue al presente.**
Dado 5000 muestras de 100 ms seguidas de 3000 muestras de 9000 ms,
cuando se lee `current_stage_p95_with_count("S5")`,
entonces el p95 refleja ~9000 ms (la ventana está llena de muestras
recientes), no el histórico diluido.

**E3 — Summary cacheado.**
Dado un bucket sin muestras nuevas,
cuando `summary()` se llama repetidamente,
entonces el resultado se computa una sola vez (mismas lecturas, cero
ordenaciones extra).

**E4 — Sin race en el snapshot.**
Dado N threads registrando stages con nombres nuevos mientras otro
thread llama `stages_snapshot()` en loop,
cuando corren concurrentemente,
entonces no se levanta `RuntimeError` y el snapshot siempre es un dict
consistente.

**E5 — Compatibilidad de shape.**
Dado cualquier consumidor existente (`batch_summary`, `analyze`,
`diagnose`, TUI),
cuando lee un summary,
entonces las claves `count/p50_ms/p95_ms/p99_ms/sum_ms` están presentes
con los mismos tipos que pre-108.
