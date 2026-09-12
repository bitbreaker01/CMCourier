# CMCourier — Índice de Documentación

> [← Volver al README](../README.md)

El único mapa de cada documento del proyecto. Encontrá el cuadrante que coincida con tu intención y entrá.

La estructura sigue el [framework Diátaxis](https://diataxis.fr): documentación dividida por **propósito** (aprender / resolver / consultar / entender) en lugar de por tema. La capa de Diátaxis está cruzada con dos cuadrantes adicionales: **ADRs** (decisiones con su contexto histórico) y **runbooks** (operación bajo presión).

---

## Si recién llegás

| Empezá por | Propósito |
|------------|-----------|
| [`README.md`](../README.md) | Overview, estado actual, quickstart |
| [`ONBOARDING.md`](ONBOARDING.md) | Mapa mental del proyecto en 30 min |
| [`tutorials/00-getting-started.md`](tutorials/00-getting-started.md) | Del clone al primer smoke run |

---

## Aprender — Tutoriales

Orientados al **aprendizaje progresivo**. Leélos en orden si es tu primer contacto con el proyecto.

| # | Documento | De qué trata |
|---|-----------|--------------|
| 00 | [`tutorials/00-getting-started.md`](tutorials/00-getting-started.md) | Setup local + smoke test |
| 01 | [`tutorials/01-the-yaml-config.md`](tutorials/01-the-yaml-config.md) | Tour completo del archivo de config |
| 02 | [`tutorials/02-pipelines-and-how-to-use-them.md`](tutorials/02-pipelines-and-how-to-use-them.md) | Las 4 pipelines (csv-trigger, rvabrep, local-scan, single-doc) |
| 03 | [`tutorials/03-execution-modes-batched-vs-streaming.md`](tutorials/03-execution-modes-batched-vs-streaming.md) | Modo `batched` vs `streaming` con tradeoffs |
| 04 | [`tutorials/04-all-commands-tour.md`](tutorials/04-all-commands-tour.md) | Recorrido por todos los comandos del CLI |
| 05 | [`tutorials/05-doctor-deep-dive.md`](tutorials/05-doctor-deep-dive.md) | El comando `doctor` y cómo funciona |
| 06 | [`tutorials/06-first-streaming-run.md`](tutorials/06-first-streaming-run.md) | Walkthrough completo de una corrida streaming con TUI |
| 07 | [`tutorials/07-debugging-a-failed-batch.md`](tutorials/07-debugging-a-failed-batch.md) | Diagnosticar y recuperar un batch con failures |

Índice de la sección: [`tutorials/README.md`](tutorials/README.md).

---

## Resolver — How-to

Orientados a **problemas específicos**. Recetas: pre-requisitos → pasos → verificación.

### Para el operador

| Receta | Cuándo aplica |
|--------|---------------|
| [`how-to/operator/run-a-migration-from-csv.md`](how-to/operator/run-a-migration-from-csv.md) | Corrida estándar con CSV-trigger |
| [`how-to/operator/run-a-streaming-load-against-staging.md`](how-to/operator/run-a-streaming-load-against-staging.md) | Volumen alto sin reventar memoria |
| [`how-to/operator/recover-from-a-corrupted-tracking-db.md`](how-to/operator/recover-from-a-corrupted-tracking-db.md) | SQLite tracking dañado |
| [`how-to/operator/read-the-batch-census.md`](how-to/operator/read-the-batch-census.md) | Leer el censo balde por balde (`EXCLUIDO` / `BLOQUEADO` / `FALLO`) y saber qué hacer con cada razón (148) |
| [`how-to/operator/preparar-el-banco-de-pruebas.md`](how-to/operator/preparar-el-banco-de-pruebas.md) | Armar en RVABREP de desarrollo una fila por escenario y verificar los 22 caminos del pipeline |
| [`how-to/operator/retry-only-failed-records.md`](how-to/operator/retry-only-failed-records.md) | Re-correr solo lo que falló |
| [`how-to/operator/tune-aimd-for-a-slow-link.md`](how-to/operator/tune-aimd-for-a-slow-link.md) | Ajustar `growth_factor`, `halve_factor`, `halve_threshold_ratio` |
| [`how-to/operator/configure-heavy-light-lanes.md`](how-to/operator/configure-heavy-light-lanes.md) | Activar lanes para corpus mixto |
| [`how-to/operator/wipe-staging-state.md`](how-to/operator/wipe-staging-state.md) | Limpiar staging entre corridas |
| [`how-to/operator/interpret-the-tui-tabs.md`](how-to/operator/interpret-the-tui-tabs.md) | Atlas visual de las 5 tabs de la TUI de corrida |
| [`how-to/probar-la-consola.md`](how-to/probar-la-consola.md) | La consola de operación paso a paso contra un Alfresco local (incluye SQL Server) |
| [`how-to/as400-sync.md`](how-to/as400-sync.md) | Habilitar y operar el sync NIARVILOG (`claim` vs `periodic`) |
| [`how-to/cm-type-manifest.md`](how-to/cm-type-manifest.md) | Descubrir/revisar/verificar el manifest de tipos CM, reemplazo de `MetadatosCM.csv` (145) |
| [`how-to/metadata-format.md`](how-to/metadata-format.md) | Normalizar el valor de un metadato con `format`: ceros a la izquierda y `CHAR(n)` de AS400 (146) |
| [`how-to/identity-chain.md`](how-to/identity-chain.md) | Resolver shortname / CIF con una cadena de saltos (hijo → padre → shortname → CIF → nombre), `identity:` y el ancho real de `CTENUM` (147) |
| [`how-to/client-eligibility.md`](how-to/client-eligibility.md) | Migrar sólo los clientes con producto activo: el bloque `eligibility:`, el preflight de la lista y `CLIENT_NOT_ACTIVE` en el censo (150) |
| [`how-to/build-offline-installer.md`](how-to/build-offline-installer.md) | Armar el bundle air-gapped para la estación del banco (101, 142) |

Índice: [`how-to/operator/README.md`](how-to/operator/README.md).

### Para el desarrollador

| Receta | Cuándo aplica |
|--------|---------------|
| [`how-to/developer/add-a-new-config-field.md`](how-to/developer/add-a-new-config-field.md) | Extender el schema Pydantic |
| [`how-to/developer/add-a-new-cmis-property.md`](how-to/developer/add-a-new-cmis-property.md) | Agregar metadata target a CMIS |
| [`how-to/developer/add-a-new-source-system.md`](how-to/developer/add-a-new-source-system.md) | Implementar otro `IDataSource` |
| [`how-to/developer/run-the-test-suite.md`](how-to/developer/run-the-test-suite.md) | Todas las maneras de correr tests |
| [`how-to/developer/profile-a-bottleneck.md`](how-to/developer/profile-a-bottleneck.md) | Diagnosticar dónde se va el tiempo |

Índice: [`how-to/developer/README.md`](how-to/developer/README.md).

---

## Consultar — Reference

Orientada a **lookup rápido**. Tablas, schemas, listas exhaustivas. No se lee top-to-bottom — se busca.

| Documento | Cubre |
|-----------|-------|
| [`reference/cli.md`](reference/cli.md) | Todos los comandos, flags, exit codes — incluida la consola (`console`): pestañas, teclas y lock |
| [`reference/config-schema.md`](reference/config-schema.md) | Cada modelo Pydantic, cada campo, default y rango |
| [`reference/tui-keybindings.md`](reference/tui-keybindings.md) | Atajos por tab del TUI |
| [`reference/observability-fields.md`](reference/observability-fields.md) | Estructuras de telemetría (network, system, lanes) |
| [`reference/tracking-db-schema.md`](reference/tracking-db-schema.md) | Tablas, índices, PRAGMAs, state machine |
| [`reference/error-codes.md`](reference/error-codes.md) | Todas las exceptions con stage y acción |
| [`reference/glossary.md`](reference/glossary.md) | AIMD, RVABREP, NIARVILOG, etc. |

Índice: [`reference/README.md`](reference/README.md).

---

## Entender — Explanation

Orientadas al **por qué**. Tradeoffs, contexto histórico, principios. Léelos cuando quieras profundidad arquitectónica.

| Documento | Concepto |
|-----------|----------|
| [`explanation/architecture-overview.md`](explanation/architecture-overview.md) | Hexagonal architecture: capas y reglas de dependencia |
| [`explanation/pipeline-stages.md`](explanation/pipeline-stages.md) | S0 → S7: vida de un documento |
| [`explanation/streaming-vs-batched.md`](explanation/streaming-vs-batched.md) | Los dos modos de ejecución |
| [`explanation/the-bucket-pattern.md`](explanation/the-bucket-pattern.md) | Producer-consumer con bounded queue |
| [`explanation/aimd-auto-tuning.md`](explanation/aimd-auto-tuning.md) | AIMD para el pool S5 (specs 025 + 068) |
| [`explanation/heavy-light-lanes.md`](explanation/heavy-light-lanes.md) | Dual semáforo + rebalance daemon |
| [`explanation/processpool-for-pdf-assembly.md`](explanation/processpool-for-pdf-assembly.md) | Bypassing the GIL en S4 (spec 066) |
| [`explanation/bandwidth-honesty.md`](explanation/bandwidth-honesty.md) | El sampler que distribuye bytes (spec 069) |
| [`explanation/http2-multiplexing.md`](explanation/http2-multiplexing.md) | httpx + ALPN + multiplexing en CMIS |
| [`explanation/idempotency-and-retries.md`](explanation/idempotency-and-retries.md) | `rvabrep_txn_num`, state machine, retry policy |
| [`explanation/pii-handling.md`](explanation/pii-handling.md) | Constitution Principle VIII en práctica |
| [`explanation/windows-vs-linux.md`](explanation/windows-vs-linux.md) | Portabilidad: qué funciona, qué no |
| [`explanation/operations-console.md`](explanation/operations-console.md) | La consola de operación (123–135): draft/applied, pausa cooperativa, techo manual vs AIMD, ETA |

Índice: [`explanation/README.md`](explanation/README.md).

---

## Decisiones — ADRs

Cada decisión arquitectónica con su contexto, alternativas y consecuencias. Útiles cuando preguntás **por qué hicimos X y no Y**.

| ADR | Tema | Specs |
|-----|------|-------|
| [`adr/001-hexagonal-architecture.md`](adr/001-hexagonal-architecture.md) | Hexagonal vs MVC/layered | 001, 002, 019 |
| [`adr/002-sqlite-tracking-store.md`](adr/002-sqlite-tracking-store.md) | SQLite local vs DB externa | 007 |
| [`adr/003-streaming-mode.md`](adr/003-streaming-mode.md) | Streaming orchestrator + bucket | 063, 064, 065 |
| [`adr/004-aimd-auto-tune.md`](adr/004-aimd-auto-tune.md) | AIMD multiplicativo para S5 | 025, 068 |
| [`adr/005-processpool-for-s4.md`](adr/005-processpool-for-s4.md) | ProcessPool spawn para PDF | 066 |
| [`adr/006-heavy-light-lanes.md`](adr/006-heavy-light-lanes.md) | Dual lanes con rebalance | 036, 065, 070 |
| [`adr/007-csv-trigger-primary-source.md`](adr/007-csv-trigger-primary-source.md) | CSV externo vs query AS400 | 011, 048 |
| [`adr/008-textual-tui.md`](adr/008-textual-tui.md) | TUI Textual vs logs a stdout | 025, 052, 064, 067 |

Índice: [`adr/README.md`](adr/README.md).

---

## Apagar fuegos — Runbooks

Para producción cuando algo se rompe. Síntoma → diagnóstico rápido → mitigación → resolución → post-mortem.

| Runbook | Severidad típica |
|---------|------------------|
| [`runbooks/cmis-down.md`](runbooks/cmis-down.md) | P1 |
| [`runbooks/as400-down.md`](runbooks/as400-down.md) | P1–P2 |
| [`runbooks/disk-full-during-prep.md`](runbooks/disk-full-during-prep.md) | P2 |
| [`runbooks/tracking-db-locked.md`](runbooks/tracking-db-locked.md) | P2 |
| [`runbooks/migration-stuck-no-progress.md`](runbooks/migration-stuck-no-progress.md) | P2 |

Índice: [`runbooks/README.md`](runbooks/README.md).

---

## Diagramas

Mermaid (render nativo en GitHub). Acompañan a las explanations.

| Diagrama | Qué muestra |
|----------|-------------|
| [`diagrams/hexagonal-layers.md`](diagrams/hexagonal-layers.md) | Capas + reglas de dependencia |
| [`diagrams/s0-s7-flow.md`](diagrams/s0-s7-flow.md) | Vida de un documento |
| [`diagrams/streaming-pipeline.md`](diagrams/streaming-pipeline.md) | Producer/bucket/consumer con lanes |
| [`diagrams/state-machine.md`](diagrams/state-machine.md) | Estados de `migration_log.status` |
| [`diagrams/sync-subsystem.md`](diagrams/sync-subsystem.md) | SQLite ↔ AS400 NIARVILOG: claim / periodic / recover |
| [`diagrams/console-flow.md`](diagrams/console-flow.md) | Máquina de estados de la consola y su cadena de guardas |
| [`diagrams/file-imports-map.md`](diagrams/file-imports-map.md) | Mapa de imports inter-módulo |

Índice: [`diagrams/README.md`](diagrams/README.md).

---

## Contribuir

Lectura obligatoria antes de tu primer PR.

| Documento | Contenido |
|-----------|-----------|
| [`contributing/code-style.md`](contributing/code-style.md) | Convenciones Python, naming, límites de tamaño, ruff/mypy |
| [`contributing/spec-driven-flow.md`](contributing/spec-driven-flow.md) | Workflow SDD (spec → plan → tasks → commits) |
| [`contributing/testing-philosophy.md`](contributing/testing-philosophy.md) | TDD estricto, qué se mockea, coverage gate |
| [`../CONTRIBUTING.md`](../CONTRIBUTING.md) | Workflow git, conventional commits, reglas de PR |

Índice: [`contributing/README.md`](contributing/README.md).

---

## Datos de referencia

Samples del proyecto legacy y del flujo CMIS.

| Documento | Propósito |
|-----------|-----------|
| [`../reference-data/csv/`](../reference-data/csv/) | CSVs de referencia: `MapeoRVI_CM.csv` (Modelo Documental), `MetadatosCM.csv` (definiciones por clase), `TriggerExample.csv`, metadatos por fuente |
| [`../reference-data/excel/RVILIB_RVABREP.xlsx`](../reference-data/excel/RVILIB_RVABREP.xlsx) | Volcado real de tabla RVABREP — forma de columnas y filas de ejemplo |
| [`../reference-data/cmis-responses/EjemploRespuestaCMIS.txt`](../reference-data/cmis-responses/EjemploRespuestaCMIS.txt) | Ejemplo real de respuesta CMIS Browser Binding |
| [`reference/config-reference.yaml`](reference/config-reference.yaml) | YAML de config anotado con todas las opciones |

---

## Planificación del proyecto

| Documento | Contenido |
|-----------|-----------|
| [`roadmap/POST-MVP.md`](roadmap/POST-MVP.md) | Features diferidas más allá del MVP: intención + diseño + criterios |
| [`../specs/`](../specs/) | Artefactos SDD por cambio (`spec.md`, `plan.md`, `tasks.md`) |

---

## Ley de ingeniería

| Documento | Propósito |
|-----------|-----------|
| [`../.specify/memory/constitution.md`](../.specify/memory/constitution.md) | Los 9 principios inmutables. Specs y código que los violen son rechazados |
| [`../CHANGELOG.md`](../CHANGELOG.md) | Historia versionada (Keep a Changelog) |

---

## Mantenimiento de este índice

Este archivo se actualiza con **cada cambio** que agregue, mueva o renombre un artefacto de documentación. El `tasks.md` del cambio incluye una tarea para actualizarlo. [`CONTRIBUTING.md`](../CONTRIBUTING.md) documenta esa responsabilidad.

Cuadrantes futuros (diferidos hasta que aparezca contenido natural):

- **`docs/tutorials/`** ya existe y crece naturalmente.
- **`docs/explanation/`** ya tiene 13 documentos — agregar uno nuevo cuando un concepto arquitectónico merezca walkthrough standalone (típicamente con la spec que lo introduce).
- **`docs/reference/`** crecerá cuando aparezcan más superficies estables (ej. una API HTTP, si alguna vez se shippea).
