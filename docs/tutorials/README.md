> [← Volver al índice](../INDEX.md)

# Tutoriales — Aprender CMCourier de Punta a Punta

> Documentación orientada al aprendizaje. **"Llevame de la mano por un ejemplo."**

Un tutorial asume que sos nuevo (o casi) en CMCourier y querés entender cómo funciona armando algo real. No es una receta — es un walkthrough guiado. Si ya sabés qué querés hacer, te conviene una [how-to](../how-to/README.md). Si querés entender por qué un componente funciona como funciona, leé [explanation](../explanation/README.md).

Todos los tutoriales suponen que tenés acceso al repo y que podés correr Python 3.11+ localmente. Algunos suben de nivel y necesitan Docker (para el Alfresco de staging) o acceso a un AS400 de pruebas.

---

## Orden recomendado

Si nunca tocaste el proyecto, leelos en orden. Cada uno se apoya en lo anterior.

| # | Tutorial | De qué se trata |
|---|----------|-----------------|
| 00 | [Getting Started](00-getting-started.md) | Clonar, instalar dependencias, correr el smoke test, ver el `--help` |
| 01 | [El YAML de configuración](01-the-yaml-config.md) | Tour completo del config — de mínimo a producción, sección por sección (incluye el registro `connections:` y las fuentes SQL Server) |
| 02 | [Pipelines y cuándo usarlas](02-pipelines-and-how-to-use-them.md) | Las cuatro pipelines (csv-trigger, rvabrep, local-scan, single-doc) y cómo elegir |
| 03 | [Batched vs streaming](03-execution-modes-batched-vs-streaming.md) | Los dos modos de ejecución, tradeoffs, cuándo elegir cada uno |
| 04 | [Tour de todos los comandos](04-all-commands-tour.md) | Recorrido del CLI completo (`console`, `doctor`, `batch`, `inspect`, `analyze`, etc.) |
| 05 | [`doctor` en profundidad](05-doctor-deep-dive.md) | Cada check, qué valida, cómo interpretar y qué hacer si falla |
| 06 | [Tu primera corrida streaming](06-first-streaming-run.md) | Walkthrough real con TUI: tabs, números, AIMD escalando workers |
| 07 | [Debugging de un batch fallido](07-debugging-a-failed-batch.md) | Provocar un fallo, leer logs, inspeccionar el tracking DB y recuperarse |

---

## Convenciones

- Los identificadores (`csv-trigger-pipeline`, `processing.mode`, `CmisUploader`) están en inglés con backticks.
- La prosa está en castellano rioplatense (voseo).
- Los comandos arrancan siempre desde la raíz del repo. Asumí que `cmcourier` está en el PATH (la instalación editable del 00 lo configura).
- Cada tutorial cierra con "Siguientes pasos" — links a docs relacionados para seguir bajando.

## Cross-references

- [`docs/INDEX.md`](../INDEX.md) — mapa canónico
- [`docs/how-to/README.md`](../how-to/README.md) — recetas concretas
- [`docs/how-to/probar-la-consola.md`](../how-to/probar-la-consola.md) — la consola de operación paso a paso, contra un Alfresco local
- [`docs/reference/config-reference.yaml`](../reference/config-reference.yaml) — config anotado con TODAS las opciones
- [`docs/_internal/dossier.md`](../_internal/dossier.md) — fuente de verdad para escritores de docs
