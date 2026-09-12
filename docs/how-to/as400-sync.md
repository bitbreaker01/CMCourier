# How to: Idempotencia distribuida AS400 NIARVILOG

> [← Volver al índice](../INDEX.md) · [How-to](README.md)

> Disponible desde el cambio **034** (2026-05-11). POST-MVP §4 —
> coordinar idempotencia cross-batch con la tabla centralizada del banco
> ``RVILIB.NIARVILOG`` mientras se mantiene SQLite como la máquina
> de estados por-batch.

---

## Cuándo habilitar esto

Activá esto cuando **al menos una** de estas se aplique:

* El banco requiere tracking de migración centralizado en AS400
  (compliance / auditoría).
* CMCourier y una implementación paralela (ej. un migrador Java
  competidor) son evaluados en ventanas alternantes sobre el mismo
  scope — el claim distribuido previene doble upload.
* Operadores corren CMCourier desde múltiples workstations y el
  SQLite por-workstation no alcanza.

Apagalo (el default) cuando:

* Estás corriendo localmente para dev / staging / dry-run.
* El banco confirmó que el tracking local SQLite es suficiente.

---

## Config TL;DR

```yaml
tracking:
  db_path: /var/lib/cmcourier/tracking.db   # SQLite queda como está
  as400_sync:
    enabled: true                            # ← el toggle
    connection:
      host: as400.bank.example
      port: 446
      database: RVILIB
      driver: "iSeries Access ODBC Driver"
    library: RVILIB                          # default
    table: NIARVILOG                         # default
    columns:                                 # 049 — nombres por-entorno
      # omitir entero → nombres canónicos; override solo lo que difiere
      status_column: ESTADO
      txn_num_column: NUMTRX
    stale_in_progress_minutes: 30            # cleanup de filas STSCOD='I'
    retry_attempts: 3                        # OperationalError transient
    retry_base_delay_s: 5.0                  # exponential backoff
```

Las credenciales viven en env vars (igual que el trigger AS400):
``AS400_USERNAME``, ``AS400_PASSWORD``.

Cuando ``enabled: true``, ``cmcourier doctor`` valida la
conexión + la existencia de ``RVILIB.NIARVILOG``. Corrélo
antes de cualquier corrida de pipeline:

```bash
cmcourier doctor --config prod.yaml --check as400_sync
```

---

## Mapping de campos (fijo)

CMCourier escribe cada columna NIARVILOG desde la fuente siguiente.
Las precondiciones las hacen valer las constraints de schema del banco —
asegurate de que tus fixtures matcheen:

| Columna AS400 | ← | Fuente CMCourier | Notas |
|---|---|---|---|
| ``SISCOD CHAR(1)`` | ← | ``trigger.system_id`` | 1 char exacto |
| ``TRNNUM CHAR(7)`` | ← | ``document.txn_num`` | = RVABREP ``ABAANB``; 7 chars exactos |
| ``DOCFRM CHAR(30)`` | ← | ``document.index7`` | = RVABREP ``ABAHCD`` (tipo RVI, ej ``CC03``) |
| ``IMGARC CHAR(12)`` | ← | ``document.file_name`` | Archivo fuente de primera página, ej ``DAAAH9X4.001`` |
| ``IMGTIP CHAR(1)`` | ← | ``document.image_type`` | ej ``B`` (TIFF), ``O`` (PDF) |
| ``CTECIF VARCHAR(30)`` | ← | ``trigger.shortname`` | El campo "shortname" del banco |
| ``CTENUM DECIMAL(9,0)`` | ← | ``int(trigger.cif or 0)`` | CIF como numérico |
| ``STSCOD CHAR(1)`` | ← | derivado | ``N`` / ``I`` / ``O`` / ``F`` |
| ``IDNBAC VARCHAR(10)`` | ← | ``mapping.id_corto`` | ID de CM, ej ``CN01`` — sale de ``MapeoRVI_CM.IDCM`` en los tres modos |
| ``TIPIDN VARCHAR(128)`` | ← | ``mapping.cmis_type`` | 145: en modo **manifest** sale del `type_id` del manifest JSON (`cmcourier types discover`), no de una columna del CSV. En modo split (035, deprecado) sale de ``MapeoRVI_CM.CMISType``; ``""`` en modo consolidado si está ausente |
| ``OBJIDN VARCHAR(128)`` | ← | ``record.cm_object_id`` (post-S5) | El object id CMIS |
| ``NUMREI INTEGER`` | ← | ``record.retry_count`` | Contador de retry |
| ``PMRREI TIMESTAMP`` | ← | tiempo de claim | ``CURRENT_TIMESTAMP`` en INSERT |
| ``FINREI TIMESTAMP`` | ← | DB2 auto-update | Implícito vía ``ROW CHANGE TIMESTAMP`` |
| ``EERRMSG VARCHAR(1024)`` | ← | ``record.error_message`` | Truncado a 1024 |

### Nombres de columna por-entorno (049)

La tabla de arriba lista los nombres físicos **canónicos** de columna. El
banco corre CMCourier contra varios entornos AS400 cuya tabla
NIARVILOG tiene las mismas 15 columnas bajo **distintos nombres
físicos**. Mapealos con ``tracking.as400_sync.columns`` — una clave
lógica por columna, defaulteando al nombre canónico:

| clave `columns.*` | default canónico | significado lógico |
|---|---|---|
| ``system_id_column`` | ``SISCOD`` | system id del trigger |
| ``txn_num_column`` | ``TRNNUM`` | número de txn RVABREP |
| ``doc_format_column`` | ``DOCFRM`` | tipo de doc RVI |
| ``image_archive_column`` | ``IMGARC`` | nombre de archivo de primera página |
| ``image_type_column`` | ``IMGTIP`` | tipo de imagen |
| ``client_cif_column`` | ``CTECIF`` | shortname del cliente |
| ``client_num_column`` | ``CTENUM`` | CIF como numérico |
| ``status_column`` | ``STSCOD`` | estado N / I / O / F |
| ``idcm_column`` | ``IDNBAC`` | id corto de CM |
| ``cm_type_column`` | ``TIPIDN`` | tipo CMIS |
| ``cm_object_id_column`` | ``OBJIDN`` | object id CMIS |
| ``retry_count_column`` | ``NUMREI`` | contador de retry |
| ``started_at_column`` | ``PMRREI`` | timestamp de claim |
| ``finished_at_column`` | ``FINREI`` | timestamp row-change DB2 |
| ``error_message_column`` | ``EERRMSG`` | último error |

Omití el bloque ``columns`` enteramente y cada nombre queda canónico —
el SQL emitido es byte-idéntico a pre-049. Override solo las claves
que difieren en tu entorno.

**Validación de identificadores.** Estos nombres — más ``library`` y
``table`` — se interpolan directamente en SQL (un identificador SQL nunca
puede ser un bind-param ``?``). Cada uno se valida al cargar config
contra la gramática de identificadores ordinarios DB2-for-i
(``letter / @ / # / $`` después ``letters / digits / _ / @ / # / $``,
128 chars máx). Un nombre con un espacio, comilla, punto y coma, dígito
inicial, o sobre-largo levanta un ``ConfigurationError`` antes de que
se abra alguna conexión.

### Transiciones de estado

```
        (no row yet)
              │
              ▼  try_claim → INSERT (rowcount=1)
            ┌─────┐
            │  I  │ ← en progreso (lo poseemos)
            └──┬──┘
        upload ok                 upload failed
              │                          │
              ▼                          ▼
            ┌─────┐                  ┌─────┐
            │  O  │ ← done           │  F  │ ← failed
            └─────┘                  └─────┘
                                        │
                                        ▼  cleanup_stale (after 30 min)
                                      ┌─────┐
                                      │  N  │ ← reclamable
                                      └─────┘
```

El pre-flight ``cleanup_stale_in_progress`` resetea filas pegadas en
``I`` por más de ``stale_in_progress_minutes`` de vuelta a ``N`` —
recupera de cualquier proceso que crasheó a mitad del claim.

---

## Modelo de concurrencia

Cuando ``enabled: true``, la stage S5 del pipeline hace esto para
cada doc:

1. ``UPDATE NIARVILOG SET STSCOD='I' WHERE …PK… AND STSCOD='N'``.
2. Si ``rowcount == 1`` → ganamos el claim; proceder.
3. Si ``rowcount == 0`` → la fila falta **o** está en
   ``I/O/F``. Probar ``INSERT`` con ``STSCOD='I'``.
4. ``IntegrityError`` en INSERT → alguien más insertó primero
   (perdimos la race) → saltear este doc y loguear ``as400_claim_lost``.

Esto es **atomicidad a nivel DB2**: dos procesos pegándole a la misma
fila ven ownership exclusivo determinista. El migrador Java paralelo
del banco puede usar el mismo protocolo sin cambios.

Después del upload:

* Éxito → SQLite ``S5_DONE`` + ``UPDATE STSCOD='O', OBJIDN=?``.
* Fallo → SQLite ``S5_FAILED`` + ``UPDATE STSCOD='F', EERRMSG=?, NUMREI=NUMREI+1``.

SQLite se escribe **primero** (es el anchor de resume en-proceso),
AS400 segundo.

---

## Reconciliación pre-flight

Cuando el pipeline arranca y el toggle está activo, el
``IdempotencyCoordinator`` hace:

1. ``cleanup_stale_in_progress`` — resetear filas viejas ``I``.
2. Para cada txn en el scope del batch:
   * ``read_state_by_txn(trnnum)`` devuelve la fila NIARVILOG.
   * Comparar con ``is_uploaded(txn)`` de SQLite.
   * Tres outcomes:
     * **Imported**: AS400 dice ``O``, SQLite no tiene fila →
       registrarlo (el operador puede re-correr el pipeline; el
       resume en-proceso ve AS400 ``O`` y saltea).
     * **Conflict**: AS400 dice ``N/I/F``, SQLite dice
       uploaded → resolución dirigida por operador (próxima sección).
     * **Consistent**: sin acción.

Si los conflictos son no vacíos, el pipeline aborta con exit 2.

---

## La regla de autoridad (151)

NIARVILOG es el **punto de coordinación entre varios programas** que
hacen la misma migración: CMCourier y el proceso Java del banco. Para que
eso funcione, los dos lados tienen que poder enterarse de lo que hizo el
otro — y tiene que estar claro quién manda sobre qué. Una sola frase
gobierna las dos direcciones:

> **El lado local manda sobre los documentos que CMCourier procesó.
> El AS400 manda sobre los documentos que CMCourier nunca vio.**

De ahí sale todo lo demás:

| Dirección | Comando | Qué hace |
|---|---|---|
| local → AS400 | ``sync recover`` | corrige lo que hicimos nosotros: INSERTa las filas que faltan y ACTUALIZA las desactualizadas |
| AS400 → local | ``sync pull`` | sólo rellena huecos: importa lo que hizo otro programa y **nunca pisa** un estado terminal local |

Cuando los dos afirman cosas distintas sobre el mismo documento,
**ninguna dirección decide sola**: se reporta como *divergencia* y la
resuelve el operador con ``sync resolve``. Dos direcciones que se pisan
entre sí no son una sincronización, son una pelea.

### Qué hacer con cada divergencia

| Qué se ve | Qué significa | Qué hacer |
|---|---|---|
| ``recover``: ``divergente <txn>: objidn_mismatch`` — AS400 ``'O'`` con otro ``OBJIDN`` | hay **dos objetos en CM** para el mismo documento (lo subimos nosotros y lo subió el otro programa) | verificá los dos objectId en Content Manager. El que sobra se borra a mano; después ``sync resolve <txn> --prefer-local --cm-object-id <el bueno>`` |
| ``pull``/``status``: ``AS400 STSCOD='F' vs local S5_DONE`` | nosotros lo subimos bien, el AS400 quedó viejo (se cayó durante S5) | **local manda**: ``cmcourier sync recover --apply`` lo lleva a ``'O'``. Es el caso normal |
| ``pull``/``status``: ``AS400 STSCOD='O' vs local S5_FAILED`` | el otro programa lo subió después de que a nosotros nos falló | **AS400 manda**: ``sync resolve <txn> --prefer-as400`` |
| ``recover``: ``unrecoverable <txn>: stale_update_no_rows`` | entre nuestra lectura y el UPDATE, otro proceso llevó la fila a ``'O'`` | re-corré ``sync recover``; si sigue, mirá la fila con ``sync resolve`` |

Nada de esto se resuelve solo, y es deliberado: que un programa decida
por su cuenta cuál de dos verdades gana es exactamente lo que no
queremos.

---

## Playbook de resolución de conflictos

Los conflictos aparecen solo cuando los dos stores no acuerdan en un
estado terminal "¿está hecho este doc?". Resolver con el
subcomando ``cmcourier sync``.

### Inspección

```bash
cmcourier sync status --config prod.yaml
# sync status: stale_cleaned=2 escaneadas=18432 importables=57 divergentes=3
#   divergente 0001234: AS400 STSCOD='F' vs local S5_DONE: el pull no pisa un estado terminal local
```

Corre el cleanup de los ``'I'`` vencidos y **barre la tabla** para
reportar las divergencias. No escribe una sola fila del tracking local.
Desde 151 el barrido es el mismo que el de ``pull``: con una tabla
grande tarda lo que tarda leerla.

### Traer lo que hicieron los otros (151)

```bash
cmcourier sync pull --config prod.yaml            # dry-run
cmcourier sync pull --config prod.yaml --apply    # escribe
# sync pull [APPLY]: escaneadas=18432 importadas_ok=55 importadas_fallidas=2 \
#   consistentes=18374 divergentes=1
```

Importa al tracking local los documentos que el otro programa subió
(``STSCOD='O'`` → ``S5_DONE``) o rompió (``'F'`` → ``S5_FAILED`` con
``reason_code=EXTERNAL_FAILURE``, balde ``FALLO``, y el ``EERRMSG`` en
``error_message``). Las filas importadas van al ``batch_id`` sintético
``__as400_import__``: un documento que subió otro programa **no es una
exclusión nuestra** y no puede ensuciar el censo (148) de un batch real.

Se trae todo, sin rango — pero *"todo" no significa "todo en RAM"*: la
lectura va en streaming y las escrituras por lotes. Es idempotente, así
que re-correrlo tras un corte es seguro. ``'I'`` y ``'N'`` quedan afuera
a propósito: son estados en vuelo que cambian solos, e importarlos sería
fotografiar algo que ya no es cierto.

### Empujar lo nuestro (099 + 151)

```bash
cmcourier sync recover --config prod.yaml --apply
# sync recover [APPLY]: recuperadas=12 actualizadas=4 consistentes=980 \
#   divergentes=1 unrecoverable=0
```

``--batch-id`` acota a un batch; **vacío = todos los batches del
tracking**. Desde 151 compara el ESTADO y no la presencia de la clave:
las filas presentes pero en ``'F'``/``'I'``/``'N'`` se ACTUALIZAN a
``'O'`` (con guarda ``STSCOD <> 'O'``, así no pisa un terminal que otro
proceso escribió mientras tanto). Pre-151 esas filas se contaban como
``already_present`` — un nombre que sonaba a éxito — y quedaban
divergentes para siempre.

### Preferir AS400 (más común)

Cuando AS400 tiene el estado autoritativo ``O`` pero SQLite local
no lo sabe:

```bash
cmcourier sync resolve 0001234 --prefer-as400 --config prod.yaml
# resolved 0001234: imported AS400 state — STSCOD='O', OBJIDN='cm-abc-xyz'
```

Esto **imprime** el estado AS400 pero no escribe SQLite
directamente. El operador después re-corre el pipeline con
``--resume`` — la lógica en-proceso ve AS400 ``O`` y
saltea el doc limpiamente. Esto evita extender la API
``ITrackingStore`` de SQLite solo para el flow de resolve.

### Preferir local (raro)

Cuando SQLite subió el doc pero AS400 perdió el update
(ej. AS400 estaba caído durante S5):

```bash
cmcourier sync resolve 0001234 \
  --prefer-local \
  --cm-object-id cm-abc-xyz \
  --config prod.yaml
# resolved 0001234: pushed local cm_object_id='cm-abc-xyz' to AS400.
```

El ``--cm-object-id`` es **requerido** — sacalo de
``cmcourier batch show <batch_id>``. El UPDATE solo dispara si
la fila ya existe en NIARVILOG; si no, re-corré
el pipeline así ``try_claim`` la inserta.

---

## Retry / backoff

Un ``pyodbc.OperationalError`` transient (drops de red, deadlocks,
"servidor temporalmente no disponible") dispara retry automático:

* Intentos: ``retry_attempts`` desde el YAML (default 3).
* Delay: ``retry_base_delay_s * 2^(attempt-1)`` capeado en
  5 minutos. Secuencia default: 5s, 10s, 20s.
* Entre intentos, la conexión cacheada se resetea (la mayoría de los
  errores transient dejan la conexión en un estado inutilizable).
* Después de que el último intento falla → ``As400UnreachableError``
  levantado; el pipeline aborta con exit 2.

``IntegrityError`` **nunca** se retryea — es la
señal de detección de race para ``try_claim``. Otras subclases
de ``pyodbc.Error`` (mismatches de schema, errores de sintaxis) se
propagan como ``As400CoordinationError`` inmediatamente.

---

## Referencia de perillas operacionales

| Campo YAML | Default | Descripción |
|---|---|---|
| ``enabled`` | ``false`` | Toggle maestro. ``true`` activa todo lo de abajo. |
| ``connection`` | requerido cuando enabled | Params ODBC AS400 (host, port, database, driver). |
| ``library`` | ``RVILIB`` | Nombre del schema DB2. Validado como identificador DB2. |
| ``table`` | ``NIARVILOG`` | Nombre de tabla. Override si el banco la renombró. Validado como identificador DB2. |
| ``columns`` | nombres canónicos | Mapa de nombres físicos de columna por-entorno — ver [Nombres de columna por-entorno](#nombres-de-columna-por-entorno-049). Cada valor validado como identificador DB2. |
| ``stale_in_progress_minutes`` | ``30`` | Cuánto puede sentarse una fila ``I`` antes de que pre-flight la resetee. |
| ``retry_attempts`` | ``3`` | Intentos totales por escritura (incl. el primero). Rango: 1..10. |
| ``retry_base_delay_s`` | ``5.0`` | Base para exponential backoff. Debe ser > 0. |

---

## Limitaciones conocidas (intencionales)

* **Una fila por txn**: según la convención operacional del banco,
  NIARVILOG tiene a lo más una fila por ``TRNNUM`` (el ``IMGARC``
  de la primera página). Docs multi-página comparten una fila única.
  Confirmado con el operador en spec 034.
* **``sync resolve --prefer-as400`` no escribe SQLite
  directamente** en 034 — el operador re-corre el pipeline con
  ``--resume``. Desde 151 el camino masivo de esa dirección SÍ escribe:
  es ``sync pull``, que importa todo lo que el tracking local no tiene.
  ``resolve`` queda para el caso de a uno.
* **El ``pull`` no importa ``'I'`` ni ``'N'``**, y no tiene filtros de
  rango (``--since`` / ``--system``). El operador eligió traer todo; si
  con la tabla real resulta lento, los filtros son un agregado chico con
  su propia spec — no se construyen por las dudas.
* **Las divergencias no se resuelven solas.** Se reportan; las resuelve
  el operador. Ver [La regla de autoridad](#la-regla-de-autoridad-151).
* **``sync resolve --prefer-local`` requiere
  ``--cm-object-id`` explícito**. El operador lo saca de
  ``cmcourier batch show`` — evita extender
  ``ITrackingStore`` con una superficie ``find_record_by_txn``
  que se usa solo acá.

---

## Cross-references

* Entrada del roadmap POST-MVP: ``docs/roadmap/POST-MVP.md`` §4.
* Spec: ``specs/034-as400-niarvilog-sync/``.
* Split de CSV de mapping: cambio 035 (``MapeoRVI_CM.csv`` +
  ``MetadatosCM.csv`` + columna ``CMISType`` —
  ver ``specs/035-mapping-csv-split/`` y ``MappingConfig``
  en ``docs/configuration-guide.md``), **deprecado por 145**.
* Manifest de tipos CM: cambio 145 — ``MapeoRVI_CM.csv`` reducido a
  ``IDSistema,IDRVI,IDCM`` + manifest JSON; ``IDNBAC``/``TIPIDN`` salen
  de ahí en vez de columnas del CSV. Ver
  [`cm-type-manifest.md`](cm-type-manifest.md).
* Relacionados: cambio 014 (fuente trigger AS400 — mismo patrón pyodbc),
  cambio 028 (multi-batch — el claim ocurre adentro de ``_upload_one``).
