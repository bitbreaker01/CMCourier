# 132 — Pausa cooperativa y re-autenticación CMIS en caliente

## Por qué

Cuando la sesión CMIS expira a mitad de una corrida larga, hoy el
uploader hace UN re-warmup con las MISMAS credenciales
(`cmis_uploader.py:_post_with_retries`, `auth_retried`) y al segundo 401
lanza `CMISClientError(401)`: el doc se marca `S5_FAILED` como
`http_4xx`, y el siguiente, y el siguiente — cada worker quema su doc
contra una sesión muerta hasta que el operador cancela con `x` (drain) y
reanuda el batch desde `[7]`. En streaming ni siquiera hay resume: lo que
estaba en el bucket se descarta como `skipped`.

La causa raíz es que `CancellationToken` (097) es el ÚNICO canal de
control entre la UI y los workers y es de un solo sentido: no hay forma
de decirle al pipeline "frená, esperá, seguí".

## Qué

**REQ-001 — Compuerta de pausa en el token.** `CancellationToken` suma
`pause()`, `resume()`, `is_paused()` y `checkpoint() -> bool`:
`checkpoint` BLOQUEA mientras la corrida esté pausada y devuelve `False`
si fue cancelada (antes o durante la espera), `True` si puede seguir.
`cancel()` sigue siendo de un solo sentido y despierta a los que esperan
en la compuerta. Los diez sitios de poll (`_stage_s0_s1`, `_s2_one`,
`_s3_one`, `_s4_one`, `_upload_one`, los loops de chunks de
`MultiBatchOrchestrator` y el `_prep_loop` de `StreamingOrchestrator`)
pasan de `is_cancelled()` a `checkpoint()`: un worker pausado se queda
parado ANTES de tomar trabajo nuevo (y antes del slot del semáforo); lo
que ya está en vuelo termina. Nada se descarta.

**REQ-002 — Credenciales en caliente.** `IUploader` gana
`set_credentials(username, password)`. `CmisUploader` reemplaza el auth
del `httpx.Client`, limpia la cookie jar (`JSESSIONID`) y marca la sesión
fría bajo `_warm_lock`, así el próximo POST hace el warmup con las
credenciales nuevas. `StagedPipeline.set_cmis_credentials(u, p)` delega.

**REQ-003 — Auto-pausa al 401.** `StagedPipeline.set_auth_expired_handler(cb)`
registra un callback opcional. En `_upload_one`, un `CMISClientError`
con `status_code == 401` y handler registrado: (1) pausa el token,
(2) invoca el handler UNA vez por episodio (no una vez por worker),
(3) espera en `checkpoint()`, (4) si la corrida se reanudó, reintenta el
upload del MISMO doc con las credenciales nuevas; si se canceló, el doc
se marca `S5_FAILED` como hoy. Como máximo dos episodios por doc: al
tercer 401 consecutivo se rinde y marca fallido. Sin handler (CLI
headless, TUI clásica) el comportamiento es el de hoy.

**REQ-004 — Consola.** `ConsoleRunManager` suma `pause()`, `resume()`,
`paused` y `reauth_pending`. El handler de 401 marca `reauth_pending` y
llama `app.on_auth_expired(manager)` en el thread de la UI: notificación
de error, salto a `[2] CREDENCIALES` con la pista "sesión CMIS
rechazada — corrida PAUSADA". En `[6] MONITOR`: `p` pausa (con
confirmación), `r` reanuda; si `reauth_pending`, `resume()` empuja
`creds.get("cmis")` al pipeline antes de soltar la compuerta, y si esa
credencial no fue probada OK pide confirmación. El header del monitor
muestra `PAUSADA` / `PAUSADA · esperando credenciales CMIS`; la barra
superior muestra `⏸ pausada`. `outcome()` no cambia.

**REQ-005 — Guía.** `docs/how-to/probar-la-consola.md`: sección "Pausa y
re-autenticación" con el flujo 401 → `[2]` → `r`; la viñeta se va de
"Qué NO hace todavía".

## Escenarios

**E1 —** `token.pause()`; un thread llama `checkpoint()` y queda
bloqueado; `resume()` lo libera con `True`. `cancel()` mientras está
pausado lo libera con `False`.

**E2 —** Pipeline pausado: `_upload_one` no llama al uploader hasta que
alguien hace `resume()`; después sube y devuelve `"done"`. Con
`cancel()` en lugar de `resume()` devuelve `"skipped"` sin subir.

**E3 —** Uploader devuelve 401 dos veces; con handler registrado el
pipeline queda pausado, el handler se llamó una vez aunque dos workers
hayan chocado con el 401, y tras `set_cmis_credentials` + `resume()` el
doc se sube con éxito (`"done"`) y `uploader.set_credentials` recibió la
credencial nueva. Sin handler: `"failed"` como hoy.

**E4 —** `CmisUploader.set_credentials("nuevo", "clave")`: el próximo
POST va precedido de un GET de warmup y ambos llevan
`Authorization: Basic base64(nuevo:clave)`; la cookie de la sesión vieja
no viaja.

**E5 —** Consola con corrida activa: `p` en `[6]` abre confirmación y
pausa; el header dice `PAUSADA`; `r` reanuda. `on_auth_expired` cambia a
`[2]` y muestra la pista; `r` con `reauth_pending` y cmis no probado abre
confirmación; confirmado, empuja la credencial y reanuda.

## Notas de implementación

- La compuerta es un segundo `threading.Event` ("running", seteado por
  defecto) dentro del mismo token. `cancel()` setea AMBOS eventos: los
  que esperan despiertan y ven `False`.
- El gate en `_upload_one` queda ANTES del preflight y del slot del
  semáforo (igual que el chequeo de cancelación de 097): un worker
  pausado no retiene presupuesto de concurrencia.
- El reintento del 401 ocurre DENTRO del slot del semáforo y del
  `StageTimer` de S5: el doc ya pasó preflight y claim; reintentar ahí es
  correcto (mismo record, misma reserva).
- `DeadlineWatchdog` NO se pausa: si `--max-duration` vence durante la
  pausa, la corrida se cancela y los docs pendientes quedan para resume.
  Documentado en la guía.
- Los orquestadores multi-batch y streaming acceden al token vía
  `getattr(self._pipeline, "cancel_token", None)` (dobles de test); el
  `checkpoint()` se hace sólo si el token existe.
