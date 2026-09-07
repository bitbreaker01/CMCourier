# Subsistema de sync — SQLite ↔ AS400 NIARVILOG

> [← Volver al índice](../INDEX.md) · [Diagramas](README.md)

CMCourier lleva **dos** libros de contabilidad de la migración. El local es la SQLite de `tracking.db_path` (`migration_log`), que existe siempre. El remoto es la tabla `RVILIB.NIARVILOG` en el AS400, que existe sólo si `tracking.as400_sync.enabled: true` (034).

El local alcanza para idempotencia dentro de una estación. El remoto existe porque el banco corre CMCourier desde varias estaciones contra el mismo Content Manager, y también porque los sistemas legacy del banco leen NIARVILOG para saber qué documento migró y a qué objeto CM fue a parar. `tracking.as400_sync.mode` (096) elige **cuándo** se escribe la fila remota, y esa elección es un tradeoff duro entre seguridad y latencia.

## Las dos bases y quién las escribe

```mermaid
flowchart TB
    subgraph pipeline["Pipeline (S5 UPLOAD)"]
        S5["S5 · POST a CMIS"]
    end

    subgraph local["Local — SQLite"]
        LOG[("migration_log<br/>S5_DONE / S5_FAILED<br/>+ cm_object_id")]
    end

    subgraph remote["Remoto — AS400 DB2"]
        NIA[("RVILIB.NIARVILOG<br/>STSCOD: N → I → O | F")]
    end

    S5 -->|siempre| LOG

    S5 -.->|"mode: claim<br/>síncrono, 2-3 round-trips/doc"| NIA
    LOG -.->|"mode: periodic<br/>PeriodicReconciler cada<br/>periodic.interval_minutes"| NIA

    NIA -->|"import de filas 'O' ajenas"| LOG

    CLI["cmcourier sync<br/>status · recover · resolve"] --> NIA
    CLI --> LOG
    TAB["consola [8] SYNC"] --> CLI
```

Las flechas punteadas son mutuamente excluyentes: el `mode` elige una.

## `STSCOD` — la máquina de estados remota

```mermaid
stateDiagram-v2
    [*] --> N: INSERT de la fila (o ya existía)

    N --> I: try_claim() · UPDATE ... WHERE STSCOD='N'
    note right of I
        El claim es la exclusión mutua.
        rowcount == 0 ⇒ otro proceso ganó
        ⇒ este proceso NO sube el documento.
    end note

    I --> O: mark_uploaded() · OBJIDN = cm_object_id
    I --> F: mark_failed() · EERRMSG = motivo
    I --> N: stale cleanup · FINREI más viejo que<br/>stale_in_progress_minutes

    O --> [*]
    F --> [*]
```

| `STSCOD` | Qué significa |
|----------|---------------|
| `N` | New — la fila existe, nadie la tomó. |
| `I` | In progress — alguien la reclamó y está subiendo. |
| `O` | Ok — subida, con `OBJIDN` = el `cm_object_id` de Content Manager. |
| `F` | Failed — con el motivo en `EERRMSG`. |

El `I` es el estado peligroso: si el proceso que reclamó la fila muere, queda pegada. Por eso `sync status` (y el arranque de una corrida) corre el cleanup que devuelve a `N` toda fila `I` cuyo `FINREI` sea más viejo que `stale_in_progress_minutes` (default 30).

## `claim` vs `periodic`

```mermaid
flowchart LR
    subgraph claim["mode: claim (default)"]
        direction TB
        C1["try_claim: N→I"] --> C2["POST a CMIS"] --> C3["mark_uploaded: I→O"]
    end

    subgraph periodic["mode: periodic"]
        direction TB
        P1["POST a CMIS"] --> P2["escribir SQLite"] --> P3["…devolver el worker"]
        P4["PeriodicReconciler<br/>(thread de fondo)"] -.->|"cada interval_minutes"| P5["run_pass: propagar<br/>N→O en un solo UPDATE"]
    end
```

`claim` paga 2-3 round-trips DB2 **síncronos por documento** y a cambio garantiza que dos migradores concurrentes no suban el mismo documento dos veces: el `UPDATE ... WHERE STSCOD='N'` es atómico, y el que ve `rowcount == 0` se abstiene.

`periodic` saca al AS400 del camino crítico — S5 escribe sólo SQLite y un `PeriodicReconciler` propaga en lote. Es mucho más rápido, y **no previene el doble-upload**: los conflictos se detectan post-hoc. Sólo tiene sentido si sabés que no hay un migrador competidor. Desde 117 la propagación de un documento ya terminado es un único `UPDATE` guardado por `STSCOD='N'` (el paso por `I` era puro round-trip), y desde 098 una pasada nunca levanta excepción ni pierde un ítem: lo que falla vuelve a la cola (`requeued`).

## Los tres comandos de reconciliación

```mermaid
flowchart TD
    START(["divergencia entre SQLite y NIARVILOG"]) --> STATUS

    STATUS["sync status<br/>read-only"]
    STATUS -->|"limpia los I vencidos<br/>+ prueba conectividad"| DIAG{"¿qué falta?"}

    DIAG -->|"docs S5_DONE en SQLite<br/>sin fila en AS400<br/>(daño del modo periodic)"| REC
    DIAG -->|"un TRNNUM puntual<br/>discrepa"| RES

    subgraph REC["sync recover (099/118)"]
        direction TB
        R1["dry-run — SIEMPRE primero<br/>re-deriva DOCFRM/IMGTIP de RVABREP<br/>y IDNBAC/TIPIDN del mapping"]
        R1 --> R2{"¿filas recuperables?"}
        R2 -->|no| R3["unrecoverable:<br/>sin fila RVABREP o IDRVI sin mapear<br/>— nunca se inserta a ciegas"]
        R2 -->|sí| R4["--apply → INSERT de filas 'O'<br/>idempotente: saltea las presentes"]
    end

    subgraph RES["sync resolve TXN"]
        direction TB
        S1{"¿quién manda?"}
        S1 -->|"--prefer-as400"| S2["pull a SQLite<br/>(read-only del lado AS400)"]
        S1 -->|"--prefer-local"| S3["push cm_object_id a AS400<br/>requiere --cm-object-id"]
    end
```

La regla del `recover` es que **nunca inserta a ciegas**: si un `txn` no tiene fila RVABREP, o su `IDRVI` no está en el Modelo Documental, no hay forma de derivar `DOCFRM`/`IMGTIP`/`IDNBAC`/`TIPIDN` y la fila se reporta como `unrecoverable` en vez de inventarse. Por eso el dry-run no es una cortesía: es el paso donde ves qué se va a escribir antes de escribirlo.

## La pestaña `[8]` de la consola

`cmcourier console` expone exactamente estas tres operaciones — el mismo módulo `cli/sync_ops.py`, no una reimplementación (128).

```mermaid
flowchart TD
    T8["[8] SYNC"] --> AVAIL{"sync_unavailable_reason()"}
    AVAIL -->|"as400_sync.enabled: false"| OFF["pestaña deshabilitada<br/>con el motivo en claro"]
    AVAIL -->|"faltan credenciales"| CRED["→ te manda a [2] CREDENCIALES"]
    AVAIL -->|None| OPS

    subgraph OPS["operaciones (todas en background)"]
        direction TB
        O1["s · estado"]
        O2["simular (dry-run)"] --> GATE{"¿el dry-run del<br/>MISMO batch_id<br/>encontró filas?"}
        GATE -->|no| DIS["'aplicar' queda deshabilitado"]
        GATE -->|sí| O3["aplicar · confirmación<br/>(tipear PRD si environment: prd)"]
        O4["resolver · txn + preferencia<br/>'local manda' pide cm_object_id<br/>+ confirmación"]
    end
```

El gate del `aplicar` es la parte interesante: no alcanza con haber corrido *un* dry-run, tiene que ser el del **mismo `batch_id`** y tiene que haber encontrado filas. Es la misma idea que el interlock de `prd` en el launcher — hacer que el camino destructivo requiera un paso previo deliberado.

## Ver también

- [`explanation/idempotency-and-retries.md`](../explanation/idempotency-and-retries.md) — la state machine local y la política de retry
- [`explanation/operations-console.md`](../explanation/operations-console.md) — por qué la consola envuelve estos comandos
- [`diagrams/console-flow.md`](console-flow.md) — la máquina de estados de la consola
- [`reference/cli.md`](../reference/cli.md#sync--as400-niarvilog-reconciliation-034) — flags exactos de `sync status | recover | resolve`
- [`how-to/as400-sync.md`](../how-to/as400-sync.md) · [`how-to/testing-as400-sync.md`](../how-to/testing-as400-sync.md)
- [`runbooks/as400-down.md`](../runbooks/as400-down.md) — qué hacer cuando el AS400 no responde a mitad de corrida
