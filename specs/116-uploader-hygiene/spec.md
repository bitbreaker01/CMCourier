# 116 — Higiene del uploader: burst cap, cobro por bytes reales y timeouts granulares

## Por qué

Tres hallazgos medios de la auditoría, todos en
`adapters/upload/cmis_uploader.py`:

### A. El `TokenBucket` acumula tokens sin techo (P7)

`consume` (`cmis_uploader.py:166`) hace
`self._tokens += elapsed * self._rate` sin cap. Tras una pausa (una
fase de PREP larga, un hueco entre chunks), el bucket acumula tokens
ilimitados y el primer tramo de uploads sale **sin throttling** — el
límite de red configurado se viola exactamente cuando más importa
(la ráfaga post-pausa contra una red corporativa compartida).

### B. `BandwidthLimiter.read` cobra por lo pedido, no por lo leído (P18)

`read` (`:182-185`) consume `chunk_size` tokens ANTES de leer; una
lectura corta o el EOF cobran tokens por bytes que nunca viajaron
(y `size=-1` cobra 1 MiB fijo). El throughput real queda sesgado por
debajo del configurado.

### C. `httpx.Timeout(300)` uniforme (P9)

Un solo float aplica a connect, read, write y pool
(`:242`, más el override por request). Un host caído tarda **5
minutos** en fallar el connect en vez de segundos; un slot de pool
agotado espera 5 minutos. El read/write largo sí es legítimo (uploads
grandes sobre redes lentas).

### D. El docstring del módulo miente

`cmis_uploader.py:19-21` promete "creación recursiva de carpetas con
cache en memoria" — no existe ni el cache ni la creación en las ~1080
líneas del módulo (solo `verify_folder_exists`, read-only, para el
doctor).

## Qué

**REQ-001 — Burst cap de 1 segundo.** Los tokens acumulados se capean a
`rate × 1.0` (un segundo de presupuesto). La ráfaga post-pausa queda
acotada a 1 s del límite configurado. El arranque (tokens en 0) y la
tasa sostenida no cambian.

**REQ-002 — Cobro por bytes leídos.** `BandwidthLimiter.read` lee
primero y consume `len(data)` tokens — lecturas cortas y EOF cobran lo
justo. (El pacing sigue ocurriendo antes de devolver el chunk al
encoder, así que el throttle es equivalente.)

**REQ-003 — Reloj inyectable.** `TokenBucket` acepta `clock`/`sleep`
inyectables para tests determinísticos (default `time.monotonic` /
`time.sleep`).

**REQ-004 — Timeouts granulares.** El connect se capea a 10 s
(`min(10, timeout_configurado)`); read/write/pool conservan el timeout
configurado (y el ajuste en vivo del AIMD vía `_timeout_s`). Aplica
tanto al cliente como al override por request.

**REQ-005 — Docstring sincerado.** El módulo describe lo que hay:
verificación read-only de carpetas + recuperación idempotente de 409.

## Escenarios

**E1 — Post-pausa acotado.** Bucket a 1 MB/s idle 100 s → puede
consumir a lo sumo ~1 MB sin dormir; el segundo MB paga la espera.

**E2 — EOF barato.** Un `read` que devuelve 10 KB de un pedido de 1 MiB
consume 10 KB de tokens.

**E3 — Connect corto.** El cliente y los requests anuncian
`connect ≤ 10 s` con read/write en el valor configurado.

**E4 — Tasa sostenida intacta.** El test de 081 (1 MB a 8 Mbps ≈ 1 s)
sigue pasando.
