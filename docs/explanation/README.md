# Explicaciones — Cómo Funciona CMCourier

> [← Volver al índice](../INDEX.md) · Explanation

Documentación orientada al **entendimiento**. Acá no vas a encontrar pasos para correr nada (eso es how-to) ni listas secas de campos (eso es reference). Lo que vas a encontrar es el **porqué**: por qué la arquitectura es la que es, por qué tal número y no otro, qué problema concreto resolvimos y qué tradeoffs aceptamos a cambio.

Cada explicación está pensada para alguien que ya leyó el README y entendió **qué** hace CMCourier, y ahora quiere entender **cómo** funciona por dentro. Si en algún momento te encontrás escribiendo o revisando código que toca alguno de estos temas, leelo primero — te va a ahorrar pelearte con decisiones que parecen arbitrarias y no lo son.

## Lista de explicaciones

| Tema | De qué se trata |
|------|-----------------|
| [`architecture-overview.md`](architecture-overview.md) | La arquitectura hexagonal — capas, dependency rule, por qué `domain/` no importa nada |
| [`pipeline-stages.md`](pipeline-stages.md) | La vida de un documento de S0 a S7 — qué hace cada stage, qué excepciones tira, qué deja en disco |
| [`streaming-vs-batched.md`](streaming-vs-batched.md) | Los dos modos de ejecución — cuándo elegir cada uno y por qué existen los dos |
| [`the-bucket-pattern.md`](the-bucket-pattern.md) | El bucket: producer-consumer con back-pressure natural vía `queue.Queue` acotada |
| [`aimd-auto-tuning.md`](aimd-auto-tuning.md) | AIMD para el pool de S5 — por qué `1.25×` y no `+1`, por qué `0.75×` y no `÷2` |
| [`heavy-light-lanes.md`](heavy-light-lanes.md) | El bug del worker chico atrás de un upload gigante y la solución de dos lanes |
| [`processpool-for-pdf-assembly.md`](processpool-for-pdf-assembly.md) | Por qué S4 corre en procesos y no en threads — el GIL y `spawn` vs `fork` |
| [`bandwidth-honesty.md`](bandwidth-honesty.md) | Por qué el sampler de bandwidth distribuye bytes uniformemente sobre la transmisión |
| [`http2-multiplexing.md`](http2-multiplexing.md) | `httpx[http2]` en vez de `requests`: ALPN, multiplexing real, qué cambia el conteo de conexiones |
| [`idempotency-and-retries.md`](idempotency-and-retries.md) | `rvabrep_txn_num` UNIQUE, la máquina de estados, `S1_SKIPPED`, política de retry por tipo de error |
| [`pii-handling.md`](pii-handling.md) | Cómo se redacta PII en logs — central masking helper, denylist, `--unmask-pii` y por qué grita |
| [`windows-vs-linux.md`](windows-vs-linux.md) | Portabilidad: qué funciona igual, qué pide `spawn`, qué pide WSL, dónde están los caveats |
| [`operations-console.md`](operations-console.md) | Por qué una TUI de operación: draft/applied, pausa cooperativa, techo manual vs AIMD, por qué la ETA exige `total` |

## Convención

Los archivos se llaman `<concept-slug>.md`, kebab-case, sustantivos. Renombrar es **breaking** para links externos — bumpear `CHANGELOG.md` si pasa.

Cada explicación abre con el **problema** que el concepto resuelve. Después construye desde primeros principios. Diagramas Mermaid, tablas y fragmentos de código son bienvenidos cuando agregan valor. Cierra cross-linkeando con specs, constitución o documentos hermanos.

Una explicación **no** es un tutorial, **no** es un how-to, **no** es una referencia. Es razonamiento. Si lo que escribís suena a "para hacer X seguí estos pasos", está en el cuadrante equivocado.

## Ver también

- [`docs/INDEX.md`](../INDEX.md) — mapa canónico de toda la documentación
- [`docs/how-to/README.md`](../how-to/README.md) — para recetas prácticas
- [`docs/reference/README.md`](../reference/README.md) — para datos puntuales
- [`.specify/memory/constitution.md`](../../.specify/memory/constitution.md) — la ley de ingeniería del proyecto
