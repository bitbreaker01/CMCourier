# 103 — Control de tiempo de ejecución del pipeline (`--max-duration`)

## Por qué

El plan de stress define pruebas **acotadas por tiempo**:

* T03 — escalones de 30 a 60 minutos cada uno.
* T04 — soak de 8 a 24 horas.
* T05 — estrés de 1 a 3 horas.

Hoy el pipeline no tiene forma de auto-detenerse por tiempo. Lo único
que existe es `--total N`, un tope por **cantidad de triggers**
(`app.py:136`), no por reloj. Consecuencias:

* Para T04 (soak desatendido de 24h) alguien tendría que esperar 24
  horas frente al TUI para apretar "q".
* La metodología de T03 — correr el proceso repetidamente con distintos
  `cmis.workers` — necesita que **cada** corrida tenga un corte de
  tiempo limpio y desatendido.

La maquinaria de frenado **ya existe**. El spec 097 introdujo el
`CancellationToken` + el *drain* cooperativo: los orchestrators chequean
`is_cancelled()` en puntos seguros (`staged.py:630,790,835,929,1159`) y,
al verlo prendido, dejan de tomar trabajo nuevo y esperan que lo
in-flight termine. Hoy ese token lo prende **solo** la tecla "q" del TUI
(`tui/app.py:142`).

Verificación clave del wiring:

```
# staged.py:225  — el orchestrator SIEMPRE crea el token
self._cancel_token = CancellationToken()
```

El token existe en toda corrida, con TUI o headless. Los orchestrators
ya lo chequean. **Lo único que falta es una causa de cancelación
disparada por reloj de pared.**

## Qué

Una opción `--max-duration` que, transcurrido un tiempo de pared desde
el arranque del pipeline, **prende el `CancellationToken` existente** —
el mismo *drain* ordenado que dispara "q", sin TUI de por medio.

### Requisitos

**REQ-001 — Opción `--max-duration`.** Acepta una duración humana:
`30m`, `2h`, `90m`, `1h30m`, `45s`. Se parsea a segundos (módulo de
parseo análogo a `parse_size` de `sizing.py`). Ausente = sin límite de
tiempo (comportamiento idéntico a pre-103). Se agrega a los cuatro
comandos `... -pipeline run` (csv-trigger, rvabrep, local-scan,
single-doc), igual que `--total`.

**REQ-002 — El deadline prende el `CancellationToken`.** Al vencer el
plazo se prende el token del orchestrator. Drain cooperativo, **NO**
kill: lo in-flight (uploads CMIS, INSERTs a SQLite) termina
ordenadamente. Reusa íntegro el camino de 097.

> ❌ **No-solución descartada:** matar el proceso (`SIGKILL` /
> `sys.exit` abrupto) al vencer el plazo. Corromper el tracking DB o
> dejar uploads parciales en el destino es exactamente lo que 097
> evita. El deadline **reusa** el drain, no lo puentea.

**REQ-003 — Funciona headless.** El watchdog de deadline es un thread
daemon que no depende del TUI. Es el caso de uso principal: T04 (soak
desatendido) corre con `--no-tui`.

**REQ-004 — Salida limpia y distinguible.** Al frenar por deadline el
run:

* persiste el estado parcial del batch (ya lo hace el drain de 097);
* emite un evento estructurado `pipeline_stopped_by_deadline` con
  `start_ts`, `deadline_ts`, `stop_ts` y `docs_completed`;
* imprime un resumen final que dice explícitamente "detenido por
  `--max-duration`", distinto de "completó todo el trabajo";
* el **exit code se mantiene en 0** — frenar por tiempo no es un error,
  es lo que se pidió. La distinción "se acabó el tiempo" vs "terminó
  todo" va por el log/resumen, no por el exit code (no se rompe el
  contrato de códigos 0/2/3).

**REQ-005 — El deadline es tiempo de pared desde el arranque del
pipeline**, contado **después** del doctor pre-flight (no se le cuenta
el pre-flight al presupuesto de prueba). Se loguea hora de arranque,
deadline calculado y hora real de detención.

**REQ-006 — `--max-duration` y `--total` coexisten.** El que se cumpla
primero gana. Ambos disparan el mismo drain.

**REQ-007 — El watchdog es inofensivo si el pipeline termina antes.**
Cancela una sola vez; `CancellationToken.cancel()` ya es idempotente
(`cancellation.py:34`). Si el corpus se agota antes del deadline, el
watchdog dispara sobre un pipeline ya cerrado sin efecto.

### Fuera de alcance

* Pausar / reanudar interactivo. Esto es solo "frená a las X horas".
* `--drain-timeout` (techo a cuánto espera el drain a los in-flight). Si
  más adelante un upload gigante colgado estira el drain, se evalúa
  aparte. Por ahora el drain espera lo necesario.
* Pin de concurrencia / apagado de `auto_tune` — **descartado**:
  `auto_tune.enabled` ya es `false` por default (`schema.py:477`); la
  concurrencia se pinea variando `cmis.workers` sin tocar código.

## Escenarios

**E1 — Deadline en corrida headless.**
Dado `--no-tui --max-duration 30m`,
cuando pasan 30 minutos,
entonces el pipeline drena, persiste el parcial y sale con código 0 y el
resumen "detenido por --max-duration".

**E2 — Drain ordenado, no kill.**
Dado que el deadline vence con 8 uploads in-flight,
cuando se prende el token,
entonces esos 8 uploads terminan (éxito o fallo registrado) antes de que
el proceso salga; ninguno queda a medio escribir en el destino.

**E3 — Termina antes del deadline.**
Dado `--max-duration 4h` y un corpus que se agota en 1h,
cuando el pipeline termina,
entonces sale normalmente y el watchdog dispara sin efecto.

**E4 — `--total` gana al tiempo.**
Dado `--max-duration 2h --total 1000`,
cuando se procesan 1000 triggers en 20 minutos,
entonces el run frena por `--total`, no espera las 2h.

**E5 — Deadline gana a `--total`.**
Dado `--max-duration 30m --total 999999999`,
cuando pasan 30 minutos,
entonces frena por deadline.

**E6 — Sin la opción, comportamiento intacto.**
Dado un run sin `--max-duration`,
cuando corre,
entonces no se crea watchdog y el comportamiento es idéntico a pre-103.
