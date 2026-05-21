# 097 — Cancelación cooperativa del pipeline desde el TUI

## Por qué

El operador reportó: al apretar **"q"** con la corrida en progreso, el
TUI se cierra pero **el pipeline sigue corriendo**; solo Ctrl+C
repetido lo frena del todo.

No es un bug puntual — es una **feature que falta**. El rastreo del
código lo confirma:

```python
# cli/_tui_runner.py:85-93
worker = threading.Thread(target=_worker, name="cmcourier-pipeline", daemon=False)
worker.start()
app = CMCourierTUI(data_provider)
try:
    app.run()              # TUI en el thread principal
finally:
    worker.join()          # al salir del TUI, ESPERA al pipeline
```

* `"q"` dispara el `action_quit` de textual (`tui/app.py:49`,
  `Binding("q", "quit", "Quit")`) → cierra `app.run()`.
* El `finally: worker.join()` **bloquea el thread principal** esperando
  que el pipeline termine **solo** — el pipeline nunca recibe señal de
  "pará".
* `rg cancel|abort|stop_event|should_stop` en `orchestrators/` →
  **cero resultados**. No existe ningún mecanismo de cancelación.

Ctrl+C necesita varios golpes porque el worker thread y los
`ThreadPoolExecutor` de S5 son `daemon=False`; SIGINT solo llega al
thread principal y Python no cierra el proceso con threads non-daemon
vivos.

> ❌ **No-solución descartada**: `daemon=True` en el worker mataría
> los threads en seco — a mitad de un INSERT a SQLite o de un upload
> CMIS. Corrompe el tracking DB / deja uploads parciales. El
> `daemon=False` es deliberado y correcto.

## Qué

Un mecanismo de **cancelación cooperativa**: un token que la "q"
prende (previa confirmación) y que los orchestrators chequean en
puntos seguros para frenar ordenadamente (**drain**: para de tomar
trabajo nuevo, deja terminar lo que ya está en vuelo).

### `CancellationToken` (nuevo)

`services/cancellation.py` — wrapper fino sobre `threading.Event`:

```python
class CancellationToken:
    def cancel(self) -> None: ...
    def is_cancelled(self) -> bool: ...
```

### Flujo de la "q"

1. `"q"` con la corrida **completa** → quit normal (pre-097).
2. `"q"` con la corrida **en progreso** → abre un `ModalScreen` de
   confirmación: *"¿Cancelar la corrida? [s/n]"*.
   * `n` → cierra el modal, la corrida sigue.
   * `s` → `cancel_token.cancel()` + `app.exit()`. El
     `worker.join()` del runner ahora espera solo el **drain** (los
     uploads en vuelo terminan — segundos), y el proceso sale limpio.

Esto evita matar una migración productiva por apretar una tecla sin
querer, y convierte la "q" en una salida real en vez de un cuelgue.

### Cambios

1. **`services/cancellation.py`** (nuevo) — `CancellationToken`.

2. **`StagedPipeline`** — `__init__` crea `self._cancel_token`
   (siempre); property pública `cancel_token`. Chequeos cooperativos:
   * `_stage_s0_s1`: `break` del loop de triggers si está cancelado.
   * `_s2_one` / `_s3_one` / `_s4_one`: si cancelado al entrar →
     devuelve `(None, False)` (saltea el doc, no cuenta como falla).
   * `_upload_one`: si cancelado al entrar → devuelve `"skipped"`
     sin subir. Los docs ya en vuelo (pasado el chequeo) terminan
     solos — eso ES el drain.

3. **`MultiBatchOrchestrator` / `StreamingOrchestrator`** — exponen
   `cancel_token` delegando al `StagedPipeline` interno. Sus loops
   propios (chunk loop / iteración de triggers de los producers)
   chequean el token para dejar de tomar trabajo nuevo.

4. **`cli/_tui_runner.py`** — toma `orchestrator.cancel_token` y se lo
   pasa al `CMCourierTUI`.

5. **`tui/app.py`** — `CMCourierTUI.__init__` recibe el
   `cancel_token`; `action_quit` override + `ModalScreen` de
   confirmación (`tui/confirm_screen.py`, nuevo).

### Tests

* `tests/unit/services/test_cancellation.py`: el token.
* `tests/unit/orchestrators/test_staged_cancellation.py`: con el token
  cancelado, `_upload_one` → `"skipped"`, `_sN_one` → `(None, False)`,
  el loop de S0/S1 corta. Sin cancelar → comportamiento byte-idéntico.
* `tests/unit/tui/test_quit_confirmation.py`: `"q"` con corrida en
  progreso abre el modal; `s` prende el token; `n` no; `"q"` con
  corrida completa sale directo.

## Criterios de aceptación

1. `"q"` con la corrida completa → sale igual que pre-097.
2. `"q"` con la corrida en progreso → modal de confirmación.
3. Confirmar la cancelación frena el pipeline en **segundos** (drain)
   y el proceso sale limpio — sin necesidad de Ctrl+C.
4. El tracking SQLite queda consistente: los docs no procesados quedan
   pendientes para un resume; ninguno a medio escribir.
5. Sin cancelación, el comportamiento es byte-idéntico a pre-097.
6. `pytest -m unit` pasa.

## Riesgos

* **El drain espera el upload en vuelo más lento**: si un doc de
  50 MB está a mitad de subida, el drain lo espera. Es correcto por
  diseño (drain ≠ abort) — pero el operador puede ver unos segundos
  de espera tras confirmar. Aceptable.
* **Ctrl+C sigue siendo el camino duro**: 097 NO cambia el manejo de
  SIGINT — solo arregla la "q". Ctrl+C queda como el abort de
  emergencia. Un cambio futuro podría cablear un signal handler al
  mismo `cancel_token`.
* **Superficie amplia**: el token se chequea en los per-doc methods
  de `StagedPipeline`, que los tres orchestrators comparten — un
  chequeo mal puesto afecta a los tres modos. Mitigado con tests por
  modo.

## Notas

- El chequeo cooperativo va al **inicio** de cada método per-doc, no
  en el medio — así un doc nunca queda a mitad de stage. Atomicidad
  por-stage preservada.
- `CancellationToken` se crea siempre (no solo en modo TUI); en
  corridas headless simplemente nunca se prende. Esto evita ramas
  `if token is not None` desparramadas.
- Relacionado: cambio 025 (TUI + worker pool), 063 (streaming),
  028 (multi-batch).
