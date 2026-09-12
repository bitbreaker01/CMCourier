# Agregar una propiedad CMIS nueva

> [← Volver al índice](../../INDEX.md) · [How-to](../README.md) · [Developer](README.md)

Una "propiedad CMIS nueva" suele ser un nuevo metadato que el banco pide indexar en Content Manager: CIF, fecha de operación, código de sucursal, etc. CMCourier resuelve cada propiedad como un **campo lógico** cuyo `CMISPropertyId` físico vive en el CSV de `MetadatosCM` del Modelo Documental. La cadena de resolución sale de `metadata.field_sources` con fallback por capas.

## Cuándo aplica

- El banco agrega un metadato nuevo al Modelo Documental.
- Necesitás resolver el mismo campo lógico desde otra fuente (ej. ahora viene de un CSV cliente que antes no existía).
- Querés cambiar el orden de fallback de un campo ya configurado.

## Pasos

### 1. Definí el nombre canónico y agregalo a `metadata.field_aliases`

Por convención el nombre canónico arranca con `BAC_` (alineado al server CM). Si el campo lógico se va a invocar desde el Modelo Documental con un nombre amigable (`CIF`, `OperationDate`), registrá el alias en YAML:

```yaml
metadata:
  field_aliases:
    CIF: BAC_CIF
    OperationDate: BAC_OPDT          # nuevo
```

Cualquier nombre que aparezca en `MetadatosCM.csv` se normaliza por `field_aliases` antes de buscarse en `field_sources`. Si el campo no tiene alias (porque el nombre amigable y el canónico coinciden), saltá este paso.

### 2. Agregá la entrada en `metadata.field_sources` con la cadena de fallback

```yaml
metadata:
  field_sources:
    BAC_OPDT:
      sources:
        - source_type: rvabrep                       # primero RVABREP
          lookup_value_column: creation_date_column
        - source_type: "csv:clients"                 # luego CSV cliente
          lookup_key_column: CIF
          lookup_value_column: OPDT
          validation:
            allowed_pattern: '^\d{4}-\d{2}-\d{2}$'
        - source_type: trigger                       # último recurso: el trigger
          lookup_value_column: cif
      default_value: "1900-01-01"
```

`source_type` válidos (validados por `FieldSourceItem._validate_source_type`):

- `trigger` — atributo del `Trigger` (o `audit_row()` para subtipos basados en fila).
- `rvabrep` — atributo del `RVABREPDocument`.
- `csv:<alias>` — CSV registrado en `metadata.sources` con `alias=<alias>`.
- `as400:<alias>` / `mssql:<alias>` (130) — fuente AS400/SQL Server con nombre, registrada en `metadata.sources`. Implementado desde el cambio 084 (`MetadataService._fetch_from_source` → `_fetch_lookup`, con prefetch en memoria o `get_by_fields` por documento según `prefetch_enabled`) — la nota anterior sobre `NotImplementedError` quedó obsoleta.

### 3. Registrá la fuente CSV/AS400 si todavía no existe

Si el `source_type` apunta a un alias nuevo, sumalo a `metadata.sources`:

```yaml
metadata:
  sources:
    - kind: csv
      alias: clients
      csv_path: /path/to/clients.csv
```

Para AS400 (con `kind: as400`), pasa `as400_connection` y exactamente uno de `table` o `query` — ver `As400MetadataSourceConfig`.

### 4. Vinculá la propiedad en el Modelo Documental

El `CMISPropertyId` final (lo que sale por el wire al servidor CMIS) NO se configura en `cmis.*`. De dónde sale depende del modo de `mapping`:

**Modo split (035, deprecado)**: del CSV `MetadatosCM.csv`, columna `CMISPropertyId` (configurable vía `mapping.metadatos_cmis_property_id_column`, default `"CMISPropertyId"`). Asegurate de que la fila correspondiente al `IDCorto` del documento tenga:

| IDCorto | Metadato | Requerido | CMISPropertyId |
|---------|----------|-----------|----------------|
| `BAC_CC03` | `BAC_OPDT` | `Yes` | `bac:opdt` |

**Modo manifest (145, recomendado)**: no hay CSV que editar. El `CMISPropertyId` sale directo del `id` de la propiedad tal cual la publica Content Manager (`clbNonGroup.BAC_OPDT`, `cmcourier:BAC_OPDT`, etc.), descubierto con `cmcourier types discover`. Para que la propiedad viaje al wire, marcala `usar` en el tipo correspondiente (`cmcourier types review <IDCM> --use BAC_OPDT`, o desde `M·MODELO`) — el nombre canónico que ves ahí (`BAC_OPDT`, sin el prefijo `clbNonGroup.`/`cmcourier:`) es la misma clave que `metadata.field_sources` espera. Ver [`cm-type-manifest.md`](../cm-type-manifest.md).

En ambos modos, el servicio de mapping (`services/mapping.py`) arma `CMMapping.cmis_property_ids` que luego `MetadataService` consulta para emitir la propiedad correcta. Si el campo no aparece ahí (fila de `MetadatosCM` faltante, o propiedad marcada `omitir`/inexistente en el manifest), no llega al uploader — y en modo manifest, `types check` te lo marca CRITICAL antes de que llegue a producción.

### 5. Test unit del campo nuevo

Agregar en `tests/unit/services/test_metadata.py`:

```python
def test_bac_opdt_resuelve_desde_rvabrep_primero(tmp_path: Path) -> None:
    # Armado: FieldSourceConfig con rvabrep como primer source, csv como fallback
    cfg = MetadataConfig(
        field_aliases={},
        field_sources={
            "BAC_OPDT": FieldSourceConfig(
                sources=(
                    SourceConfig(
                        source_type="rvabrep",
                        lookup_value_column="creation_date_column",
                    ),
                    SourceConfig(
                        source_type="csv:clients",
                        lookup_key_column="CIF",
                        lookup_value_column="OPDT",
                    ),
                ),
                default_value="1900-01-01",
            )
        },
    )
    # ... resto del test con TabularDataSource real sobre fixture CSV
```

Casos a cubrir mínimo: hit en el primer source, miss → hit en el segundo, miss en todos → `default_value` (formateado con el `format` de campo y **sin validar**, 149), miss en todos sin default → `SourceFailedError`, y una fuente cuyo valor no pasa su `allowed_pattern` → se descarta esa fuente y sigue la cadena.

### 6. Validá con doctor

```bash
cmcourier doctor --config tu-config.yaml --check metadata
cmcourier doctor --config tu-config.yaml --check cm-targets
```

- `--check metadata` corre `metadata_sources` y `sample_dry_run` (resuelve el primer documento real y muestra qué campos se resolvieron contra qué fuente).
- `--check cm-targets` corre `cmis_properties_alignment` y verifica que cada `CMISPropertyId` del Modelo Documental exista en la `typeDefinition` del server CM (no inventamos propiedades del lado cliente).

## Verificación

```bash
pytest tests/unit/services/test_metadata.py -v
pytest tests/unit/services/test_mapping.py -v
pytest tests/integration/pipeline/ -v -k metadata     # si toca el end-to-end de S3
cmcourier doctor --config tu-config.yaml --check metadata
cmcourier doctor --config tu-config.yaml --check cm-targets
```

## Gotchas

- **Caso-insensible solo en aliases**: `field_aliases` resuelve case-insensitive (vía `aliases_lower`), pero los `source_type` y `lookup_value_column` son case-sensitive. `OPDT` ≠ `opdt`.
- **CIF self-healed**: si un `ClientTrigger` arranca sin CIF y se resuelve más tarde, los CSV lookups del mismo `resolve()` ya usan el CIF healed (parámetro `cif_override`). No te asustes si ves el CIF aparecer "mágicamente" en el segundo source — es feature, no bug.
- **PII en logs**: `MetadataService` loguea NOMBRES de campos, nunca VALORES (Principio VIII). Si tu source nuevo necesita inspección, usá `unmask_pii: true` solo en dev; el doctor emite WARNING en arranque.
- **`prefetch_enabled: true`** (default) eagermente carga toda la fuente CSV al construir el servicio. Si la fuente es enorme, considerá `prefetch_enabled: false` (degrada a `get_by_fields` por documento, más lento pero menos RAM).
- **El cache de metadata (037)** invalida por `fields_hash`. Si cambia el set de campos requeridos (agregás `BAC_OPDT`), el hash cambia y las filas viejas del cache se ignoran sin tener que purgar manualmente.

## Ver también

- [`../cm-type-manifest.md`](../cm-type-manifest.md) — modo manifest (145): de dónde sale el `CMISPropertyId` cuando no hay `MetadatosCM.csv`
- [`../../reference/config-schema.md`](../../reference/config-schema.md) — sección `metadata.*`
- `src/cmcourier/services/metadata.py` — `MetadataService.resolve()` y `_fetch_from_source`
- `src/cmcourier/services/mapping.py` — cómo se arma `CMMapping.cmis_property_ids` desde `MetadatosCM.csv`
- `src/cmcourier/cli/doctor.py` — `_check_metadata_sources`, `_check_cmis_properties_alignment`
- [`document-cache.md`](../document-cache.md) — invalidación del cache cross-batch
