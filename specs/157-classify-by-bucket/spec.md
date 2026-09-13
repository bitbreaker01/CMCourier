# 157 — Excluido y bloqueado no son fallidos: el status dice la verdad

## Por qué

El operador corrió un batch con la lista de elegibilidad activa y vio
**`fallidos 38527`** en la lista de batches. Los 38527 eran
`CLIENT_NOT_ACTIVE` — clientes sin producto activo, una decisión de
negocio. No fallaron: se excluyeron a propósito.

La causa es la costura de la 148. Cada documento tiene tres ejes
ortogonales: `status` (dónde paró), `reason_code` (por qué), `bucket`
(quién lo arregla). `CLIENT_NOT_ACTIVE` está bien en el balde `EXCLUIDO`
(`models.py:256`), pero como pasa en S2 y la 148 decidió "cero estados
nuevos", se persiste con `status = S2_FAILED`. Y **todo lo que cuenta lo
hace por el status, no por el balde** (`sqlite.py:983`,
`batches_pane.py:91`): un `LIKE '%_FAILED'` se lleva puesto al cliente
inactivo.

Resultado: `batch show` (el censo) lo clasifica bien en EXCLUIDO, pero la
lista de batches, el header del monitor y `retry-failed` lo tratan como
falla. Dos vistas del mismo dato que no coinciden — la familia de bugs de
toda esta semana. La decisión de "cero estados nuevos" fue la que abrió
el agujero; ahora se cierra: **el sufijo del status tiene que coincidir
con el balde.**

## Qué

### REQ-001 — Tres sufijos terminales, uno por balde

`StageStatus` gana dos sufijos, para que el estado terminal de un
documento no-subido diga a qué balde pertenece:

| Sufijo | Balde | Significado |
|--------|-------|-------------|
| `_FAILED` | `FALLO` | se rompió en ejecución (existente) |
| `_BLOCKED` | `BLOQUEADO` | falta config; lo arregla el operador (NUEVO) |
| `_EXCLUDED` | `EXCLUIDO` | decisión de negocio/origen; no es falla (NUEVO) |

Miembros nuevos: `S2_EXCLUDED`, `S2_BLOCKED`, `S3_BLOCKED` (los únicos
stages donde hoy caen razones EXCLUIDO/BLOQUEADO en un `_FAILED`). Se
agregan los que hagan falta si el mapeo lo exige, pero SÓLO esos.

**`S1_FILTERED` y `S1_SKIPPED` se conservan tal cual** (051/062): ya son
no-`_FAILED`, ya cuentan bien, y renombrarlos rompería bases existentes,
código y docs sin ganar nada. Cuentan como categoría `excluido`.

### REQ-002 — El status se elige por el balde de la razón

Helper de dominio `terminal_status_for(stage: int, bucket: ReasonBucket)
-> StageStatus`: `FALLO → Sn_FAILED`, `BLOQUEADO → Sn_BLOCKED`,
`EXCLUIDO → Sn_EXCLUDED`. En `orchestrators/staged.py`, donde hoy se
escribe `S2_FAILED`/`S3_FAILED` para una exclusión o un bloqueo, se usa
este helper con el balde de `excluded.reason_code`. Las razones FALLO no
cambian de status.

Mapeo resultante de las razones que hoy visten `_FAILED` mal:
- `CLIENT_NOT_ACTIVE` → `S2_EXCLUDED`
- `CODE_NOT_MAPPED`, `TYPE_NOT_IN_MANIFEST`, `IDENTITY_UNRESOLVED` → `S2_BLOCKED`
- `METADATA_UNRESOLVED` → `S3_BLOCKED`
- `SOURCE_ROW_INCOMPLETE` (BLOQUEADO, hoy llega como `ExcludedTrigger` en
  S1 → `S1_FILTERED`): se mantiene en S1 pero como `S1_BLOCKED` para que
  su categoría sea `bloqueado`, no `excluido` — es config, no baja.

`_require_terminal_state` (`sqlite.py`) ya acepta por sufijo; se le suman
`_BLOCKED` y `_EXCLUDED`.

### REQ-003 — Contar por categoría de sufijo, no por `_FAILED`

Ahora que el sufijo lleva el balde, la categoría sale del sufijo, sin que
el conteo tenga que importar el mapa razón→balde:

| Categoría | Sufijos |
|-----------|---------|
| subidos | `S5_DONE` |
| fallidos | `*_FAILED` |
| bloqueados | `*_BLOCKED` |
| excluidos | `*_EXCLUDED`, `S1_FILTERED`, `S1_SKIPPED` |
| pendientes | `*_PENDING` |

- `get_batch_details` (`sqlite.py:983`): la lista de "fallidos" del
  detalle pasa a ser SÓLO `*_FAILED`. Un bloque separado lista los
  `*_BLOCKED`.
- El pivot y `_counts` de `batches_pane.py` derivan las cuatro
  categorías. La lista de batches muestra **subidos / fallidos /
  bloqueados / excluidos** (columna nueva `bloqueados`).
- `retry_failed`: sin cambio semántico — ya excluía el balde EXCLUIDO
  (150); ahora, al ser `*_EXCLUDED`/`*_BLOCKED` estados propios, el
  `LIKE '%_FAILED'` los deja fuera solo. Un `_BLOCKED` NO se reintenta
  con `R`: se arregla la config y se re-corre la migración (se documenta).
- `reanudable` = tiene `*_FAILED` o `*_PENDING`. Un batch cuyo único
  "problema" son exclusiones o bloqueos NO es reanudable por retry.

### REQ-004 — La vista en vivo, igual (extiende 155)

El header del monitor y `OUTCOMES` separan las cuatro categorías. La
corrida del operador —38527 excluidos, 0 subidos, 0 fallidos— tiene que
mostrarse como **excluidos**, no como fallidos, y el cierre no la anuncia
en rojo por 38527 "fallas" que no existen. El candado de parridad de 155
(`failed_total` del snapshot == fallas de `batch show`) se actualiza para
comparar las cuatro categorías, no sólo fallidos.

### REQ-005 — `batch show` (censo) ya está bien, verificar

El censo de 148 ya agrupa por balde, así que su salida no cambia. Hay que
verificar que sigue cuadrando con los conteos nuevos (subidos + cada
categoría == total en origen) y que el `!! DESCUADRE` sigue disparando
sólo cuando corresponde.

### REQ-006 — Migración de datos existentes

Las bases que el operador ya tiene traen filas `S2_FAILED` con
`reason_code` de balde EXCLUIDO/BLOQUEADO. Una migración idempotente al
abrir la base reescribe el `status` de esas filas al sufijo nuevo según
`REASON_BUCKETS[reason_code]`, para que los batches viejos se lean bien.
Filas `_FAILED` sin `reason_code`, o con reason de balde FALLO, no se
tocan.

### REQ-007 — Docs

`docs/reference/tracking-db-schema.md` (los estados nuevos, la tabla de
categorías), `docs/how-to/operator/read-the-batch-census.md` y
`retry-only-failed-records.md` (un `_BLOCKED` se arregla con config +
re-run, no con retry), `interpret-the-tui-tabs.md`,
`explanation/state-machine.md`, `idempotency-and-retries.md`;
`CHANGELOG.md`.

## Fuera de alcance

- Renombrar `S1_FILTERED`/`S1_SKIPPED`. Funcionan, son historia (051/062)
  y renombrarlos rompe bases sin ganar nada.
- La pestaña de prueba de tipos — es su propia spec (158).
