# How-to: Pre-flight de destino CMIS (038)

> [← Volver al índice](../INDEX.md) · [How-to](README.md)

> Estado: `[0.41.0]` y posterior. Cubre los nuevos checks de doctor bajo
> el grupo `cm-targets`, las columnas `CMISFolder` / `CMISPropertyId` de
> los CSVs de mapping split, los eventos de trace de payload de upload,
> y la perilla de debug `unmask_pii`.
>
> **145 — sólo aplica a `mapping` en modo split.** Las columnas
> `CMISFolder` / `CMISPropertyId` de este runbook son del modo split
> (`rvi_cm_csv_path` + `metadatos_csv_path`, deprecado). En modo
> **manifest** (`rvi_cm_csv_path` + `type_manifest_path`, recomendado) la
> carpeta y los ids de propiedad CMIS salen del manifest JSON — no hay
> columnas que llenar y el check equivalente es `cm_manifest` /
> `types check`. Ver [`cm-type-manifest.md`](cm-type-manifest.md). El
> modo consolidado (`csv_path`) tampoco tiene estas columnas.

Antes de cualquier batch productivo querés que te respondan: **¿mi
destino CMIS realmente acepta lo que CMCourier está por mandar?** Errores
4xx en medio de un batch significan horas perdidas y una DB de tracking
llena de filas `S5_FAILED`. Este runbook recorre las tres piezas de
pre-flight que 038 shippea para que encuentres el problema **antes**
del primer POST.

## TL;DR

```bash
# Llená las nuevas columnas opcionales de los CSVs split (una fila para arrancar):
#   MapeoRVI_CM.CMISFolder        = el path de carpeta CMIS bajo root
#   MetadatosCM.CMISPropertyId    = el id de propiedad CMIS a nivel wire

# Después, contra cualquier endpoint CMIS (Alfresco staging o el CM del banco):
cmcourier doctor --config config.yaml --check cm-targets
```

Querés tres PASSes verdes:

- `cm_type_alignment` — cada `CMISType` resuelve en el servidor.
- `cmis_folders_exist` — cada `CMISFolder` es un `cmis:folder` en el servidor.
- `cmis_properties_alignment` — cada par `(CMISType, CMISPropertyId)` de
  MetadatosCM está declarado en el `propertyDefinitions` de ese tipo.

Si alguno es FAIL, arreglalo en tu config o en CMIS antes de correr la
pipeline. El doctor sale non-zero así cualquier wrapper de CI / cron aborta.

## §1 — Las dos columnas nuevas del CSV (modo split)

### `MapeoRVI_CM.CMISFolder`

| Columna | Comportamiento cuando está seteada | Comportamiento cuando está vacía / ausente |
| --- | --- | --- |
| `CMISFolder` | La URL de upload de S5 se vuelve `{base}/{repo}/root/{CMISFolder}`. El check `cmis_folders_exist` del doctor verifica que cada valor único no vacío sea una carpeta en el servidor CMIS. | S5 cae al `cm_folder` derivado (`/$type/BAC_<clase_id>` según la spec). `cmis_folders_exist` saltea el check entero (SKIP). |

La columna es **totalmente aditiva** — los CSVs pre-038 funcionan sin cambios.

Fila sample (modo split, `MapeoRVI_CM.csv`):

```csv
IDSistema,IDRVI,IDCM,IDClaseDocumental,CMISType,CMISFolder
,FB01,CN01,01.01.01.01.01,D:cmcourier:bacDoc,/cmcourier-staging/CN01
```

### `MetadatosCM.CMISPropertyId`

El "catálogo de propiedades" — la traducción nombre amigable → id de
propiedad CMIS a nivel wire por `IDCorto`.

| Columna | Comportamiento cuando está seteada | Comportamiento cuando está vacía / ausente |
| --- | --- | --- |
| `CMISPropertyId` | `MetadataService.resolve` reescribe la clave de propiedad resuelta del alias canónico (`BAC_CIF`) al id CMIS a nivel wire (`clbNonGroup.BAC_CIF` en PRD, `cmcourier:BAC_CIF` en staging). El check `cmis_properties_alignment` del doctor cruza cada par con el `propertyDefinitions` del tipo CMIS. | La clave de propiedad resuelta queda canónica — comportamiento pre-038. `cmis_properties_alignment` saltea (SKIP). |

Filas sample (modo split, `MetadatosCM.csv`):

```csv
IDCorto,Metadato,Requerido,CMISPropertyId
CN01,CIF,Yes,cmcourier:BAC_CIF
CN01,Nombre_Cliente,Yes,cmcourier:Nombre_Cliente
CN01,Short_Name,Yes,cmcourier:Short_Name
```

> Catálogos parciales son válidos — una celda vacía para un metadato
> mantiene la clave de esa propiedad canónica mientras el resto se traduce.

## §2 — Leyendo `doctor --check cm-targets`

```bash
cmcourier doctor --config config.yaml --check cm-targets
```

Vas a ver (en orden):

1. `cm_type_alignment` — cada `cm_object_type` único (del `CMISType`
   de MapeoRVI si está seteado, si no la forma derivada) resuelve vía
   `GET ?cmisselector=typeDefinition`.
2. `cmis_folders_exist` (038) — cada `CMISFolder` único no vacío es una
   `cmis:folder` en el servidor. Read-only — nunca crea nada.
3. `cmis_properties_alignment` (038) — cada par `(CMISType,
   CMISPropertyId)` está en el `propertyDefinitions` del tipo.

### Cómo se ve un FAIL

```
cmis_folders_exist
  status: FAIL
  message: 2 CMIS folder(s) missing on the server. Create them in CMIS before running the pipeline.
  details:
    missing_folders: /cmcourier-staging/CN02,/cmcourier-staging/CN03
    checked_count: 5
```

```
cmis_properties_alignment
  status: FAIL
  message: 1 property gap(s): D:cmcourier:bacDoc missing 1: cmcourier:DoesNotExist
  details:
    missing: D:cmcourier:bacDoc missing 1: cmcourier:DoesNotExist
    checked_pairs: 6
```

Arreglá los items listados en CMIS (folders) o en tu MetadatosCM
(typo en `CMISPropertyId`) antes de continuar.

## §3 — Eventos de trace del payload de upload

Cada POST S5 exitoso ahora escribe un evento `s5_upload_attempt` a
`logs/<batch_id>/metrics.jsonl`. Cada POST que falla agrega un
evento `s5_upload_failed` con el status de respuesta, excerpt del body, y un
`curl_equivalent` ejecutable que reproduce el fallo.

### Leyendo intentos

```bash
jq -c 'select(.event=="s5_upload_attempt")' logs/<batch_id>/metrics.jsonl | head -3
```

Cada record lleva `url`, `object_type_id`, `document_name`, `mime_type`,
`content_bytes`, y un blob `properties_json` con la bolsa de propiedades
que estábamos por mandar. **Los valores PII están enmascarados por
default** (`cif`, `customer_name`, `account_number`, `phone`, `email`,
`dni`, etc., más sus variantes a nivel wire `clbNonGroup.BAC_CIF`,
`cmcourier:Nombre_Cliente`, etc.).

### Leyendo fallos

```bash
jq -c 'select(.event=="s5_upload_failed")' logs/<batch_id>/metrics.jsonl
```

Cada record de fallo lleva todo lo que lleva el intento más:

- `status_code`: el status HTTP (típicamente 4xx — ver gaps de propiedad abajo).
- `response_body`: truncado a 1024 chars.
- `curl_equivalent`: un curl ejecutable que reproduce el POST que falla
  (con `-u admin:***` y valores de propiedad enmascarados según el toggle de abajo).

## §4 — `observability.unmask_pii` — cuando realmente necesitás valores crudos

```yaml
observability:
  unmask_pii: true   # solo debugging — NUNCA en batches PRD
```

Cuando esta perilla es true:

- `s5_upload_attempt` y `s5_upload_failed` emiten valores crudos en
  `properties_json`.
- `curl_equivalent` también lleva valores crudos.
- El doctor emite un `WARN` llamado `unmask_pii_active` arriba de cada
  reporte, así un batch PRD perdido nunca corre sin que veas la desviación.

Las credenciales de `auth` (`-u user:pass`) **nunca** se desenmascaran —
siempre se renderizan como `-u admin:***` sin importar este flag.

## §5 — Disciplina operacional

- **Los administradores CMIS del banco son dueños del árbol de carpetas.** La
  primitiva `verify_folder_exists` del adapter es read-only — CMCourier
  ya no crea carpetas on demand. Si falta una carpeta, el doctor te
  dice cuál y la provisionás manualmente en CMIS.
- **Corré `doctor --check cm-targets` después de cualquier edición de CSV.** Un typo
  en `CMISPropertyId` es un error a nivel wire que el servidor del banco
  va a rechazar — mejor encontrarlo en 30 segundos con el doctor que
  después de 9 000 documentos.
- **El default de PII es enmascarado. Mantenelo así en PRD.** El toggle
  `unmask_pii` existe solo para debugging activo en staging o un batch
  de test controlado.
