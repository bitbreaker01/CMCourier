# 093 — Métricas sub-stage en S4 (granularidad para diagnose)

## Por qué

Operador productivo con prep lento (350s / 200 docs pesados =
1.75s/doc). El `cmcourier diagnose` de spec 092 puede decir "S4 es
el cuello", pero **NO puede decir dónde dentro de S4 se va el
tiempo**. S4 internamente hace 4 cosas distintas:

1. **`source_stat`**: `Path.is_file()` del archivo fuente — si el
   source es un SMB share lento o tiene AV, esto es alto.
2. **`copy_native`** (PDF nativo) / **`encode_pdf`** (TIFF/JPEG): el
   trabajo real. Para PDF nativo es `shutil.copy2` (I/O); para
   paginado es `img2pdf.convert` (CPU).
3. **`discover_pages`** (solo paginado): `glob + sort` de las
   páginas. Lento si el dir tiene millones de archivos.
4. **`dst_stat`**: `Path.stat()` del staged para el `size_bytes`.

**Sin instrumentación sub-stage, el operador adivina dónde tocar.**

## Qué

### Cambios

1. **Nuevo dataclass `AssemblyTimings`** en `pdf_assembler.py`:
   campos opcionales por sub-stage (`source_stat_ms`,
   `copy_native_ms`, `discover_pages_ms`, `encode_pdf_ms`,
   `dst_stat_ms`) + `path_kind` que identifica el camino tomado
   (`native_pdf`, `paged_img2pdf`, `paged_fallback`).

2. **Nuevo método `PdfAssembler.assemble_traced(doc)`**:
   - Retorna `tuple[StagedFile, AssemblyTimings]`.
   - Mide cada sub-paso con `time.perf_counter()`.
   - El método viejo `assemble(doc)` es ahora un wrapper que
     llama a `assemble_traced` y descarta los timings — **zero
     breaking change para callers existentes**.

3. **Nuevo `_pool_assemble_traced` en `pool.py`**:
   - Análogo del `_pool_assemble` pero retorna la tupla.
   - Devuelve los timings vía pickle al proceso padre (single
     return value, sin logging cross-process).
   - El viejo `_pool_assemble` se conserva (no se usa, pero
     mantengo backward-compat de la API pública).

4. **`StagedPipeline._stage_s4_one` usa el path traced**:
   - Llama a `_pool_assemble_traced` (subprocess) o
     `assembler.assemble_traced` (inline).
   - Después llama al nuevo helper `_record_s4_substages(rec,
     timings)` que registra cada timing > 0 como
     `S4.<sub_name>` en el `MetricsRecorder`.

5. **`_record_s4_substages`** (helper estático nuevo):
   - Cada sub-stage > 0 se registra como bucket separado.
   - Campos en 0 (camino no tomado) se skipean.
   - Aparecen automáticamente en `batch_summary` y por ende en
     `cmcourier diagnose`.

### Output esperado de `diagnose` post-093

```
Latencia por stage:

  Stage              Count  Avg ms  P50 ms  P95 ms   Sum s     %
  ─────────────────────────────────────────────────────────────────
  S1                   200     5.0     4.0    12.0     1.0   0.3%
  S2                   200     1.0     1.0     3.0     0.2   0.1%
  S3                   200    80.0    45.0   280.0    16.0   3.4%
  S4                   200  1650.0  1400.0  3800.0   330.0  70.2% ←
  S4.source_stat       200     8.0     5.0    25.0     1.6   0.3%
  S4.copy_native       200  1620.0  1380.0  3750.0   324.0  68.9%  ← donde se va el tiempo
  S4.dst_stat          200     2.0     1.0     5.0     0.4   0.1%
  S5                   200   610.0   450.0  1820.0   122.0  26.0%
```

**Lectura**: S4 es el cuello, **y dentro de S4 el 68.9% del wall
TOTAL se va en `copy_native`**. Eso confirma cuello de disco
(lectura del source). Sospechas concretas: SMB share lento, AV
scan, HDD. Fix: investigar source disk.

### Tests

* `tests/unit/adapters/assembly/test_pdf_assembler_traced.py`:
  - `assemble_traced` retorna tupla.
  - Camino `native_pdf` popula `source_stat_ms`, `copy_native_ms`,
    `dst_stat_ms`; deja los del path paged en 0.
  - El wrapper `assemble` sigue retornando solo `StagedFile`.

* `tests/unit/orchestrators/test_s4_substage_metrics.py`:
  - `_record_s4_substages` registra los buckets correctos para
    cada path_kind.
  - Campos en 0 se skipean.
  - Los valores propagan correctamente al `_build_summary`.

## Criterios de aceptación

1. `assemble(doc)` sigue retornando solo `StagedFile` — backward
   compat byte-idéntica.
2. `assemble_traced(doc)` retorna `(StagedFile, AssemblyTimings)`
   con timings > 0 para el path tomado.
3. En un batch real con docs nativos, el `batch_summary` incluye
   buckets `S4.source_stat`, `S4.copy_native`, `S4.dst_stat`.
4. En un batch real con docs paginados, los buckets son
   `S4.discover_pages`, `S4.encode_pdf`, `S4.dst_stat`.
5. `cmcourier diagnose --latest` muestra los sub-stages junto con
   S4 sin necesitar cambios al comando de spec 092.
6. `pytest -m unit` pasa.

## Riesgos

* **Overhead de instrumentación**: 5 calls a `time.perf_counter()`
  por doc. Costo despreciable (~0.001 ms cada uno) — orden de
  magnitud por debajo del trabajo medido.
* **Pickle overhead extra del subprocess**: el resultado pasa de
  `StagedFile` a `tuple[StagedFile, AssemblyTimings]`. Pickle
  agrega ~50 bytes — irrelevante.
* **`AssemblyTimings` con `path_kind` libre**: los tests asumen 3
  variantes (`native_pdf`, `paged_img2pdf`, `paged_fallback`). Si
  se agrega un nuevo camino futuro, el `_record_s4_substages`
  sigue funcionando porque itera sobre campos > 0, no por
  `path_kind`.

## Notas

- **Pareja con spec 092** (`cmcourier diagnose`). 092 emite la
  tabla por stage; 093 agrega el nivel de detalle DENTRO de S4
  sin modificar el comando — el `MetricsRecorder` ya soporta
  cualquier nombre de stage, así que los nuevos buckets aparecen
  automáticamente.
- **Para granularidad DENTRO de S5** (upload), una spec futura
  haría lo análogo: medir multipart-format, socket-send,
  server-roundtrip. Out of scope acá.
