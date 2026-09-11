# How-to para Operadores

> [← Volver al índice](../../INDEX.md) · [How-to](../README.md)

Recetas cortas y accionables para el día a día del operador de CMCourier. Cada guía es **una tarea, un objetivo, copy-paste**. Si querés entender por qué algo funciona como funciona, las explicaciones viven en [`../../explanation/`](../../explanation/).

## Convenciones

- **Pre-requisitos** asumen siempre: Python 3.11+, CMCourier instalado (`uv pip install -e .`), `cmcourier doctor --config tu-config.yaml` pasando en verde.
- **Identificadores** (comandos, flags, paths, nombres de campos YAML) van en inglés. La narrativa, en castellano.
- Cuando un comando falle, primero corré `cmcourier doctor` — el 80% de los problemas vienen de credenciales o de conectividad.

## Recetas disponibles

| Receta | Cuándo te sirve |
|--------|-----------------|
| [`run-a-migration-from-csv.md`](run-a-migration-from-csv.md) | La corrida canónica — tenés un CSV de triggers y querés migrar |
| [`run-a-streaming-load-against-staging.md`](run-a-streaming-load-against-staging.md) | Volumen alto contra staging sin reventar memoria |
| [`recover-from-a-corrupted-tracking-db.md`](recover-from-a-corrupted-tracking-db.md) | Crash a media transacción dejó el SQLite tocado |
| [`read-the-batch-census.md`](read-the-batch-census.md) | Terminó una corrida y tenés que decir qué pasó con CADA documento del origen (148) |
| [`retry-only-failed-records.md`](retry-only-failed-records.md) | Una corrida terminó con FAILED y querés reintentar solo esos |
| [`tune-aimd-for-a-slow-link.md`](tune-aimd-for-a-slow-link.md) | El AIMD escala muy agresivo (timeouts) o muy tímido (subutiliza el link) |
| [`configure-heavy-light-lanes.md`](configure-heavy-light-lanes.md) | Tu corpus tiene mezcla de docs chicos y grandes, querés latencia predecible |
| [`wipe-staging-state.md`](wipe-staging-state.md) | Reset entre smoke-runs deterministas (Alfresco + tracking DB) |
| [`interpret-the-tui-tabs.md`](interpret-the-tui-tabs.md) | Atlas visual de las 5 tabs del TUI — qué número significa qué |

## Ver también

- [`../../INDEX.md`](../../INDEX.md) — mapa canónico de toda la documentación
- [`../developer/`](../developer/) — recetas orientadas a desarrollo
- [`../../explanation/`](../../explanation/) — explicaciones de cómo funciona internamente
- [`../../../CONTRIBUTING.md`](../../../CONTRIBUTING.md) — workflow y convenciones del proyecto
