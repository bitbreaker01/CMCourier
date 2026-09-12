# 151 — Sincronización de verdad entre el tracking local y RVIMGLOG

## Por qué

La tabla del AS400 (`NIARVILOG` en el código, `RVIMGLOG` en el banco) es
**el punto de coordinación entre varios programas** que hacen la misma
migración: CMCourier y el proceso Java del banco. Para que eso funcione,
los dos lados tienen que poder enterarse de lo que hizo el otro.

Hoy no funciona, por tres motivos distintos.

### 1. `sync recover` chequea PRESENCIA, no ESTADO

`services/recovery.py:148-150`:

```python
present = self._as400.read_states_by_txns([r.txn_num for r in records])
already_present = [r.txn_num for r in records if r.txn_num in present]
missing = [r for r in records if r.txn_num not in present]
```

Un documento que localmente está `S5_DONE` pero que en el AS400 quedó en
`'F'`, `'I'` o `'N'` se cuenta como `already_present` y **se deja
divergente**. `recover` arregla filas AUSENTES y nunca filas
DESACTUALIZADAS — y como lo reporta como "ya estaba presente", parece que
salió todo bien. Éste es el verdadero bug de "el mejor estado no gana".

### 2. La dirección AS400 → local NO EXISTE

`record_external_upload` (`adapters/tracking/sqlite.py:653`) está bien
escrito y es **código muerto en producción**: su único llamador
(`services/reconciler.py:355-378`) necesita un `import_scope_provider`
que `config/wiring.py:312-316` nunca pasa, así que el scope siempre es
`set()` y los candidatos siempre `[]`. `IdempotencyCoordinator.preflight_sync`
(`services/idempotency.py:279`) tampoco se llama desde producción, y no
escribiría en SQLite aunque se llamara. Y `sync resolve --prefer-as400`
no escribe nada por comentario explícito (`cli/sync_ops.py:150-152`).

**Cero filas importadas en producción, nunca.** Además no existe ninguna
consulta que LISTE la tabla: todas las lecturas al AS400 son por lista de
TXN que el lado local ya conoce (`read_states_by_txns`,
`adapters/tracking/as400_niarvilog.py:421`). No se puede preguntar "¿qué
hay ahí?", sólo "¿qué sabés de estos que yo ya tengo?".

### 3. `uploaded_records` no deduplica por TXN

`sqlite.py:765-769` no tiene `DISTINCT` ni `GROUP BY`, y el índice único
es `(rvabrep_txn_num, batch_id)`. Un TXN `S5_DONE` en dos batches produce
dos INSERT contra la misma PK del AS400.

*(Lo que SÍ funciona y el operador no sabía: `batch_id=None` ya barre
todos los batches —`sqlite.py:771`, `sync_pane.py:148`—. No hay que
construir nada para eso; hay que decirlo mejor.)*

## Qué

### REQ-001 — La regla de autoridad

Una sola frase que gobierna las dos direcciones:

> **El lado local manda sobre los documentos que CMCourier procesó. El
> AS400 manda sobre los documentos que CMCourier nunca vio.**

De ahí sale todo lo demás: `recover` (local → AS400) corrige lo que
nosotros hicimos; `pull` (AS400 → local) sólo rellena huecos y **nunca
pisa** un estado terminal local. Cuando los dos afirman cosas distintas
sobre el mismo documento, **ninguna dirección decide sola**: se reporta
como divergencia y lo resuelve el operador. Dos direcciones que se pisan
entre sí no son una sincronización, son una pelea.

### REQ-002 — `uploaded_records` deduplica por TXN

`SELECT` con agrupación por `rvabrep_txn_num`, una fila por TXN, elección
determinística (el `completed_at` más reciente; a igualdad, el `id` mayor).
Sin esto, `recovery.py:150` genera dos INSERT para la misma PK del AS400.

### REQ-003 — `recover` compara estado y ACTUALIZA

`read_states_by_txns` ya devuelve el `STSCOD`; hoy se descarta y sólo se
mira la clave. Pasan a existir tres grupos en lugar de dos:

| Grupo | Condición | Acción |
|---|---|---|
| `missing` | el TXN no está en AS400 | INSERT `'O'` (como hoy) |
| `stale` | está, con `STSCOD != 'O'` | **UPDATE a `'O'`** + `OBJIDN` + `EERRMSG=''` |
| `consistent` | está con `'O'` | nada |

Un `'O'` con un `OBJIDN` distinto del local NO es `consistent`: es una
divergencia real (dos objetos en CM para el mismo documento) y se reporta
sin tocar nada.

El dry-run distingue los tres grupos, y el reporte también: hoy
`already_present` esconde las filas desactualizadas detrás de un conteo
que suena a éxito.

### REQ-004 — `sync pull`: traer lo que hicieron los otros programas

Comando nuevo `cmcourier sync pull` (+ botón en `8·SYNC`).

- **Consulta nueva** en `as400_niarvilog.py`: listar la tabla por
  `STSCOD IN ('O','F')`. Es la primera lectura del AS400 que no parte de
  una lista de TXN conocidos.
- **Sin rango**, por decisión del operador: se trae todo. Pero **"todo"
  no puede significar "todo en RAM"**: la consulta va en streaming
  (`query_stream` / `fetchmany`, el patrón de `odbc_base.py:127-149`) y
  las escrituras a SQLite van por lotes. El precedente es 148, donde
  `get_by_fields_in` terminaba en `fetchall()` y por eso el censo no
  podía barrer un sistema entero.
- **Qué se escribe**, por REQ-001:
  - TXN sin fila terminal local ⇒ se importa. `'O'` → `S5_DONE` vía
    `record_external_upload` (que ya existe y sólo necesita un llamador);
    `'F'` → `S5_FAILED` con `reason_code = EXTERNAL_FAILURE` (código
    nuevo, balde `FALLO`) y `EERRMSG` en `error_message`.
  - TXN con fila terminal local que COINCIDE ⇒ nada.
  - TXN con fila terminal local que NO coincide ⇒ **divergencia**: se
    reporta, no se pisa.
- Las filas importadas van al batch sintético que ya usa
  `record_external_upload`, para no ensuciar el censo de un batch real:
  un documento que subió otro programa no es una exclusión nuestra.
- Dry-run por default, `--apply` para escribir, igual que `recover`.

### REQ-005 — Dejar de mentir en la UI

- `cli/commands/sync.py:7-8` dice que `sync status` *"reporta cualquier
  conflicto sin tocar estado"*. No reporta ninguno (`sync_ops.py:90-98`
  sólo limpia los `'I'` vencidos). O lo reporta, o el texto dice lo que
  hace. **Que lo reporte**: con `pull` construido, listar divergencias es
  la misma consulta sin escribir.
- `services/idempotency.py:331-332` publicita
  `sync resolve ... (or --all)`. Ese flag no existe.
- `8·SYNC`: el campo `batch_id` dice explícitamente que vacío = todos los
  batches, en vez de un placeholder cortado. La capacidad estaba; el
  operador no la veía.

### REQ-006 — Docs

`docs/how-to/` guía de la sincronización de dos vías con la regla de
autoridad de REQ-001 y qué hacer con cada divergencia;
`docs/reference/cli.md` (`sync pull`, `sync status`, `sync recover`);
`docs/explanation/idempotency-and-retries.md`;
`docs/explanation/operations-console.md` (`8·SYNC`); `CHANGELOG.md`.

## Fuera de alcance

- **Filtros de rango del `pull`** (`--since`, `--system`). El operador
  eligió traer todo. Si con la tabla real resulta lento, son un agregado
  chico y tienen su propia spec — no se construyen por las dudas.
- **Importar `'I'` y `'N'`.** Son estados en vuelo que cambian solos;
  importarlos sería fotografiar algo que ya no es cierto.
- **Resolución automática de divergencias.** Se reportan; las resuelve el
  operador con `sync resolve`. Que un programa decida solo cuál de dos
  verdades gana es exactamente lo que no queremos.
