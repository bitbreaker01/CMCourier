> [← Volver al índice](../INDEX.md) · [Reference](README.md)

# Error codes

Jerarquía completa de excepciones en `src/cmcourier/domain/exceptions.py`. Todas heredan de `CMCourierError`. Cada subclase concreta declara su `context` por keyword — los structured loggers lo extraen sin parsear el mensaje.

## Jerarquía

```
CMCourierError
├── ConfigurationError                  (startup)
├── TriggerError                        (S0)
├── IndexingError                       (S1, base)
│   ├── RVABREPNotFoundError
│   ├── RVABREPDeletedError
│   └── RVABREPDuplicateError
├── MappingError                        (S2, base)
│   └── IDRViNotMappedError
├── MetadataError                       (S3, base)
│   ├── SourceFailedError
│   └── DefaultValidationFailedError    (deprecada en 149 — nada la levanta)
├── AssemblyError                       (S4, base)
│   ├── SourceFileMissingError          (pickle-safe via __reduce__)
│   └── PDFAssemblyFailedError          (pickle-safe via __reduce__)
├── UploadError                         (S5, base)
│   ├── CMISClientError
│   ├── CMISServerError
│   └── RetriesExhaustedError
└── TrackingError                       (S6)
```

## Tabla por excepción

| Exception | Inherits from | Stage | Trigger | Operator action |
|-----------|---------------|-------|---------|-----------------|
| `CMCourierError` | `Exception` | — | Raíz. Nunca se lanza directamente. | — |
| `ConfigurationError` | `CMCourierError` | startup | YAML inválido, env var faltante, `trigger.kind` desalineado con el comando. | Revisar el YAML / variables de entorno. Exit code 2. |
| `TriggerError` | `CMCourierError` | S0 | Source de triggers inalcanzable, malformado o vacío. | Verificar `trigger.csv_path` / `trigger.scan_path` / conexión RVABREP. |
| `IndexingError` | `CMCourierError` | S1 | Base — captura cualquier falla de indexing. | — |
| `RVABREPNotFoundError` | `IndexingError` | S1 | No hay filas RVABREP para el par `(shortname, system_id)`. | Inspect: `cmcourier inspect rvabrep <short> <sys>`. Posible drift trigger ↔ RVABREP. |
| `RVABREPDeletedError` | `IndexingError` | S1 | Todas las filas RVABREP tienen `ABACST` no vacío. `deleted_count` indica cuántas. | Documento borrado. Excluir del trigger o filtrar upstream. |
| `RVABREPDuplicateError` | `IndexingError` | S1 | Matchearon múltiples filas cuando se esperaba una. | Investigar duplicados en RVABREP. |
| `MappingError` | `CMCourierError` | S2 | Base. | — |
| `IDRViNotMappedError` | `MappingError` | S2 | El `id_rvi` (`ABAHCD`) no está en el Modelo Documental. | `cmcourier inspect mapping <id_rvi>`. Agregar fila a `MapeoRVI_CM.csv` o filtrar en RVABREP. |
| `MetadataError` | `CMCourierError` | S3 | Base. | — |
| `SourceFailedError` | `MetadataError` | S3 | Una fuente de metadata lanzó excepción o devolvió sin valor y no hay fallback. `field_name` + `source`. | Revisar CSV/AS400 source. Considerar agregar `default_value`. |
| `DefaultValidationFailedError` | `MetadataError` | S3 | **Deprecada en 149: el runtime ya no la levanta.** Pre-149 el `default_value` se validaba contra el `allowed_pattern` de la PRIMERA fuente — magia que el YAML no declaraba y acoplamiento posicional. Hoy el default se formatea y se devuelve. | Ninguna. Si el default no matchea ningún patrón de sus fuentes, `types check` lo reporta como **INFO** (no bloquea: un marcador deliberado como `000000` es legítimo). |
| `AssemblyError` | `CMCourierError` | S4 | Base. | — |
| `SourceFileMissingError` | `AssemblyError` | S4 | El archivo en `assembly.source_root` no está. `file_path` lo identifica. **Tiene `__reduce__` para que cruce `ProcessPoolExecutor`.** | Verificar `assembly.source_root` y permisos. Recover desde tape / file server. |
| `PDFAssemblyFailedError` | `AssemblyError` | S4 | img2pdf / Pillow / PyPDF2 lanzó excepción. `txn_num` + `reason`. **Tiene `__reduce__`.** | Inspeccionar el archivo fuente (puede estar corrupto). Re-correr con `--from-stage 4`. |
| `UploadError` | `CMCourierError` | S5 | Base. | — |
| `CMISClientError` | `UploadError` | S5 | HTTP 4xx del server CMIS. `status_code` + `response_body`. **No se retrya** — el request está mal. | Doctor (`--check cm-types,cm-targets`). Revisar metadatos / type IDs / folder paths. |
| `CMISServerError` | `UploadError` | S5 | HTTP 5xx. Se retrya con backoff exponencial. | Si persiste, revisar salud del server CMIS. El circuit breaker abrirá tras 5xx + socket errors consecutivos. |
| `RetriesExhaustedError` | `UploadError` | S5 | El upload agotó `retry_max_attempts`. `txn_num` + `attempts`. | Investigar la causa raíz (red, CMIS, archivo). `batch retry-failed --stage S5`. |
| `TrackingError` | `CMCourierError` | S6 | Falla de escritura al SQLite tracking store. **Se loguea, no se propaga** — no bloquea el pipeline. | Revisar permisos sobre `tracking.db_path` y espacio en disco. |

## Patrón de captura por stage

Las clases base de stage permiten filtrar sin enumerar subclases:

```python
try:
    ...
except MappingError as exc:        # también captura IDRViNotMappedError
    log.error("mapping failed", **exc.context)
```

`exc.context` siempre es un `dict[str, object]` — esto está garantizado por `CMCourierError.__init__`.

## Picklability (066)

`SourceFileMissingError` y `PDFAssemblyFailedError` tienen `__reduce__` porque cruzan el boundary de `ProcessPoolExecutor` (S4 con `s4_use_processes: true`). Las otras excepciones quedan en threads y no necesitan ese hook.

## Ver también

- [`cli.md`](cli.md) — exit codes del CLI (1 ↔ stage failures, 2 ↔ `ConfigurationError`).
- [`tracking-db-schema.md`](tracking-db-schema.md) — qué `status` queda al lanzar cada excepción.
- [How-to: validation checklist](../how-to/validation-checklist.md) — `doctor` corre los checks que previenen casi todo lo de la fila `ConfigurationError`.
