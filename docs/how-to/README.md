# How-to — Recetas para Tareas Específicas

> [← Volver al índice](../INDEX.md)

Documentación orientada a problemas. **"¿Cómo hago para…"**

Una guía how-to asume que ya sabés qué es CMCourier y querés lograr un objetivo específico. Es una secuencia de pasos prácticos — sin narrativa, sin teoría. Si querés *entender* cómo funciona algo, andá a [`../explanation/`](../explanation/README.md). Si sos nuevo en el proyecto, empezá por [`../ONBOARDING.md`](../ONBOARDING.md).

---

## Para el operador

Tareas comunes que ejecutás cuando corrés migraciones.

| Receta | Cuándo aplica |
|--------|---------------|
| [`operator/run-a-migration-from-csv.md`](operator/run-a-migration-from-csv.md) | Corrida estándar con CSV-trigger |
| [`operator/run-a-streaming-load-against-staging.md`](operator/run-a-streaming-load-against-staging.md) | Volumen alto sin reventar memoria |
| [`operator/recover-from-a-corrupted-tracking-db.md`](operator/recover-from-a-corrupted-tracking-db.md) | SQLite tracking dañado |
| [`operator/read-the-batch-census.md`](operator/read-the-batch-census.md) | Leer el censo de un batch balde por balde y saber qué hacer con cada razón (148) |
| [`operator/retry-only-failed-records.md`](operator/retry-only-failed-records.md) | Re-correr solo lo que falló |
| [`operator/tune-aimd-for-a-slow-link.md`](operator/tune-aimd-for-a-slow-link.md) | Ajustar AIMD para tu link |
| [`operator/configure-heavy-light-lanes.md`](operator/configure-heavy-light-lanes.md) | Activar lanes para corpus mixto |
| [`operator/wipe-staging-state.md`](operator/wipe-staging-state.md) | Limpiar staging entre corridas |
| [`operator/interpret-the-tui-tabs.md`](operator/interpret-the-tui-tabs.md) | Atlas visual de las 5 tabs |
| [`build-offline-installer.md`](build-offline-installer.md) | Empaquetar CMCourier para un servidor air-gapped |

Índice dedicado: [`operator/README.md`](operator/README.md).

---

## Para el desarrollador

Cómo extender el sistema sin romper la arquitectura.

| Receta | Cuándo aplica |
|--------|---------------|
| [`developer/add-a-new-config-field.md`](developer/add-a-new-config-field.md) | Extender el schema Pydantic |
| [`developer/add-a-new-cmis-property.md`](developer/add-a-new-cmis-property.md) | Agregar metadata target a CMIS |
| [`developer/add-a-new-source-system.md`](developer/add-a-new-source-system.md) | Implementar otro `IDataSource` |
| [`developer/run-the-test-suite.md`](developer/run-the-test-suite.md) | Todas las maneras de correr tests |
| [`developer/profile-a-bottleneck.md`](developer/profile-a-bottleneck.md) | Diagnosticar dónde se va el tiempo |

Índice dedicado: [`developer/README.md`](developer/README.md).

---

## Por feature específica

Guías más densas, cada una atada a una feature concreta del sistema.

| Receta | Feature / Spec |
|--------|----------------|
| [`heavy-light-lanes.md`](heavy-light-lanes.md) | Lanes adaptativos heavy/light (036, POST-MVP §1) |
| [`multi-batch.md`](multi-batch.md) | Multi-batch con `batches_in_flight` (028) |
| [`as400-sync.md`](as400-sync.md) | Idempotencia distribuida AS400 NIARVILOG (034, POST-MVP §4) |
| [`testing-as400-sync.md`](testing-as400-sync.md) | Probar el sync AS400 en staging — drills de conflicto, `sync resolve` (034) |
| [`document-cache.md`](document-cache.md) | Cache cross-batch de metadatos (037, POST-MVP §9) |
| [`log-analysis.md`](log-analysis.md) | Análisis offline con `cmcourier analyze` (027) |
| [`mock-rvabrep-generator.md`](mock-rvabrep-generator.md) | Generar CSV RVABREP sintético (039) |
| [`cm-type-manifest.md`](cm-type-manifest.md) | Manifest de tipos CM: `types discover`/`review`/`check`/`update`, reemplazo de `MetadatosCM.csv` (145) |
| [`metadata-format.md`](metadata-format.md) | Normalizar el valor de un metadato con `format` — ceros a la izquierda, `CHAR(n)` de AS400 (146) |
| [`identity-chain.md`](identity-chain.md) | Resolver el shortname / CIF del cliente con una cadena de saltos: hijo → padre → shortname → CIF → nombre (147) |
| [`cmis-target-preflight.md`](cmis-target-preflight.md) | Pre-flight CMIS folders + properties (038, modo split) |
| [`staging-dry-run.md`](staging-dry-run.md) | Dry-run con datos reales |
| [`local-staging-simulation.md`](local-staging-simulation.md) | Alfresco + Docker para simular CMIS |
| [`probar-la-consola.md`](probar-la-consola.md) | Probar la consola interactiva `cmcourier console` en local (123-125) |
| [`validation-checklist.md`](validation-checklist.md) | Checklist E2E cross-platform |

---

## Escribir una nueva how-to

1. Elegí un slug preciso basado en verbo (`run-X`, `recover-from-Y`, `configure-Z`).
2. Decidí si va en `operator/`, `developer/`, o suelta acá (si está atada a una feature específica).
3. Empezá con breadcrumb estándar:
   ```
   > [← Volver al índice](../INDEX.md) · [How-to](README.md)
   ```
   (Ajustá el path relativo a la profundidad real.)
4. Estructurá: intro corta → **Cuándo aplica** → **Pre-requisitos** → **Pasos** numerados con comandos → **Verificación** → `## Ver también`.
5. Listala en este README, en el subdirectorio README (si aplica), y en `docs/INDEX.md`.

Una how-to **no** es un tutorial. Los tutoriales enseñan; las how-tos resuelven un problema que el lector ya tiene. Si necesitás enseñar, andá a [`../tutorials/`](../tutorials/README.md).

---

## Ver también

- [`../tutorials/`](../tutorials/README.md) — para aprender, no para resolver
- [`../explanation/`](../explanation/README.md) — para entender el por qué
- [`../reference/`](../reference/README.md) — para lookup rápido de flags, fields, exceptions
- [`../runbooks/`](../runbooks/README.md) — para incidentes en producción
- [`../INDEX.md`](../INDEX.md) — mapa canónico
