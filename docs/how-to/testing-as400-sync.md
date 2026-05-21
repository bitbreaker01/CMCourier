# How to: Probar el sync AS400 NIARVILOG en staging

> [← Volver al índice](../INDEX.md) · [How-to](README.md)

> Guía de QA / operación para validar el cambio **034** (idempotencia
> distribuida AS400) contra un entorno **no productivo**. Si lo que
> buscás es el contrato de la feature — mapping de columnas, semántica
> de estados, perillas — leé primero [as400-sync.md](as400-sync.md).
> Esta guía asume que ya lo entendiste y te dice **cómo ejercitarlo**.

---

## Qué vas a probar — y qué NO

El sync AS400 tiene dos planos. Esta guía cubre **solo el operacional**:

| Plano | Cubierto acá | Dónde está |
|---|---|---|
| Lógica de claim / resolvers con `pyodbc` fakeado | ❌ | Suite: `tests/integration/cli/test_sync.py`, `test_as400_niarvilog.py` |
| Conectividad real + drills de conflicto + `sync resolve` | ✅ | Esta guía |

Necesitás un **DB2-for-i alcanzable** con una tabla `NIARVILOG` real
(la de staging del banco, o una réplica tuya). No hay forma de fakear
esto desde el CLI — el `mock` genera RVABREP, **no** filas NIARVILOG.

> ⚠️ **Gotcha #1 — no existe resolución en bulk.** El docstring del
> módulo `sync.py` menciona `sync resolve --all`. **No está
> implementado.** El comando `resolve` exige un `TRNNUM` posicional:
> resolvés **un txn por vez**. Si tu plan de prueba dependía de un
> bulk-resolve, ajustá expectativas.

---

## Pre-requisitos

### 1. Credenciales en el entorno

Igual que el trigger AS400 — viven en env vars, nunca en el YAML:

```bash
export AS400_USERNAME=...
export AS400_PASSWORD=...
```

### 2. Bloque `as400_sync` en el config de staging

El `config-staging.yaml.template` **no trae** el bloque `as400_sync`
—el dry-run estándar corre solo con SQLite—. Agregalo a mano:

```yaml
tracking:
  db_path: sample/staging-tracking.db
  as400_sync:
    enabled: true
    connection:
      host: as400-staging.example
      port: 446
      database: RVILIB
      driver: "iSeries Access ODBC Driver"
    library: RVILIB
    table: NIARVILOG
    # columns: { ... }   # solo si tu NIARVILOG usa nombres no canónicos
    stale_in_progress_minutes: 30
    retry_attempts: 3
    retry_base_delay_s: 5.0
```

> 💡 **Tip de staging:** bajá `stale_in_progress_minutes` a `1` mientras
> probás. Así el cleanup pre-flight te recicla filas `I` pegadas en
> minutos en vez de en media hora — clave para los drills de abajo.

---

## Fase 0 — Gate de conectividad (`doctor`)

Antes de cualquier corrida, validá la conexión y que la tabla exista:

```bash
cmcourier doctor --config config-staging.yaml --check as400_sync
```

Qué hace por dentro: conecta al AS400 y dispara una probe barata
`SELECT 1 FROM RVILIB.NIARVILOG WHERE 1=0` (cero filas, solo valida
schema). Resultados posibles:

| Salida | Significado |
|---|---|
| `PASS — AS400 NIARVILOG reachable at …` | Todo bien, seguí. |
| `SKIP — disabled` | Te olvidaste `enabled: true`. |
| `FAIL — credentials missing` | Falta `AS400_USERNAME` / `AS400_PASSWORD`. |
| `FAIL` (otro) | Host/tabla/driver mal, o AS400 caído. Frená acá. |

**No avances a la Fase 1 sin un `PASS`.** Es así de fácil.

---

## Fase 1 — Happy path: claim y transición a `O`

Objetivo: confirmar que una corrida limpia escribe NIARVILOG correcto.

1. Reseteá el estado local (ver [Reset entre corridas](#reset-entre-corridas)).
2. **Vaciá tu scope en NIARVILOG** — borrá las filas de los `TRNNUM`
   que vas a migrar, o que arranquen en `STSCOD='N'`. Si quedan en
   `O`, la corrida los saltea y no probás nada.
3. Corré el pipeline normal contra staging.
4. Mientras corre / al terminar, verificá cada fila:

```sql
SELECT TRNNUM, STSCOD, OBJIDN, NUMREI, EERRMSG
FROM RVILIB.NIARVILOG
WHERE TRNNUM IN ('0001234', '0001235', ...);
```

**Criterio de aceptación:**

- Cada doc subido → `STSCOD='O'` con `OBJIDN` = el object id de CMIS.
- Cada doc fallido → `STSCOD='F'`, `EERRMSG` con el motivo, `NUMREI` ≥ 1.
- Ninguna fila quedó pegada en `I` después de que la corrida terminó.

Recordá el orden de escritura: **SQLite primero, AS400 segundo**. SQLite
es el anchor de resume; si ves SQLite `S5_DONE` pero AS400 sin `O`, eso
es un conflicto legítimo — justo lo que probás en la Fase 3.

---

## Fase 2 — Provocar conflictos a propósito

Un conflicto = los dos stores no acuerdan sobre "¿está hecho este doc?".
El pre-flight los detecta y **aborta el pipeline con exit 2** si hay
alguno. Para probar la resolución, primero tenés que fabricar uno.

### Conflicto tipo "Imported" (AS400 adelante de SQLite)

AS400 dice `O`, SQLite no tiene la fila. Lo más fácil de simular:

1. Corré una migración completa (Fase 1) → NIARVILOG queda en `O`.
2. **Borrá el SQLite local** con `wipe-local-state.sh` — pero **no**
   toques AS400.
3. Volvé a correr el pipeline sobre el mismo scope.

→ El pre-flight ve AS400 `O` + SQLite vacío → lo registra como
*Imported*. Esto **no aborta** — el resume in-process saltea el doc.

### Conflicto tipo "Conflict" real (el que aborta)

SQLite dice `uploaded`, AS400 dice `N`/`I`/`F`. Para fabricarlo:

1. Corré una migración (Fase 1) → SQLite `S5_DONE`, AS400 `O`.
2. **A mano, en AS400**, pisá una fila a un estado no terminal:

```sql
UPDATE RVILIB.NIARVILOG SET STSCOD='F', OBJIDN='', EERRMSG='drill'
WHERE TRNNUM='0001234';
```

3. Volvé a correr el pipeline.

→ El pre-flight ve SQLite `uploaded` + AS400 `F` → **Conflict**.
Con conflictos no vacíos, **el pipeline aborta con exit 2** antes de
procesar nada. Ese exit 2 es el comportamiento correcto, no un bug.

---

## Fase 3 — Resolver conflictos (`cmcourier sync`)

Con el conflicto de la Fase 2 sobre la mesa, esta es la rutina.

### Paso 1 — Inspeccionar (read-only)

```bash
cmcourier sync status --config config-staging.yaml
# sync status: stale_cleaned=2
```

Corre el `cleanup_stale_in_progress` (recicla filas `I` viejas a `N`)
y reporta cuántas reseteó. **No resuelve conflictos** — solo limpia
filas pegadas e informa. Si el AS400 está caído, sale con **exit 3**.

### Paso 2 — Resolver, opción A: preferir AS400 (lo más común)

AS400 tiene el estado autoritativo `O` y querés alinear lo local:

```bash
cmcourier sync resolve 0001234 --prefer-as400 --config config-staging.yaml
# resolved 0001234: imported AS400 state — STSCOD='O', OBJIDN='cm-abc-xyz'
```

> ⚠️ **Gotcha #2 — `--prefer-as400` NO escribe SQLite.** Solo
> **imprime** el estado de AS400. Para cerrar el loop, **re-corré el
> pipeline con `--resume`**: la lógica in-process ve AS400 `O` y
> saltea el doc limpio. Si esperabas que el SQLite se actualizara
> solo, no pasa — es intencional (034 no quiso extender la API de
> `ITrackingStore` solo para esto).

Exit codes de este camino:
- Exit `1` → el txn no está en AS400, o su `STSCOD` no es `O`
  (solo las filas `O` tienen `OBJIDN` para importar).
- Exit `3` → error de AS400.

### Paso 2 — Resolver, opción B: preferir local (raro)

SQLite subió el doc pero AS400 perdió el update (ej. AS400 estaba
caído durante S5). Empujás el `cm_object_id` a AS400:

```bash
cmcourier sync resolve 0001234 \
  --prefer-local \
  --cm-object-id cm-abc-xyz \
  --config config-staging.yaml
# resolved 0001234: pushed local cm_object_id='cm-abc-xyz' to AS400.
```

> ⚠️ **Gotcha #3 — `--cm-object-id` es obligatorio con
> `--prefer-local`.** No lo adivina. Sacalo de
> `cmcourier batch show <batch_id>`. El UPDATE **solo dispara si la
> fila ya existe** en NIARVILOG; si falta, re-corré el pipeline para
> que `try_claim` la inserte.

Exit codes:
- Exit `2` → te olvidaste `--cm-object-id`, o pasaste los dos
  `--prefer-*` juntos (o ninguno).
- Exit `1` → la fila no existe en AS400, o el UPDATE no tocó
  exactamente 1 fila.
- Exit `3` → error de AS400.

### Paso 3 — Verificar el cierre

Después de resolver + re-correr con `--resume`, repetí el `SELECT` de
la Fase 1. El pipeline no debe volver a abortar por conflicto.

---

## Fase 4 — Concurrencia: doble claim

Esto valida la atomicidad a nivel DB2 — el caso de uso central de la
feature (migrador Java paralelo del banco, multi-workstation).

1. Preparás un scope chico con filas en `STSCOD='N'`.
2. Lanzás **dos corridas del pipeline en paralelo** sobre el **mismo
   scope** (dos terminales, dos workstations, o dos procesos).
3. Revisás los logs de observabilidad.

**Criterio de aceptación:**

- Cada doc se sube **una sola vez**. Cero doble-upload en CMIS.
- El proceso que pierde la race loguea `as400_claim_lost` y saltea
  el doc — no crashea.
- En NIARVILOG, cada `TRNNUM` termina con **una** fila en `O`.

El mecanismo: `UPDATE … SET STSCOD='I' WHERE PK AND STSCOD='N'`. El
que recibe `rowcount=1` gana; el que recibe `0` intenta `INSERT` y
come un `IntegrityError` → claim perdido. El `IntegrityError`
**nunca** se retrya — es la señal de detección de race.

---

## Fase 5 — Resiliencia: AS400 intermitente

Valida el retry/backoff ante errores transient.

1. Arrancá una corrida.
2. A mitad de camino, cortá la red al AS400 (firewall, baja la VPN,
   o pausá el contenedor DB2 si tenés una réplica local).
3. Observá los logs.

**Qué esperar:**

- Un `pyodbc.OperationalError` dispara retry: `retry_attempts`
  intentos (default 3), backoff `retry_base_delay_s * 2^(n-1)`
  capeado a 5 min → secuencia default **5s, 10s, 20s**.
- Si restaurás la red dentro de la ventana → la corrida se recupera
  sola.
- Si los reintentos se agotan → `As400UnreachableError` y el
  **pipeline aborta con exit 2**.

> 💡 Para iterar rápido sin esperar 35s, bajá `retry_base_delay_s` a
> `1.0` en el config de staging.

---

## Tabla de exit codes

Tu plan de QA debería assertar estos, no solo "salió o no salió":

| Exit | Comando | Significado |
|---|---|---|
| `0` | cualquiera | OK |
| `1` | `sync resolve` | Falla a nivel txn (no existe, estado equivocado, rowcount ≠ 1) |
| `2` | `sync` / `run` | Mal uso de config/args, o pipeline abortado por conflicto / AS400 inalcanzable |
| `3` | `sync status` / `resolve` | Error de AS400 (`As400CoordinationError`) |

---

## Reset entre corridas

Para volver a probar desde cero **tenés que resetear los DOS lados** —
y acá está el error clásico:

```bash
# Lado local (SQLite tracking + WAL sidecars + logs):
bash scripts/staging/wipe-local-state.sh
```

> ⚠️ **Gotcha #4 — `wipe-local-state.sh` NO toca AS400.** Solo borra
> el SQLite local. Si reseteás solo el local y volvés a correr, las
> filas de NIARVILOG siguen en `O` → el pipeline saltea todo y tu
> prueba no prueba nada (o peor, te fabrica un conflicto *Imported*
> sin querer). El lado AS400 lo reseteás **a mano**:

```sql
-- Devolver el scope de prueba a reclamable:
UPDATE RVILIB.NIARVILOG SET STSCOD='N', OBJIDN='', EERRMSG='', NUMREI=0
WHERE TRNNUM IN ('0001234', '0001235', ...);
-- O, si tu réplica de staging es descartable, DELETE las filas del scope.
```

---

## Checklist de QA — release del sync AS400

- [ ] `doctor --check as400_sync` → `PASS`.
- [ ] Happy path: scope completo → todas las filas en `O` con `OBJIDN`.
- [ ] Doc fallido → `STSCOD='F'`, `EERRMSG` poblado, `NUMREI` ≥ 1.
- [ ] Ninguna fila pegada en `I` post-corrida.
- [ ] Conflicto *Imported* → no aborta, resume saltea el doc.
- [ ] Conflicto *Conflict* → pipeline aborta con **exit 2**.
- [ ] `sync status` → recicla filas `I`, reporta `stale_cleaned`.
- [ ] `sync resolve --prefer-as400` + `--resume` → cierra el conflicto.
- [ ] `sync resolve --prefer-local --cm-object-id` → UPDATE de 1 fila.
- [ ] `sync resolve` sin `--cm-object-id` en `--prefer-local` → **exit 2**.
- [ ] Doble corrida concurrente → cero doble-upload, `as400_claim_lost`
      logueado en el perdedor.
- [ ] AS400 caído a mitad de corrida → retry con backoff, luego
      `As400UnreachableError` + exit 2.

---

## Cross-references

* Contrato de la feature: [as400-sync.md](as400-sync.md).
* Scaffolding de staging: [`scripts/staging/README.md`](../../scripts/staging/README.md).
* Runbook de AS400 caído: [`docs/runbooks/as400-down.md`](../runbooks/as400-down.md).
* Spec: `specs/034-as400-niarvilog-sync/`.
