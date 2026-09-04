# 121 — Se elimina `find_documents_batch` (código muerto en S1)

## Por qué

La auditoría (hallazgo 5.7) encontró que
`IndexingService.find_documents_batch` + `_process_chunk`
(`indexing.py:190-242`) — el lookup RVABREP batcheado con listas `IN`
de a 50 — es **código muerto**: ningún código de producción lo llama
(solo sus propios tests). S1 corre serial vía `enrich` → 1 query por
`ClientTrigger`.

### Por qué se BORRA en vez de cablearse

Evaluado con el código en la mano:

1. **Semántica incompatible.** `find_documents` distingue "sin filas"
   (`RVABREPNotFoundError`) de "todas borradas"
   (`RVABREPDeletedError`) — la contabilidad de `S1_FILTERED` (051/062)
   depende de esa distinción. `_process_chunk` la pierde (`_classify`
   descarta borradas en silencio y yieldea `[]` para ambos casos).
   Cablearlo exige reescribir el loop S1 más delicado del pipeline
   (idempotencia cross-batch, resume scope, filtered accounting).
2. **La ganancia es estrecha.** El path productivo
   (`rvabrep-pipeline`) usa `RvabrepRowTrigger` — **cero queries en
   S1**. El mirror CSV es O(1) por trigger post-112. El único
   beneficiario sería `csv-trigger-pipeline` contra RVABREP en AS400
   directo — un escenario secundario que no justifica el riesgo.
3. **Además consulta distinto**: `_process_chunk` filtra solo por
   shortname (sin `system_id`) y re-bucketea en cliente — más filas
   por el cable que el lookup exacto.

Código muerto con semántica divergente del contrato real es una trampa
para el próximo que lo "aproveche". Afuera.

## Qué

**REQ-001 —** Se eliminan `find_documents_batch`, `_process_chunk` y
sus tests.

**REQ-002 —** Se elimina el parámetro `batch_size` de
`IndexingService` y su cableado en `wiring.py` (solo alimentaba al
método muerto).

**REQ-003 — Compatibilidad de YAML.** El campo `indexing.batch_size`
del schema se CONSERVA (los modelos son `extra="forbid"` — quitarlo
rompería YAMLs existentes) pero queda documentado como sin efecto
desde 121.

## Escenarios

**E1 —** `IndexingService` no expone `find_documents_batch`.
**E2 —** Un YAML con `indexing.batch_size: 50` sigue validando.
**E3 —** La suite completa pasa sin los tests del método muerto.
