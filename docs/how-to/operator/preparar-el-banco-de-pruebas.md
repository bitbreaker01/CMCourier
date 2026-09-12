# Preparar el banco de pruebas: cubrir todos los escenarios

Guía para armar, en tu RVABREP de **desarrollo**, un conjunto de filas que
ejercite **todos** los caminos del pipeline: cada razón del censo, cada
salto de la cadena de identidad, cada regla de formato, la elegibilidad y
las dos direcciones del sync.

La idea es simple: **una fila de RVABREP por escenario**, con un `TRNNUM`
que diga cuál es. Cuando algo salga distinto de lo esperado, sabés
exactamente qué fila mirar.

> ⚠️ Esto se hace **sólo en desarrollo**. Todo lo que sigue escribe en
> RVABREP y en la tabla de log del AS400.

---

## Paso 0 — La foto del esquema (no te lo saltees)

Antes de escribir un solo `INSERT`, mirá cómo es tu tabla de verdad. Los
largos y los `NOT NULL` varían por entorno, y un `INSERT` que no respeta
un `CHAR(7)` te va a rebotar con un error que no dice nada útil:

```sql
SELECT COLUMN_NAME, DATA_TYPE, LENGTH, NUMERIC_SCALE, IS_NULLABLE, CCSID
FROM   QSYS2.SYSCOLUMNS
WHERE  TABLE_SCHEMA = 'RVILIB' AND TABLE_NAME = 'RVABREP'
ORDER  BY ORDINAL_POSITION;
```

Y lo mismo para la tabla de log (en el banco es `RVIMGLOG`):

```sql
SELECT COLUMN_NAME, DATA_TYPE, LENGTH, NUMERIC_SCALE, CCSID
FROM   QSYS2.SYSCOLUMNS
WHERE  TABLE_NAME = 'RVIMGLOG'
ORDER  BY ORDINAL_POSITION;
```

Anotá especialmente **`CTENUM`**: su precisión es el `identity.cif.max_digits`
de tu YAML. Si no coincide, te va a pasar lo del `SQLSTATE 22003`.

El mapa lógico → físico de RVABREP que usa CMCourier:

| Lógico | Físico | Qué lleva en este banco |
|---|---|---|
| sistema | `ABAACD` | `1` |
| txn | `ABAANB` | el ID del caso (`T0001`…) |
| **index1** | `ABABCD` | shortname **o** afiliado hijo |
| index2 | `ABACCD` | CIF / afiliado / tarjeta / préstamo |
| index3..6 | `ABADCD`…`ABAGCD` | libres |
| **index7** | `ABAHCD` | **el IDRVI** (la clave del mapeo) |
| tipo imagen | `ABABST` | `P` (PDF) |
| path | `ABAICD` | carpeta del share |
| archivo | `ABAJCD` | nombre de la primera página |
| alta / vista | `ABAADT` / `ABABDT` | timestamps |
| páginas | `ABABUN` | cantidad |
| **borrado** | `ABACST` | **no vacío = borrado** |

---

## Paso 1 — Los CSV auxiliares

Cuatro archivos chiquitos. Cada uno tiene, **a propósito**, casos que
matchean y casos que no.

> **El match de los CSV es EXACTO, byte por byte.** Sin `trim`, sin
> mayúsculas/minúsculas. Un espacio de más en la columna clave y el
> lookup devuelve vacío sin decir nada. Guardá los archivos sin espacios
> sobrantes.

**`afiliados-padre-hijo.csv`** — hijo (sucursal) → padre (afiliado)

```csv
AfiliadoHijo,AfiliadoPadre
10000001,90000001
10000002,90000001
10000003,90000002
```

**`afiliado-propietario.csv`** — padre → shortname del dueño

```csv
AfiliadoPadre,Shortname
90000001,ACMESA001001
90000002,BBVASA002002
```

**`cif-shortname.csv`** — shortname → CIF

```csv
Shortname,CIF
ACMESA001001,1000
BBVASA002002,000001000
CDEFSA003003,123456789
```

> Fijate que `ACMESA001001` tiene CIF `1000` (sin ceros) y `BBVASA002002`
> tiene `000001000` (con ceros). **Los dos tienen que terminar igual** en
> Content Manager: `000001000`. Eso prueba el `pad_left`.
>
> Y `CDEFSA003003` existe acá pero **no** en `afiliado-propietario.csv` —
> es el caso del shortname que llega directo del trigger.

**`clientes-activos.csv`** — la lista de elegibilidad

```csv
Shortname,CIF
ACMESA001001,000001000
CDEFSA003003,123456789
```

> `BBVASA002002` **no está**: ése es el caso `CLIENT_NOT_ACTIVE`.

---

## Paso 2 — El YAML de pruebas

Copiá tu config y ajustá estas cuatro cosas:

```yaml
triggers:
  kind: rvabrep
  filters:
    systems: ["1"]                  # ← al SQL
    document_types: ["DC01","TC18","PP16"]   # ← NO va al SQL: clasifica
                                             #   ZZ99 y AF01 quedan fuera
eligibility:
  enabled: true
  source: "csv:clientes_activos"
  match_any:
    - {field: BAC_Shortname, column: Shortname}
    - {field: BAC_CIF,       column: CIF}

identity:
  shortname: {field: BAC_Shortname, on_missing: fail}
  cif:       {field: BAC_CIF, on_missing: fail, max_digits: 9}
```

Verificá antes de correr nada:

```powershell
cmcourier doctor -c config-dev.yaml --check tracking
cmcourier doctor -c config-dev.yaml --check mapping
cmcourier types check -c config-dev.yaml
```

---

## Paso 3 — Las filas

Un `INSERT` por bloque temático, para que puedas correr de a uno y ver
qué pasa. Ajustá la lista de columnas a lo que te devolvió el Paso 0 — si
tu tabla tiene columnas `NOT NULL` que acá no figuran, agregalas.

### 3.1 Camino feliz y formato

```sql
INSERT INTO RVILIB.RVABREP
  (ABAACD, ABAANB, ABABCD, ABACCD, ABAHCD, ABABST,
   ABAICD, ABAJCD, ABAADT, ABABDT, ABABUN, ABACST)
VALUES
  -- C01: shortname directo, CIF "1000" en el CSV → debe subir 000001000
  ('1','T0001','ACMESA001001','', 'DC01','P','/RVI9/DEV','T0001.PDF',
   CURRENT TIMESTAMP, CURRENT TIMESTAMP, 1, ''),

  -- C02: shortname directo, CIF ya con ceros → mismo resultado
  ('1','T0002','BBVASA002002','', 'DC01','P','/RVI9/DEV','T0002.PDF',
   CURRENT TIMESTAMP, CURRENT TIMESTAMP, 1, ''),

  -- C03: shortname con relleno de espacios (columna CHAR)
  ('1','T0003','CDEFSA003003 ','','DC01','P','/RVI9/DEV','T0003.PDF',
   CURRENT TIMESTAMP, CURRENT TIMESTAMP, 1, '');
```

| Caso | Prueba | Esperado |
|---|---|---|
| C01 | `pad_left` del CIF | sube, `BAC_CIF = 000001000` |
| C02 | CIF ya formateado | sube, `BAC_CIF = 000001000` |
| C03 | `trim` del shortname | sube (el shortname vale 12 tras el trim) |

### 3.2 La cadena de identidad

```sql
INSERT INTO RVILIB.RVABREP
  (ABAACD, ABAANB, ABABCD, ABACCD, ABAHCD, ABABST,
   ABAICD, ABAJCD, ABAADT, ABABDT, ABABUN, ABACST)
VALUES
  -- C04: afiliado hijo en index1 (no hay shortname) → cadena de 3 saltos
  ('1','T0004','10000001','','DC01','P','/RVI9/DEV','T0004.PDF',
   CURRENT TIMESTAMP, CURRENT TIMESTAMP, 1, ''),

  -- C05: afiliado hijo en index2, index1 vacío
  ('1','T0005','','10000003','DC01','P','/RVI9/DEV','T0005.PDF',
   CURRENT TIMESTAMP, CURRENT TIMESTAMP, 1, ''),

  -- C06: afiliado que NO está en el CSV de padres
  ('1','T0006','19999999','','DC01','P','/RVI9/DEV','T0006.PDF',
   CURRENT TIMESTAMP, CURRENT TIMESTAMP, 1, ''),

  -- C07: fila sin shortname y sin sistema
  ('' ,'T0007','','','DC01','P','/RVI9/DEV','T0007.PDF',
   CURRENT TIMESTAMP, CURRENT TIMESTAMP, 1, '');
```

| Caso | Prueba | Esperado |
|---|---|---|
| C04 | hijo → padre → shortname → CIF, desde index1 | sube, shortname `ACMESA001001` |
| C05 | la misma cadena desde index2 | sube, shortname `BBVASA002002`… **y después `CLIENT_NOT_ACTIVE`** (ver C13) |
| C06 | afiliado desconocido | `IDENTITY_UNRESOLVED` · BLOQUEADO |
| C07 | fila incompleta | `SOURCE_ROW_INCOMPLETE` · BLOQUEADO |

### 3.3 Exclusiones

```sql
INSERT INTO RVILIB.RVABREP
  (ABAACD, ABAANB, ABABCD, ABACCD, ABAHCD, ABABST,
   ABAICD, ABAJCD, ABAADT, ABABDT, ABABUN, ABACST)
VALUES
  -- C08: código con marca de borrado
  ('1','T0008','ACMESA001001','','DC01','P','/RVI9/DEV','T0008.PDF',
   CURRENT TIMESTAMP, CURRENT TIMESTAMP, 1, 'D'),

  -- C09: código FUERA de triggers.filters.document_types
  ('1','T0009','ACMESA001001','','AF01','P','/RVI9/DEV','T0009.PDF',
   CURRENT TIMESTAMP, CURRENT TIMESTAMP, 1, ''),

  -- C10: código que NO existe en MapeoRVI_CM.csv
  ('1','T0010','ACMESA001001','','ZZ99','P','/RVI9/DEV','T0010.PDF',
   CURRENT TIMESTAMP, CURRENT TIMESTAMP, 1, ''),

  -- C11: cliente que NO está en clientes-activos.csv
  ('1','T0011','BBVASA002002','','DC01','P','/RVI9/DEV','T0011.PDF',
   CURRENT TIMESTAMP, CURRENT TIMESTAMP, 1, '');
```

| Caso | Esperado |
|---|---|
| C08 | `DELETED_AT_SOURCE` · EXCLUIDO |
| C09 | `EXCLUDED_BY_FILTER` · EXCLUIDO |
| C10 | `CODE_NOT_MAPPED` · BLOQUEADO |
| C11 | `CLIENT_NOT_ACTIVE` · EXCLUIDO |

> **C09 es el que prueba el cambio de la spec 148.** Antes de ella, esa
> fila no volvía del AS400 y era invisible. Si aparece en el censo, el
> censo funciona.
>
> Poné **varias** filas borradas del mismo shortname (`T0008a`, `T0008b`,
> `T0008c`): así verificás que no colapsan en una sola, que era el bug de
> la clave sintética.

### 3.4 Formato de los campos de negocio

```sql
INSERT INTO RVILIB.RVABREP
  (ABAACD, ABAANB, ABABCD, ABACCD, ABAHCD, ABABST,
   ABAICD, ABAJCD, ABAADT, ABABDT, ABABUN, ABACST)
VALUES
  -- C12: tarjeta de 16 dígitos en index2
  ('1','T0012','ACMESA001001','4111111111111111','TC18','P',
   '/RVI9/DEV','T0012.PDF', CURRENT TIMESTAMP, CURRENT TIMESTAMP, 1, ''),

  -- C13: tarjeta ausente → cae al default "000000"
  ('1','T0013','ACMESA001001','','TC18','P',
   '/RVI9/DEV','T0013.PDF', CURRENT TIMESTAMP, CURRENT TIMESTAMP, 1, ''),

  -- C14: préstamo de 9 dígitos en index2
  ('1','T0014','ACMESA001001','123456789','PP16','P',
   '/RVI9/DEV','T0014.PDF', CURRENT TIMESTAMP, CURRENT TIMESTAMP, 1, ''),

  -- C15: valor de 7 dígitos donde se espera una tarjeta de 14-16
  ('1','T0015','ACMESA001001','1234567','TC18','P',
   '/RVI9/DEV','T0015.PDF', CURRENT TIMESTAMP, CURRENT TIMESTAMP, 1, '');
```

| Caso | Prueba | Esperado |
|---|---|---|
| C12 | patrón `^[0-9]{14,16}$` | sube con la tarjeta |
| C13 | el default sin validar (spec 149) | sube con `000000`. **Antes de la 149 este documento moría** |
| C14 | préstamo | sube |
| C15 | el patrón descarta y cae al default | sube con `000000`, no con `1234567` |

---

## Paso 4 — Materializar los archivos

Las filas apuntan a archivos que todavía no existen. No los armes a mano:

```powershell
cmcourier mock generate --rvabrep-as400 --config config-dev.yaml `
  --root C:\ruta\share-dev `
  --pdf-min 10kb --pdf-max 200kb --img-min 5kb --img-max 100kb `
  --system 1 --seed 42
```

`--seed` fija la aleatoriedad: **el mismo árbol cada vez**, así una
corrida es comparable con la anterior.

Y ahora el caso del archivo faltante — **borrá uno a propósito**:

```powershell
Remove-Item C:\ruta\share-dev\RVI9\DEV\T0014.PDF
```

| Caso | Esperado |
|---|---|
| C16 (= T0014 sin archivo) | `SOURCE_FILE_MISSING` · FALLO |

Antes de correr, mirá lo que S1 va a producir:

```powershell
cmcourier inspect rvabrep -c config-dev.yaml
cmcourier inspect mapping DC01 -c config-dev.yaml --system 1
```

---

## Paso 5 — Correr y verificar

```powershell
cmcourier run -c config-dev.yaml
cmcourier batch show -c config-dev.yaml <batch_id>
```

La matriz de verificación. **Si alguna línea no aparece, ese camino no se
está ejercitando** — y un camino que no se ejercita es un camino que no
sabés si funciona:

```
BALDE      RAZON                  ID_RVI  DOCS  ← caso
EXCLUIDO   DELETED_AT_SOURCE      DC01     3    ← C08 a/b/c
EXCLUIDO   EXCLUDED_BY_FILTER     AF01     1    ← C09
EXCLUIDO   CLIENT_NOT_ACTIVE      DC01     2    ← C05, C11
BLOQUEADO  CODE_NOT_MAPPED        ZZ99     1    ← C10
BLOQUEADO  IDENTITY_UNRESOLVED    DC01     1    ← C06
BLOQUEADO  SOURCE_ROW_INCOMPLETE  DC01     1    ← C07
FALLO      SOURCE_FILE_MISSING    PP16     1    ← C16
```

Y lo más importante de todo: **que los números cuadren.** Si `batch show`
te imprime la línea `!! DESCUADRE`, hay documentos del origen sin
explicación y eso es un bug — del sistema o de tu preparación, pero un
bug.

### Segunda corrida: el salteo

Volvé a correr **sin tocar nada**:

```powershell
cmcourier run -c config-dev.yaml
cmcourier batch show -c config-dev.yaml <batch_id_2>
```

| Caso | Esperado |
|---|---|
| C17 | todos los que subieron en la corrida 1 → `ALREADY_UPLOADED` · EXCLUIDO |

---

## Paso 6 — El sync, las dos direcciones

Estos casos se preparan tocando la tabla de log a mano, porque simulan lo
que hizo **el otro programa**.

```sql
-- C18: documento que subiste, pero el log quedó en 'F'
UPDATE RVILIB.RVIMGLOG SET STSCOD = 'F' WHERE TRNNUM = 'T0001';

-- C19: documento que subiste, pero el log quedó en 'I' (claim colgado)
UPDATE RVILIB.RVIMGLOG SET STSCOD = 'I' WHERE TRNNUM = 'T0002';

-- C20: mismo estado, OBJIDN distinto → DOS objetos en CM
UPDATE RVILIB.RVIMGLOG SET OBJIDN = 'objeto-de-otro-programa'
WHERE TRNNUM = 'T0003';

-- C21: una fila que CMCourier nunca vio, subida por el proceso Java
INSERT INTO RVILIB.RVIMGLOG
  (SISCOD, TRNNUM, DOCFRM, IMGARC, IMGTIP, CTECIF, CTENUM,
   STSCOD, IDNBAC, TIPIDN, OBJIDN, NUMREI, EERRMSG)
VALUES ('1','T9001','DC01','T9001.PDF','P','ACMESA001001', 1000,
        'O','DC01','','objeto-java-001', 0, '');

-- C22: una fila que el proceso Java FALLÓ
INSERT INTO RVILIB.RVIMGLOG
  (SISCOD, TRNNUM, DOCFRM, IMGARC, IMGTIP, CTECIF, CTENUM,
   STSCOD, IDNBAC, TIPIDN, OBJIDN, NUMREI, EERRMSG)
VALUES ('1','T9002','DC01','T9002.PDF','P','ACMESA001001', 1000,
        'F','DC01','','', 1, 'error del proceso java');
```

Después:

```powershell
cmcourier sync status  -c config-dev.yaml            # reporta divergencias
cmcourier sync recover -c config-dev.yaml            # simula (batch_id VACÍO = todos)
cmcourier sync recover -c config-dev.yaml --apply
cmcourier sync pull    -c config-dev.yaml            # simula
cmcourier sync pull    -c config-dev.yaml --apply
```

| Caso | Esperado |
|---|---|
| C18 | `recover` lo clasifica **stale** y lo actualiza a `'O'` |
| C19 | ídem, `'I'` → `'O'` |
| C20 | **divergencia**: se reporta y **no se toca** |
| C21 | `pull` lo importa como `S5_DONE` |
| C22 | `pull` lo importa como `S5_FAILED` + `EXTERNAL_FAILURE` |

> C18 y C19 son los que prueban el arreglo de la 151. Antes se contaban
> como *"ya presente"* y quedaban divergentes para siempre.

---

## Escenarios que NO se preparan con datos

| Razón | Cómo provocarla |
|---|---|
| `CM_TIMEOUT` / `CM_ERROR_5XX` / `CM_TRANSPORT` | apagá el CMIS a mitad de corrida, o apuntá `cmis.base_url` a un puerto muerto |
| `CM_REJECTED_4XX` | marcá `usar` una propiedad que CM no acepta, o mandá un valor más largo que su `max_length` |
| `CANCELLED` | `Ctrl-C` a mitad de corrida |
| `CLAIM_LOST` | poné a mano `STSCOD='I'` en una fila **antes** de que S5 la tome |
| `METADATA_UNRESOLVED` | sacá una entrada de `field_sources` que un tipo tenga en `usar` |
| `TYPE_NOT_IN_MANIFEST` | agregá un IDCM al `MapeoRVI_CM.csv` que no exista en el manifest |
| `ASSEMBLY_FAILED` | reemplazá un PDF por un archivo de texto con extensión `.PDF` |
| `INDEXING_FAILED` | rompé a propósito la query de `indexing.source` |

---

## Reset entre corridas

```sql
DELETE FROM RVILIB.RVABREP  WHERE ABAANB LIKE 'T0%' OR ABAANB LIKE 'T9%';
DELETE FROM RVILIB.RVIMGLOG WHERE TRNNUM LIKE 'T0%' OR TRNNUM LIKE 'T9%';
```

Y del lado local, borrá el SQLite de tracking que declara
`tracking.db_path` en tu YAML. Con eso volvés a foja cero y podés repetir
la matriz completa.

> El prefijo `T0`/`T9` en los `TRNNUM` no es decorativo: es lo que hace
> que el reset sea **quirúrgico** y no un `DELETE FROM` a ciegas. Si
> compartís el RVABREP de desarrollo con alguien más, te va a importar.

---

## Ver también

- [Leer el censo de un batch](read-the-batch-census.md) — qué hacer con cada balde
- [Cadenas de identidad](../identity-chain.md)
- [Formato del valor de un metadato](../metadata-format.md)
- [Elegibilidad por cliente activo](../client-eligibility.md)
- [Sincronización con AS400](../as400-sync.md)
