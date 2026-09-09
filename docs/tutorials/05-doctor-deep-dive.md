> [← Volver al índice](../INDEX.md) · [Tutoriales](README.md)

# 05 — `doctor` en Profundidad

`cmcourier doctor` es el comando que más vas a correr. Es el pre-flight check: valida que el config sea sano, que las conexiones estén vivas, que el mapping esté completo, que las fuentes de metadata abran, que los tipos y carpetas CMIS existan. Si pasa todos los checks, tenés alta confianza de que el `pipeline run` va a llegar a S5 sin volar.

En este tutorial entendés qué chequea cada grupo, cómo interpretar la salida y qué hacés si algo falla.

> La motivación de fondo: la constitución dice que ninguna corrida productiva se hace sin `doctor` verde. Por eso los pipelines llaman `doctor` automáticamente al arrancar (auto-doctor) salvo que pases `--skip-doctor`.

---

## Comando básico

```bash
cmcourier doctor --config prod.yaml
```

Sin flags adicionales, corre `--check all`. Output esperado en buen escenario:

```
=== CMCourier doctor ===
Config:  /etc/cmcourier/prod.yaml
Check:   all

[PASS] log_dir_writable
[PASS] cmis_connectivity
[PASS] as400_connectivity
[PASS] tracking_openable
[PASS] as400_sync
[PASS] mapping_completeness
[PASS] metadata_sources
[PASS] sample_dry_run
[PASS] cm_type_alignment
[PASS] cmis_folders_exist
[PASS] cmis_properties_alignment

Passed: 11   Failed: 0   Warn: 0   Skip: 0
Elapsed: 4.21s
```

Exit code `0` si todos los checks pasan, `1` si alguno falla. Los warnings no rebotan el exit code pero te llaman la atención.

---

## Los cinco grupos de checks

El flag `--check` te deja correr un subset cuando solo querés validar una parte.

| Grupo | Para qué |
|-------|----------|
| `connections` | Conectividad básica: log dir escribible, CMIS pingeable, AS400 conectable, tracking DB abrible, AS400 sync vivo |
| `mapping` | El archivo de mapping cubre todos los códigos RVI que vas a ver |
| `metadata` | Las fuentes de metadata abren bien + sample dry-run sobre los primeros triggers |
| `cm-types` | Los tipos CM declarados en mapping existen en CMIS |
| `cm-targets` | Los folders CMIS de destino existen + las propiedades CMIS están alineadas |
| `all` (default) | Todos los anteriores |

```bash
cmcourier doctor --config prod.yaml --check connections
cmcourier doctor --config prod.yaml --check mapping
cmcourier doctor --config prod.yaml --check metadata
cmcourier doctor --config prod.yaml --check cm-types
cmcourier doctor --config prod.yaml --check cm-targets
```

Útil cuando, por ejemplo, sabés que las conexiones están bien y solo querés validar que un mapping recién cargado esté completo.

---

## Cada check, qué valida

Fuente: `src/cmcourier/cli/doctor.py`.

### `unmask_pii_active`

- **Qué valida**: si `observability.unmask_pii: true` en el config.
- **Cuándo emite WARN**: siempre que esté en true.
- **Skip**: no se skipea, pero solo emite WARN, no PASS.
- **Por qué importa**: prender unmask_pii expone shortnames y CIFs en INFO. Aceptable para debug local; **prohibido en producción** (Principios Constitucionales V y VIII).

### `log_dir_writable`

- **Qué valida**: que `observability.log_dir` exista (o se pueda crear) y sea escribible.
- **Falla típica**: directorio sin permisos o en un volumen read-only.
- **Fix**: `mkdir -p` el directorio, asegurar permisos del usuario que corre `cmcourier`.

### `cmis_connectivity`

- **Qué valida**: que `cmis.base_url` responde a `GET` con las credenciales del entorno (`CMIS_USERNAME` / `CMIS_PASSWORD`).
- **Falla típica**: URL mal armada, certificado SSL inválido con `verify_ssl: true`, credenciales mal.
- **Fix**: chequear la URL en el browser/`curl`, verificar las env vars (`echo $CMIS_USERNAME`), si es self-signed bajar `verify_ssl: false` (default).

### `as400_connectivity`

- **Qué valida**: para cada source AS400 en el config (indexing, metadata, tracking sync), abre conexión.
- **Skip**: si no hay sources AS400 en todo el config.
- **Falla típica**: driver ODBC no instalado, host inaccesible, credenciales mal, puerto bloqueado por firewall.
- **Fix**: chequear `pyodbc.drivers()` desde Python para confirmar driver instalado; probar con `cmcourier as400-query --query "SELECT 1 FROM RVILIB.RVABREP FETCH FIRST 1 ROW ONLY"` (una tabla a la que el perfil tenga permiso). Si el login pasa pero la consulta de prueba falla con `PWS9801 … SAFENET`, el iSeries whitelistea objetos por perfil: fijá `probe_query` en la conexión con una tabla permitida (143).

### `tracking_openable`

- **Qué valida**: que `tracking.db_path` se pueda abrir (con WAL, synchronous=OFF). Si no existe, lo crea.
- **Falla típica**: directorio padre no existe, sin permisos, disco lleno.
- **Fix**: `mkdir -p $(dirname db_path)`, chequear permisos, `df -h`.

### `as400_sync`

- **Qué valida**: si `tracking.as400_sync.enabled: true`, abre conexión NIARVILOG y valida que las columnas configuradas existan.
- **Skip**: si `enabled: false`.
- **Falla típica**: la tabla NIARVILOG no existe en la library indicada, o los nombres físicos en `columns` no matchean.
- **Fix**: verificar con `as400-query "SELECT * FROM RVILIB.NIARVILOG FETCH FIRST 1 ROWS ONLY"`, ajustar `tracking.as400_sync.columns`.

### `mapping_completeness`

- **Qué valida**: que cada código de tipo RVI del archivo de mapping tenga su entrada CM (carpeta + tipo + propiedades).
- **Falla típica**: alguna fila del mapping CSV tiene celdas vacías o un `CMISType` que no existe.
- **Fix**: revisar el CSV de mapping en [`reference-data/csv/`](../../reference-data/csv/) como referencia; `inspect mapping-stats` te da el detalle.

### `metadata_sources`

- **Qué valida**: cada `metadata.sources` se puede abrir (CSV existe, AS400 source conecta).
- **Skip**: si no hay sources CSV (asume que AS400 ya se chequeó en `as400_connectivity`).
- **Falla típica**: path del CSV cambió, columnas key no existen en el header.
- **Fix**: chequear paths absolutos, columnas, encoding (utf-8 vs latin-1).

### `sample_dry_run`

- **Qué valida**: corre S0 + S1 + S2 + S3 contra los primeros triggers sin subir nada. Es un "puede el pipeline llegar a S4 con estos datos".
- **Skip**: si no hay triggers o no hay docs después de filtros.
- **Falla típica**: un trigger no resuelve en RVABREP, un código RVI no está mapeado, una propiedad de metadata no tiene fallback.
- **Fix**: corré `inspect trigger --shortname X --system Y` sobre el trigger que falló para ver dónde se cae.

### `cm_type_alignment`

- **Qué valida**: que los tipos CM declarados en el mapping (`CMISType`) existan en el repositorio CMIS (`getTypeDefinition`).
- **Skip**: si `cmis_connectivity` falló antes (no se puede chequear sin conexión).
- **Falla típica**: alguien renombró un tipo en Content Manager y el mapping quedó desactualizado.
- **Fix**: actualizar el mapping o crear el tipo faltante en CMIS.

### `cmis_folders_exist`

- **Qué valida**: cada `CMISFolder` del mapping existe en el repositorio CMIS (la query es read-only — `verify_folder_exists` no crea nada).
- **Skip**: si `cmis_connectivity` falló.
- **Falla típica**: la carpeta destino no fue creada todavía en Content Manager.
- **Fix**: crear la carpeta en CMIS (no es responsabilidad de CMCourier crearlas — es responsabilidad de la administración del CM).

### `cmis_properties_alignment`

- **Qué valida**: que las propiedades CMIS declaradas en el mapping (`CMISPropertyId`) existan en la definición del tipo CMIS correspondiente.
- **Skip**: si `cmis_connectivity` falló.
- **Falla típica**: un property id mal escrito o un property que cambió de tipo en CMIS.
- **Fix**: chequear el id en la definición CMIS del tipo (`getTypeDefinition` lista las properties).

---

## Anatomía del output

`doctor` devuelve un `DoctorReport` con:

- Lista de checks con su estado (`PASS | FAIL | WARN | SKIP`).
- Contadores: passed, failed, warn, skip.
- `elapsed_seconds` total.

Ejemplo con un fallo:

```
[PASS] log_dir_writable
[PASS] cmis_connectivity
[FAIL] mapping_completeness
       4 codes have no CM mapping: CC03, FF17, GG21, HH99
[PASS] tracking_openable
[SKIP] cm_type_alignment      (cmis_connectivity precondition not met)
[WARN] unmask_pii_active      (config.observability.unmask_pii=true)

Passed: 3   Failed: 1   Warn: 1   Skip: 1
Elapsed: 2.10s
```

Tres cosas para mirar:

1. **El que falló** te dice qué fixear.
2. **Los skipeados** te dicen qué quedó sin validar por culpa del fallo. Después de fixear, re-correr.
3. **Los warnings** no rebotan el run, pero leelos antes de pasar a producción.

---

## Auto-doctor: integración con las pipelines

Por default, todos los `<pipeline> run` corren `doctor` al arrancar. Si falla cualquier check, el pipeline aborta antes de tocar nada. Eso te protege de migraciones a medias.

Si querés saltearlo (por ejemplo en CI donde ya corrés `doctor` aparte):

```bash
cmcourier csv-trigger-pipeline run --config prod.yaml --skip-doctor
```

> Saltearlo es responsabilidad tuya. Si rompiste el config y `--skip-doctor`, vas a ver el error en runtime, no en startup.

---

## Patrones comunes

### Pre-flight previo a un dry-run

```bash
cmcourier doctor --config staging.yaml --check all --log-level DEBUG
```

`DEBUG` te da el detalle de cada check — útil para investigar por qué algo falla.

### Solo conectividad antes de un crontab

```bash
cmcourier doctor --config prod.yaml --check connections
```

Si solo querés saber "¿está vivo el AS400 hoy?" antes del run nocturno, esto es lo más rápido.

### Validación de mapping recién entregado

```bash
cmcourier doctor --config prod.yaml --check mapping
cmcourier doctor --config prod.yaml --check cm-types
cmcourier doctor --config prod.yaml --check cm-targets
```

Cuando el banco te pasa un mapping nuevo (`MapeoRVI_CM.csv` + `MetadatosCM.csv`), corré estos tres antes de aceptar el archivo.

### CI gate

Incluí `cmcourier doctor` como step de CI. Si el exit code no es 0, falla el job. Pasalo con `--log-level WARNING` para que el output sea conciso.

---

## Qué hacer cuando un check falla

| Check falla | Primer movimiento |
|-------------|-------------------|
| `log_dir_writable` | `ls -ld $(grep log_dir config.yaml)` — chequear perms |
| `cmis_connectivity` | `curl -u $CMIS_USERNAME:$CMIS_PASSWORD <base_url>` |
| `as400_connectivity` | `cmcourier as400-query --query "SELECT 1 FROM RVILIB.RVABREP FETCH FIRST 1 ROW ONLY"` |
| `tracking_openable` | `mkdir -p $(dirname db_path)`, chequear permisos |
| `as400_sync` | Verificar que NIARVILOG existe en la library y matchea `columns` |
| `mapping_completeness` | `cmcourier inspect mapping-stats` para detalle |
| `metadata_sources` | Validar cada path/conexión a mano |
| `sample_dry_run` | `cmcourier inspect trigger` sobre el trigger problemático |
| `cm_type_alignment` | Revisar definiciones de tipo en CMIS |
| `cmis_folders_exist` | Crear las carpetas faltantes en Content Manager |
| `cmis_properties_alignment` | Revisar property ids de CMIS, actualizar mapping |

Para fallos persistentes o no obvios, mirá los runbooks en `docs/how-to/cmis-target-preflight.md` y `docs/how-to/validation-checklist.md`.

---

## Siguientes pasos

- [06 — Tu primera corrida streaming](06-first-streaming-run.md): ahora que validaste, corré
- [07 — Debugging de un batch fallido](07-debugging-a-failed-batch.md): qué hacer si `doctor` pasa pero el run falla
- [`docs/how-to/cmis-target-preflight.md`](../how-to/cmis-target-preflight.md): receta de pre-flight CMIS
- [`docs/how-to/validation-checklist.md`](../how-to/validation-checklist.md): checklist de validación pre-migración
