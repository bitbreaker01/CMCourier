# 115 — Lanes: restitución de capacidad y dispatcher sin head-of-line blocking

## Por qué

Dos hallazgos de la auditoría sobre heavy/light lanes:

### A. El rebalance es de un solo sentido — la lane drenada queda en 1

`rebalance_tick` (`lane_controller.py:240-247`): una lane vacía
`idle_threshold_s` (15 s) migra su capacidad a la otra y queda con el
piso de 1. Eso es correcto **mientras siga vacía** (el slot no se usa).
El problema: cuando la lane vuelve a tener trabajo, `set_queue_depth`
con depth > 0 limpia el sello de vacío (`:165-169`) pero **no
restituye la capacidad**. La única vía de vuelta es que la OTRA lane
quede vacía 15 s — algo que con un flujo continuo de items no pasa
nunca. Resultado real: un hueco transitorio de 15 s sin docs heavy
(normal en un corpus mixto) deja la lane heavy procesando de a 1
durante el resto de la corrida.

Colateral: `set_total_budget` preserva el ratio *actual* — si el AIMD
redimensiona durante la migración, el ratio degenerado (1/51) queda
horneado.

### B. El dispatcher de streaming bloquea en una lane y mata de hambre a la otra

`_dispatcher_loop` (`streaming.py:754-779`): un único thread hace
`heavy_queue.put(item)` **bloqueante**. Si la cola heavy está llena
(los uploads heavy son lentos por definición), el dispatcher queda
clavado — no drena el bucket principal ni alimenta a los consumers
light, aunque estén todos ociosos. Combinado con A (heavy en cap 1),
el estancamiento es probable, no teórico.

## Qué

### Requisitos

**REQ-001 — Restitución al llegar trabajo.** El controller recuerda qué
lane fue drenada (`_migrated_from`). Cuando `set_queue_depth` reporta
depth > 0 para esa lane, la distribución se restituye inmediatamente al
split inicial (`_initial_split(total, heavy_initial_ratio)`) — sin
esperar ningún tick. La migración por drenaje (tests 036 existentes) no
cambia.

**REQ-002 — Dispatcher con overflow acotado por lane.** El dispatcher
rutea con `put_nowait`; si la cola de la lane está llena, el item va a
un buffer de overflow local (deque) acotado a `bucket_size`. En cada
iteración intenta drenar los overflows primero. Solo cuando el overflow
de una lane está lleno el dispatcher espera en esa lane — y aun ahí
sigue drenando el overflow de la otra en cada reintento. El poison pill
espera a que ambos overflows se vacíen antes de propagarse (ningún item
se pierde).

**REQ-003 — Back-pressure global intacta.** El bucket principal sigue
acotado; el buffering extra total está acotado por 2×`bucket_size`
items (metadatos, no bytes de archivo).

## Escenarios

**E1 — Restitución.** heavy 2/light 8 tras drenaje de heavy (1/10);
cuando llega un item heavy (`set_queue_depth("heavy", 1)`),
entonces la distribución vuelve a 2/8 sin esperar al tick.

**E2 — Head-of-line.** Cola heavy llena y sin consumers; items light
detrás en el bucket,
cuando el dispatcher corre,
entonces los items light llegan a su cola igual.

**E3 — Sin pérdida con poison.** Items en overflow al llegar el poison
→ todos los items se entregan antes que las pills.

**E4 — Migración por drenaje intacta.** Los escenarios 036 existentes
no cambian.
