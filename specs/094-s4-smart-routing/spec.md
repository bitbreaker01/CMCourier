# 094 — Smart routing PDF/TIFF en S4

## Por qué

Datos reales del operador productivo (Windows, 200 docs, ~40 MB
promedio, mix 57% PDF nativos + 43% TIFF paginados):

```
S4 total:          37013.1 ms avg/doc — 7402.6 s total
  copy_native       11730.4 ms avg — 1337.3 s total (114 PDFs)
  encode_pdf         2964.3 ms avg —   254.9 s total (86 TIFFs)
  discover_pages       22.7 ms — 1.9 s
  source_stat           2.7 ms — 0.3 s
  dst_stat              0.2 ms — 0.0 s

Suma sub-stages:                       ~1594 s
GAP (no medido en assembler):          ~5808 s ← 78% del wall time de S4!
```

**El gap de 5808 s** es overhead del `ProcessPoolExecutor` con
`spawn` (Windows): pickle del `RVABREPDocument` (~5 ms), IPC roundtrip
al subprocess (~10-30 ms), spawn de procesos nuevos cuando es cold,
y la cola del pool cuando los workers están ocupados con TIFFs.

**Problema central**: los **PDF nativos** son `shutil.copy2` puro
(I/O bound) que **libera el GIL durante la lectura del disco**. NO
necesitan ProcessPool — un ThreadPool inline daría el mismo
paralelismo I/O sin el overhead de pickle/IPC/spawn. Pero hoy
**todos los docs van al ProcessPool cuando está activo**, sin
distinguir.

Los TIFFs/JPEGs SÍ necesitan ProcessPool porque `img2pdf.convert`
es CPU bound y el GIL del thread los serializaría.

## Qué

### Nuevo flag opt-in

```yaml
processing:
  s4_use_processes: true       # default — process pool activo
  s4_smart_routing: true       # 094 — opt-in al routing por tipo
```

### Lógica de routing

```python
route_inline = self._s4_process_pool is None or (
    self._s4_smart_routing and item.document.is_pdf
)
if route_inline:
    staged, timings = self._assembler.assemble_traced(item.document)
else:
    staged, timings = self._s4_process_pool.submit(
        _pool_assemble_traced, item.document
    ).result()
```

Matriz de comportamiento:

| `s4_use_processes` | `s4_smart_routing` | PDF nativo | TIFF/JPEG |
|---|---|---|---|
| `false` | irrelevante | inline | inline |
| `true` | `false` (default) | pool | pool |
| `true` | `true` (094) | **inline** | pool |

### Cambios

1. **`ProcessingConfig.s4_smart_routing: bool = False`** (schema):
   nuevo flag opt-in. Default `False` preserva el comportamiento
   pre-094.

2. **`StagedPipeline.__init__`**: nuevo parámetro keyword-only
   `s4_smart_routing: bool = False`. Almacenado como
   `_s4_smart_routing`.

3. **`StagedPipeline._stage_s4_one`**: la rama de despacho usa
   `document.is_pdf` para decidir. Backward-compat absoluto cuando
   el flag es `False`.

4. **`wiring.py`**: pasa `config.processing.s4_smart_routing` al
   constructor del orquestador.

### Tests

* `tests/unit/orchestrators/test_s4_smart_routing.py`:
  - Smart routing ON: PDF nativo → inline, paginado → pool
  - Smart routing OFF (default): TODO al pool (pre-094)
  - Sin process pool: TODO inline
  - Schema: default y opt-in

## Criterios de aceptación

1. Sin override en YAML, `s4_smart_routing=False` y todos los docs
   van al pool si está activo — byte-idéntico a pre-094.
2. Con `s4_smart_routing: true`, `is_pdf == True` docs evitan el
   pool y van inline.
3. `is_pdf == False` (TIFF/JPEG) siempre va al pool si está activo.
4. `pytest -m unit` pasa.

## Ganancia esperada

Para el workload del operador (114 PDFs nativos en Windows):

```
Overhead estimado del pool por doc:  ~29 s (spawn + pickle + IPC + queue)
114 docs × 29 s eliminados:          ~3300 s ahorrados

Wall time pre-094:                   350 s (medido)
Wall time post-094 (predicción):     ~50-80 s (sin tocar disco/AV)
```

**Speedup esperable: 4-7×** solo por el routing.

Combinado con la exclusión de Windows Defender del source_root +
temp_dir (acción operativa fuera de código), el speedup compuesto
puede llegar a **10-15×**.

## Riesgos

* **Backward-compat total**: el flag default `False` mantiene el
  comportamiento pre-094 byte-idéntico. Configs existentes siguen
  igual hasta que el operador opte in.
* **PDFs nativos enormes** (> 500 MB): el `shutil.copy2` inline
  toma el GIL durante el `read/write` chunked. Con muchos PDFs
  gigantes paralelos, el thread del prep_workers puede serializar.
  Mitigación: el operador puede dejar `s4_smart_routing: false`
  para esos workloads atípicos.
* **Cambio en el balance del prep_workers**: con routing on, los
  threads del prep_workers asumen MÁS trabajo (el de los PDFs
  nativos). El operador puede necesitar aumentar `prep_workers`
  si tenía configurado un valor bajo asumiendo que S4 estaba
  delegado al process pool.

## Notas

- Pareja conceptual con specs 091 (benchmark que mostró el patrón)
  y 093 (sub-stage metrics que confirmaron el GAP del pool).
- El benchmark sintético en Linux mostraba ProcessPool ganando para
  Small files, pero en producción Windows + SMB + AV el overhead
  del spawn invierte la conclusión para PDFs nativos. **Los datos
  empíricos del operador productivo manda — no los sintéticos.**
- Future spec posible: heuristica más fina basada en tamaño
  (PDFs nativos > 1 GB podrían beneficiarse del pool de nuevo para
  evitar bloquear el thread del prep_worker). NO implementado hoy
  porque la simplicidad de `is_pdf` cubre el 95% de los casos.
