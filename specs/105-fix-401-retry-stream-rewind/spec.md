# 105 — Fix: el retry de 401 no rebobina el stream del multipart

## Por qué

En `_post_with_retries` (`cmis_uploader.py:804`), el rebobinado del
stream es condicional al contador de intentos:

```python
# cmis_uploader.py:834-835
if real_attempts > 0:
    stream.seek(0)
```

Pero el path del 401 (`cmis_uploader.py:910-914`) hace `continue` **sin
incrementar `real_attempts`** — decisión correcta en sí misma (el
re-warmup de sesión no debe consumir presupuesto de retries, spec 060),
con una consecuencia no vista: si el 401 llega en el **primer** intento
(`real_attempts == 0`), el reintento arranca con el file handle en EOF.

La cadena del fallo:

1. Primer POST consume el stream completo (el `MultipartEncoder` lo lee
   hasta EOF para transmitir el body).
2. El server responde 401 (sesión vencida / JSESSIONID inválido).
3. `continue` sin `seek(0)`. El nuevo `MultipartEncoder` calcula el
   `Content-Length` vía `total_len()` → `os.fstat(fileno())` = tamaño
   **completo** del archivo (el `fileno()` delegado de `BandwidthLimiter`
   lo habilita).
4. El POST anuncia N bytes pero el file part rinde 0 bytes → el server
   queda esperando el resto del body hasta el read timeout.

Resultado: **hasta 300 s de cuelgue por documento** (el
`timeout_seconds` default) cada vez que la sesión CMIS expira en medio
de una corrida — exactamente el momento en que expiran *todas* las
sesiones de los N workers a la vez.

## Qué

Rebobinar el stream al inicio de **todo** reintento, sin importar qué
path lo disparó.

### Requisitos

**REQ-001 — Rebobinado incondicional en reintentos.** El stream se
rebobina (`seek(0)`) antes de construir el encoder en cualquier
iteración del loop que no sea la primera — tanto si el reintento vino
por `RequestError`/5xx (que incrementan `real_attempts`) como por 401
(que no).

**REQ-002 — La semántica del presupuesto no cambia.** El 401 sigue sin
consumir presupuesto de retries (`real_attempts` no se incrementa) y
sigue permitido una sola vez (`auth_retried`).

**REQ-003 — El body del reintento post-401 está completo.** El segundo
POST transmite el archivo entero, con `Content-Length` consistente con
lo transmitido.

### Fuera de alcance

* El cap de burst del `TokenBucket` y el timeout uniforme de 300 s
  (hallazgos P7/P9 de la auditoría) — cambios separados.

## Escenarios

**E1 — 401 en el primer intento.**
Dado un server que responde 401 al primer POST y 201 al segundo,
cuando se sube un documento,
entonces ambos POSTs transmiten el body completo (el archivo aparece
íntegro en ambos) y el upload termina en éxito.

**E2 — 401 seguido de 500.**
Dado 401 → 500 → 201,
cuando se sube un documento,
entonces los tres POSTs transmiten el body completo y el presupuesto de
retries consumido es 1 (solo el 500).

**E3 — Comportamiento pre-105 intacto para 5xx.**
Dado 500 → 201,
cuando se sube un documento,
entonces el comportamiento es idéntico al actual (ya cubierto por
`TestRetryRebuildsEncoder`).
