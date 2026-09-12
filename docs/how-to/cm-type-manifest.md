# How-to: Manifest de tipos CM (145)

> [← Volver al índice](../INDEX.md) · [How-to](README.md)

> Disponible desde el cambio **145**. Reemplaza el mantenimiento a mano de
> `MetadatosCM.csv` por un manifest JSON descargado directo del servidor
> Content Manager (`typeDescendants`). El modo split (`MapeoRVI_CM.csv` +
> `MetadatosCM.csv`, 035) queda **deprecado** — sigue funcionando, pero
> el modo recomendado para instalaciones nuevas es **manifest**.

## Por qué existe esto

`MetadatosCM.csv` era una copia hecha a mano de lo que Content Manager ya
publica en `getTypeDefinition`. Medido contra el dump real de PRD
(`reference-data/cmis-responses/EjemploRespuestaCMIS.txt`, 368 tipos), el
CSV estaba desincronizado en las **dos** direcciones:

- el servidor marca `required:true` en 234–363 tipos para `BAC`,
  `BAC_Nombre_Documento`, `BAC_Expediente_Digital`,
  `BAC_Expedientes_Clientes`, `BAC_Nombre_Carpeta` y `BAC_ID_Corto`, y el
  CSV no los lista nunca;
- el CSV marca `Fvenc_Inicio` / `Fvenc_Fin` como `Requerido=Yes` y el
  servidor dice `required:false`;
- `CMISPropertyId` está poblado en 5 de 1231 filas: las otras 1226 clases
  irían al wire como `BAC_CIF` a secas, sin el prefijo `clbNonGroup.` que
  PRD exige.

Además `MapeoRVI_CM.csv` arrastraba columnas que o estaban muertas
(`IDSistema`: cero lecturas en `src/` antes de 145) o duplicaban al
servidor (`IDClaseDocumental`, `CMISType`, `CMISFolder`).

La idea del modo manifest: **no mantengas a mano lo que el servidor ya
sabe**. Lo único que el servidor NO sabe es (a) qué código RVI, por
sistema, va a qué clase CM, y (b) de dónde sale el VALOR de cada
propiedad — eso sigue viviendo en `metadata.field_sources` del YAML y no
cambia con este modo.

## `MapeoRVI_CM.csv` reducido a tres columnas

En modo manifest el CSV pasa de las columnas viejas (`IDClaseDocumental`,
`CMISType`, `CMISFolder`) a sólo tres:

```csv
IDSistema,IDRVI,IDCM
,FB01,DC01
RVI2,FB01,DC02
,CC03,DC01
```

- **`IDRVI`** e **`IDCM`** son obligatorias.
- **`IDSistema`** (default de columna: `IDSistema`, override con
  `mapping.rvi_cm_id_sistema_column`) es **opcional** — si la columna no
  existe, todas las filas caen al comodín (equivalente a dejarla vacía).

La fila con `IDSistema` vacío (fila 1, comodín) es la que rige por
default para cualquier sistema. La fila con `IDSistema=RVI2` (fila 2, un
override específico) sólo gana cuando el trigger trae ese sistema. La
clave del mapeo es `(sistema, IDRVI)`:

- `MappingService.get_mapping("FB01", system_id="rvi2")` → matchea la
  fila `(RVI2, FB01)` → `DC02`. La comparación de sistema es
  case-insensitive y con `strip()`.
- `MappingService.get_mapping("FB01", system_id="X")` (o sin
  `system_id`) → no hay fila específica para `X`, cae al comodín
  `(comodín, FB01)` → `DC01`.
- Un `IDRVI` que no aparece ni en el sistema específico ni en el comodín
  → `IDRViNotMappedError`.
- Dos filas con la MISMA clave `(sistema, IDRVI)` → gana la primera,
  WARNING en el log.

El sistema lo aporta el trigger que dispara la corrida (S2 en
`staged.py`, `recovery.py`, `doctor.py`, `inspect.py` vía el helper
`domain/models.py:trigger_system_id`). Los modos consolidado y split
ignoran el sistema — todo cae al comodín, comportamiento pre-145 intacto.

Todo lo demás de la fila (tipo CMIS, carpeta, propiedades requeridas)
sale ahora del **manifest**, buscando el `IDCM` de la fila. Si el `IDCM`
no existe en el manifest, la fila se descarta con WARNING y el código
queda en `MappingService.missing_cm_codes` — eso es justo lo que
`types check` (más abajo) y `doctor` reportan como CRITICAL.

## El flujo completo

```
types discover  →  revisar (CLI `review` o pestaña M·MODELO)  →  types check  →  (periódico) types diff / types update
```

### 1. Descubrir

```bash
cmcourier types discover --config config.yaml
```

Baja el árbol completo de `typeDescendants` del servidor CMIS y escribe
el manifest en `mapping.type_manifest_path` (o `--manifest PATH`).
Verifica además que cada carpeta derivada exista en el servidor
(`--no-verify-folders` para saltear ese paso y ir más rápido). El
progreso va a stderr, igual que `sync recover` (144):
`descargando tipos 0/0` → `procesando tipos k/N` → `verificando carpetas
k/N`.

Si el manifest ya tiene tipos marcados como revisados, `discover` se
niega salvo `--force` — lo normal, una vez que ya arrancaste a operar,
es `types update` (ver más abajo), no volver a descubrir de cero.

### 2. Revisar: "usar" vs "omitir"

Cada propiedad escribible de cada tipo tiene una **decisión**:
`usar` (viaja al wire en cada upload) u `omitir` (no se manda; gana el
default del servidor). Al descubrir, la decisión se sugiere así:

- **requerida y sin default en el servidor** → sugerida `usar` (si no
  la mandás, el servidor rechaza el create).
- **con default, o no requerida** → sugerida `omitir` (dejar que el
  servidor ponga lo suyo).

Un tipo entra con `reviewed: false` hasta que el operador se hace cargo
de esas decisiones explícitamente. Dos formas de revisar:

**CLI, offline** (no necesita `--config` ni credenciales CMIS):

```bash
cmcourier types review DC01 \
  --use BAC_CIF \
  --omit BAC_Nombre_Carpeta \
  --done
```

`--use` / `--omit` aceptan el id de wire completo (`clbNonGroup.BAC_CIF`)
o el nombre canónico (`BAC_CIF`) — el nombre canónico es el id de wire
sin su prefijo hasta el último `.` o `:` (`clbNonGroup.BAC_CIF` →
`BAC_CIF`, `cmcourier:BAC_CIF` → `BAC_CIF`), la misma clave que usa
`metadata.field_sources` en el YAML. Marcar `usar` una propiedad que hoy
sale con su default del servidor **exige** que también le des un
`field_source` en el YAML — si no, `types check` la marca CRITICAL.

**Consola, pestaña `M·MODELO`**: ver
[`explanation/operations-console.md`](../explanation/operations-console.md#pestaña-mmodelo)
para el detalle de botones y teclas. Funciona offline también — sólo
Descubrir/Comparar/Actualizar necesitan credenciales CMIS.

### 3. Verificar la alineación: `types check`

```bash
cmcourier types check --config config.yaml
```

Cruza manifest ↔ YAML (`metadata.field_sources`) ↔ CSV
(`MapeoRVI_CM.csv`) y agrupa hallazgos por severidad:

- **CRITICAL** (exit 1): código `IDCM` del CSV ausente en el manifest;
  propiedad `usar` sin entrada `metadata.field_sources`; tipo que el
  servidor dejó de publicar (`missing_on_server`).
- **WARNING**: tipo sin revisar (`reviewed: false`); carpeta que no
  existe en el servidor (`folder_ok is False`); propiedad requerida sin
  default marcada `omitir`; un `field_sources` cuyo largo declarado
  (valor fijo o patrón de validación) excede el `max_length` del CM;
  propiedad `usar` de tipo `datetime`/`integer`/`boolean` cuya fuente es
  un valor fijo que no parsea; un `field_aliases` colgado (más abajo).
- **INFO**: entradas de `field_sources` que ningún tipo **auditado** usa;
  un `default_value` que —ya formateado con el `format` de campo— no
  matchea NINGÚN `allowed_pattern` de sus fuentes (149). Este último es
  INFO y no WARNING a propósito: un default deliberadamente distinto de
  los datos reales (un marcador `000000` que después se busca y se
  corrige) es una técnica legítima. El check informa, no juzga. Sale una
  vez por campo, con los patrones listados en orden de config:

  ```
  metadata.field_sources.BAC_Num_Cuenta_Tarjeta: el default '000000' no matchea
  ningún allowed_pattern de sus fuentes ('^[0-9]{14,16}$')
  ```

  Es la red de seguridad que 149 le sacó al runtime, donde el default se
  validaba en silencio contra el patrón de la PRIMERA fuente y mataba
  documentos por un error de config.

#### Qué tipos se auditan: `--scope`

El manifest puede tener 362 clases y el banco usar 40. Qué subconjunto
mira el check lo decide `--scope`:

| `--scope` | Audita | Para qué |
| --- | --- | --- |
| `mapped` | sólo los `IDCM` que referencia `MapeoRVI_CM.csv` | preflight del pipeline: lo único que puede romper una corrida de HOY |
| `reviewed` (**default**) | ésos **más** todo tipo con `revisado ✓` | el flujo de revisión: lo que declaraste que pensás usar |
| `all` | el manifest entero, revisado o no | barrido completo, típicamente ruidoso |

`--all` es el atajo de `--scope all`; si se dan los dos y no coinciden,
gana `--all`.

**Por qué el default cambió a `reviewed`.** Caso real: revisás `AF01` en
la pestaña `M·MODELO`, dejás `BAC_Sucursal` y `BAC_Num_Afiliado` en
`usar`, marcás `revisado ✓` — pero todavía no agregaste `AF01` al
`MapeoRVI_CM.csv` porque el banco no habilitó ese trámite. Ninguna de
las dos propiedades tiene entrada en `metadata.field_sources`. Con el
alcance viejo (`mapped`) el check se quedaba **callado**: `AF01` no
estaba en el CSV, así que no existía para nadie. El día que lo mapeás,
el primer upload revienta con `no field_sources config for field`.
Marcar un tipo revisado es declarar "lo pienso usar", así que ahora el
CRITICAL sale hoy:

```console
$ cmcourier types check --config config.yaml
CRITICAL (2):
  [AF01/clbNonGroup.BAC_Sucursal] propiedad 'usar' sin entrada metadata.field_sources.BAC_Sucursal
  [AF01/clbNonGroup.BAC_Num_Afiliado] propiedad 'usar' sin entrada metadata.field_sources.BAC_Num_Afiliado

$ cmcourier types check --config config.yaml --scope mapped   # el alcance viejo
Sin hallazgos: manifest, YAML y CSV están alineados.
```

El WARNING de "el tipo todavía no fue revisado" sigue saliendo **sólo**
contra tipos mapeados en cualquier `scope`: bajo `all` serían cientos de
líneas inútiles, y bajo `reviewed` sería una contradicción. El orden es
estable — primero los mapeados (ordenados), después los que suma el
`scope` (ordenados) — así que ampliar el alcance agrega líneas al final,
no reordena lo que ya conocés.

El `doctor` es la excepción deliberada: su check `cm_manifest` usa
siempre `scope="mapped"` porque es un preflight del pipeline, y un tipo
que el CSV no referencia no lo puede tocar ningún upload. Un `AF01`
revisado y roto **no** frena una corrida.

#### Alias

El chequeo honra `metadata.field_aliases` igual que el runtime: una
propiedad del manifest puede llegar a su entrada de `field_sources` a
través de un alias (comparado sin distinguir mayúsculas), y en ese caso
no hay CRITICAL ni INFO — es exactamente la config que sube bien. Si el
alias existe pero apunta a una entrada que **no** está declarada, eso sí
es CRITICAL: el upload revienta con `no field_sources config for field`.
Los WARNING nombran siempre la llave resuelta de `field_sources` (la que
hay que editar), no el nombre de la propiedad.

Además, **todo** alias cuyo destino no exista da un WARNING propio, lo
consulte alguien o no:

```console
WARNING (1):
  metadata.field_aliases.Shortname apunta a field_sources.BAC_Short_Name, que no existe
```

Es el alias que quedó colgado después de renombrar la entrada de
`field_sources` — nadie lo consulta justamente por eso, y por eso el
CRITICAL por-propiedad nunca lo denunciaba. Si además lo consulta una
propiedad `usar` de un tipo auditado, salen los **dos** hallazgos: el
CRITICAL dice qué upload se rompe, el WARNING qué línea del YAML hay que
borrar o arreglar.

`--json` para consumo por script. El check offline equivalente en
`doctor` es `cm_manifest` — corre lo mismo pero sólo reporta los
CRITICAL (ver [`reference/cli.md`](../reference/cli.md#doctor)); SKIP
cuando `mapping` no está en modo manifest.

### 4. Mantenimiento periódico: `diff` / `update`

Content Manager cambia con el tiempo — el banco agrega un campo, cambia
un `max_length`, deja de publicar un tipo. Para no descubrir todo de
nuevo (lo que pisaría tus decisiones):

```bash
cmcourier types diff --config config.yaml     # exit 1 si hay diferencias
cmcourier types update --config config.yaml   # mergea conservando lo revisado
```

`update` trae los cambios del servidor: las propiedades que sobreviven
conservan su decisión `usar`/`omitir`, las nuevas entran con la decisión
sugerida, y el tipo tocado vuelve a `reviewed: false` con la lista de
`changes` (`+ clbNonGroup.BAC_Nueva (required)`, `~ max_length 20 → 30`,
`- clbNonGroup.BAC_Vieja`) para que lo revises de nuevo. Un tipo que el
servidor ya no publica **no se borra** — se marca `missing_on_server:
true` y el operador decide. Una carpeta fijada a mano (`folder_source:
"manual"`) **no se pisa** aunque cambie el `local_name` del tipo.
`--only IDCM` acota el merge a tipos puntuales.

## Cómo se deriva el ID corto

El "ID corto" (`DC01`, `PT55`, etc.) es la clave con la que se indexa el
manifest y la que aparece en `MapeoRVI_CM.IDCM`:

1. Primero se busca el `defaultValue` de la propiedad cuyo nombre
   canónico es `BAC_ID_Corto`.
2. Si no hay, se prueba el prefijo `^(\S+) - ` del `displayName`
   (`"PT95 - Escritura Hipotecaria"` → `PT95`).
3. Si ninguno matchea, el tipo va a `without_code` del manifest — no
   entra al mapeo, y el operador decide si le importa.

## ID corto compartido

En PRD hay dos clases documentales distintas que declaran el MISMO ID
corto (`DC35`: `BAC_01_01_01_03_07_01` y `..._02`). Es dato real del
servidor, así que **el discover no aborta**: elige un ganador
determinístico y guarda a los demás como candidatos.

El ganador se elige así, y siempre igual entre corridas:

1. Gana el tipo cuyo `displayName` empieza con `"<ID corto> - "` — la
   convención con la que CM nombra la clase "buena".
2. Si ninguno la cumple, o la cumplen varios, gana el primero en el
   orden en que el servidor los publicó.

Los que pierden van **enteros** (con sus propiedades y decisiones) a
`duplicates` en el manifest, y el ganador queda `reviewed: false` con
una línea en `changes` por cada candidato.

**`types discover` avisa** con un WARNING por candidato (la pestaña
`M·MODELO` escribe la misma línea en su log después de Descubrir o
Actualizar):

```text
WARNING: ID corto compartido DC35: ganador $t!-2_BAC_..._01v-1 (DC35 - Contrato);
candidato $t!-2_BAC_..._02v-1 (Contrato viejo)
```

**`types show DC35`** lista los candidatos al final del encabezado:

```text
Candidatos con el mismo ID corto:
  $t!-2_BAC_..._02v-1 (Contrato viejo)
  elegí uno con: cmcourier types review DC35 --type-id TYPE_ID
```

**`types review DC35 --type-id ...`** promueve el candidato que elijas:
entra con SUS propiedades y SUS decisiones (ya estaban completas en
`duplicates`, no hace falta volver a hablar con el servidor), el ganador
anterior pasa a ser candidato, y el tipo queda `reviewed: false` con
`changes: ["elegido a mano sobre <type_id anterior>"]`. Se aplica ANTES
que `--use` / `--omit` / `--folder`, así que podés hacer todo de una:

```bash
cmcourier types review DC35 \
  --type-id '$t!-2_BAC_..._02v-1' --use BAC_CIF --done
```

`diff` y `update` respetan tu elección: si el `type_id` que elegiste
sigue siendo uno de los candidatos vivos, ese es el ganador con el que
comparan. Sin eso, cada `diff` reportaría un cambio de `type_id`
fantasma y cada `update` te pisaría la elección.

**`types check`** lo reporta por candidato: WARNING si el `MapeoRVI_CM`
no usa ese ID corto (ruido del servidor, no rompe nada), CRITICAL si SÍ
lo usa — porque ahí los uploads podrían estar yendo a la clase
equivocada y lo tenés que resolver vos.

## Cómo se deriva la carpeta

La carpeta CMIS de cada tipo se deriva como `/$type/<localName>` (mismo
fallback histórico de `compute_cm_folder`, ahora partiendo del
`localName` real que publica el servidor). `folder_ok` queda en `true` /
`false` según `IUploader.verify_folder_exists` durante el discover
(`null` = todavía no verificada — por ejemplo con
`--no-verify-folders`). El operador puede fijarla a mano con
`types review DC01 --folder /custom/path`; a partir de ahí
`folder_source` pasa a `"manual"` y ni `update` ni un cambio de
`local_name` la vuelven a pisar.

## El JSON del manifest

Un solo archivo, pensado para vivir versionado al lado del YAML —
claves ordenadas, `indent=2`, `ensure_ascii=False`, escritura atómica
(tmp + `os.replace`) para que un `git diff` muestre exactamente qué
cambió. Ejemplo real (un tipo con dos propiedades, una `usar` y una
`omitir`, más un tipo sin código):

```json
{
  "discovered_at": "2026-08-30T12:00:00+00:00",
  "duplicates": [],
  "repository_id": "repo1",
  "service_url": "https://cm.bank.example/cmis/browser",
  "types": {
    "DC01": {
      "changes": [],
      "decisions": {
        "clbNonGroup.BAC_CIF": "usar",
        "clbNonGroup.BAC_ID_Corto": "omitir"
      },
      "display_name": "DC01 - Documento Cliente",
      "folder": "/$type/bacDoc",
      "folder_ok": true,
      "folder_source": "derivada",
      "local_name": "bacDoc",
      "missing_on_server": false,
      "properties": [
        {
          "cardinality": "single",
          "choices": [],
          "default_value": null,
          "display_name": "CIF",
          "id": "clbNonGroup.BAC_CIF",
          "inherited": false,
          "max_length": 20,
          "property_type": "string",
          "required": true,
          "updatability": "oncreate"
        },
        {
          "cardinality": "single",
          "choices": [],
          "default_value": "DC01",
          "display_name": "ID Corto",
          "id": "clbNonGroup.BAC_ID_Corto",
          "inherited": false,
          "max_length": 10,
          "property_type": "string",
          "required": true,
          "updatability": "oncreate"
        }
      ],
      "reviewed": true,
      "type_id": "D:cmcourier:bacDoc"
    }
  },
  "version": 1,
  "without_code": [
    ["D:cmcourier:legacyDoc", "Documento Legacy Sin Codigo"]
  ]
}
```

Sólo se guardan las propiedades **escribibles** (`updatability` en
`readwrite` / `oncreate`); las readonly no le sirven a nadie aguas abajo.
`cmis:name` y `cmis:objectTypeId` se excluyen porque las pone el código
en cada upload, no el operador.

`duplicates` es la lista de los candidatos que perdieron un ID corto
compartido: cada elemento tiene la MISMA forma que un value de `types`
más su `id_corto` (que en el dict es la clave y en una lista no tendría
dónde vivir). Un manifest viejo sin la clave se lee igual: se asume
vacía.

## YAML: activar el modo manifest

```yaml
mapping:
  rvi_cm_csv_path: /data/MapeoRVI_CM.csv
  type_manifest_path: /data/cm-type-manifest.json
  # opcional, sólo si tu MapeoRVI_CM no usa el nombre de columna default:
  # rvi_cm_id_sistema_column: "IDSistema"
```

El validador de `MappingConfig` exige `rvi_cm_csv_path` más
**exactamente uno** de `metadatos_csv_path` (split, deprecado) /
`type_manifest_path` (manifest). No podés tener los dos ni ninguno.

## Qué NO cambia

S3 (resolución de metadata), `staged.py`, NIARVILOG (`IDNBAC=id_corto`,
`TIPIDN=cmis_type`), `[0] PRUEBA` y el resto de `doctor` siguen leyendo
el mismo `CMMapping` de siempre — el modo manifest sólo cambia de dónde
sale ese objeto, no su forma.

## Ver también

- [`../reference/cli.md`](../reference/cli.md#grupo-types-145) — el
  grupo `types` completo, con `--help` real de cada subcomando.
- [`../reference/config-schema.md`](../reference/config-schema.md#mapping-mapping)
  — schema de `MappingConfig`.
- [`../explanation/operations-console.md`](../explanation/operations-console.md#pestaña-mmodelo)
  — la pestaña `M·MODELO`.
- [`cmis-target-preflight.md`](cmis-target-preflight.md) — el doctor
  `cm-targets` para instalaciones en modo split.
- `specs/145-cm-type-manifest/spec.md` — la spec completa.
