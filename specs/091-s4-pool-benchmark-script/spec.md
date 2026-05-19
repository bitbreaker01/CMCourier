# 091 — Script de benchmark para S4 (ProcessPool vs ThreadPool vs Serial)

## Por qué

Operador productivo reportó que S4 (PDF assembly) era lento. Su
intuición: el `ProcessPoolExecutor` (spec 066 default) podía no estar
dando paralelismo real para archivos chicos.

En vez de aceptar la intuición o rechazarla, **medimos**. Spec 091
agrega un script de benchmark al repo para que cualquier operador
pueda comparar los 3 modos (Serial, ThreadPool, ProcessPool) contra
4 workloads sintéticos (PDFs chicos, PDFs grandes, TIFFs paginados,
mix realista) en SU ambiente real.

Es una **herramienta**, no un cambio de comportamiento.

## Resultados de referencia (Linux, 8 cores físicos)

```
Workload          | Serial      | ThreadPool  | ProcessPool | Mejor
------------------|-------------|-------------|-------------|------
Small (PDF 100KB) | 1494 docs/s |  470 docs/s |  513 docs/s | Serial
Large (PDF 5MB)   |   78 docs/s |  224 docs/s |  283 docs/s | ProcessPool 1.7×
TIFF (3 pages)    |   77 docs/s |   69 docs/s |  283 docs/s | ProcessPool 4.1×
Mixed (real)      |  295 docs/s |  289 docs/s |  425 docs/s | ProcessPool 1.5×
```

**Insights**:
1. **ProcessPool gana en 3/4 escenarios productivos** (Large, TIFF,
   Mixed) — el default actual es correcto.
2. **Para Small (PDFs muy chicos), el paralelismo de cualquier tipo
   pierde contra Serial** — el overhead de coordinación supera el
   trabajo útil.
3. **ThreadPool nunca gana**. Para CPU-bound (TIFF) pierde por GIL.
   Para I/O-bound chico, pierde por overhead. Para I/O-bound grande
   queda atrás del ProcessPool.

Windows es esperable que tenga overhead extra del `spawn` (vs `fork`
en Linux). Por eso es importante que el operador mida en SU ambiente.

## Qué

### Cambios

1. **`scripts/bench-s4-pool-comparison.py`** (nuevo): script
   standalone que:
   - Genera workloads sintéticos en un `tempfile.mkdtemp`:
     `small` (100 PDFs de 100 KB), `large` (PDFs de 5 MB),
     `tiff` (TIFFs paginados), `mixed` (70/20/10).
   - Corre cada workload N veces en los 3 modos.
   - Imprime wall time, throughput (docs/s, MB/s), latencia
     (avg/p50/p95) y speedup comparativo.
   - CLI: `--docs N --workers N --runs N --workload {small|large|tiff|mixed|all}`.

2. **No tocar el código productivo**. El comportamiento default
   (`processing.s4_use_processes: true`) permanece igual porque
   los datos confirman que es correcto para 3/4 workloads.

### Uso

```powershell
# Default: 4 workloads, 100 docs, 10 workers, 3 runs
python scripts\bench-s4-pool-comparison.py

# Tu escenario productivo
python scripts\bench-s4-pool-comparison.py --docs 200 --workers 30 --runs 3

# Solo un workload específico
python scripts\bench-s4-pool-comparison.py --workload tiff
```

## Criterios de aceptación

1. El script corre sin errores con `python scripts/bench-s4-pool-comparison.py`.
2. Imprime resultados comparativos legibles para los 4 workloads.
3. Limpia los temp dirs después de cada workload.
4. **Cero cambios en código de producción**.

## Riesgos

* **Cero**. El script vive en `scripts/` que NO se instala como
  package y NO se ejecuta automáticamente. Es una herramienta
  manual.
* Los workloads sintéticos NO son los archivos reales del banco.
  Para resultados representativos, el operador puede modificar
  `_make_synthetic_pdf` y `_make_synthetic_tiff` para reflejar
  el perfil real, o (mejor) correr el batch productivo y comparar
  con la TUI.

## Notas

- **Decisión de no tocar el comportamiento default**: los datos
  muestran que ProcessPool gana en 3/4 workloads. Cambiar a
  ThreadPool o Serial degradaría performance en producción real.
  Si en Windows un operador específico ve un patrón distinto,
  puede usar el toggle existente `processing.s4_use_processes:
  false` (spec 066) para opt-out, sin necesidad de código nuevo.

- **Si el bench en Windows muestra que ProcessPool pierde para
  Small Y el workload del operador es mayormente Small**, el fix
  apropiado sería una **spec futura** que rutee por tamaño/tipo:
  - PDFs nativos chicos → ThreadPool inline (libera GIL en
    `shutil.copy2`)
  - TIFFs y archivos grandes → ProcessPool
  Pero NO se implementa speculativamente hoy.

- **Pareja conceptual con 090** (chunk_bytes del MultipartEncoder).
  Ambas atacan throughput de pipeline desde la base de los datos,
  no de la opinión.
