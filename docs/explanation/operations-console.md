# La consola de operación: por qué una TUI y no más flags

> [← Volver al índice](../INDEX.md) · [Explanation](README.md)

## El problema que estamos resolviendo

Antes de la 123, migrar un lote contra producción era esto:

```bash
export CMIS_USERNAME=... CMIS_PASSWORD=...
cmcourier doctor --config prod.yaml --check all
vim prod.yaml                                    # bajar workers, era martes a la tarde
cmcourier doctor --config prod.yaml --check all  # otra vez, porque cambió el YAML
cmcourier csv-trigger-pipeline run --config prod.yaml --total 5000
# …y en otra terminal, mientras tanto:
cmcourier batch list --config prod.yaml
cmcourier sync status --config prod.yaml
```

Funciona. Y sin embargo cada línea de esa secuencia esconde una forma de arruinar una tarde:

- **El `vim prod.yaml`.** Para bajar `cmis.workers` de 16 a 8 durante una ventana de red mala, el operador edita el archivo de configuración **productivo**. Después se olvida de volverlo atrás. El próximo que corre hereda un `workers: 8` que nadie decidió.
- **El doctor que se corrió antes de editar.** No hay nada que relacione el veredicto del doctor con la config sobre la que se lanzó. Un doctor aprobado hace veinte minutos y tres ediciones atrás es indistinguible de uno recién corrido.
- **El olvido de que era producción.** `prod.yaml` y `staging.yaml` se ven igual en la línea de comandos, y la terminal no cambia de color.
- **La otra estación.** Nada avisa que un colega está corriendo el mismo lote desde su máquina. Dos corridas concurrentes contra el mismo Content Manager duplican documentos.
- **El 401 a mitad de camino.** Si la sesión CMIS expira en la hora tres de una corrida de cinco, los documentos que la agarran se marcan fallidos uno por uno hasta que alguien mira la pantalla.

Ninguno de estos es un problema de código. Son problemas de **operación**: el sistema está bien, pero el camino que el humano recorre para usarlo no tiene barandas. Y la respuesta obvia — agregar más flags — no funciona, porque una flag no puede recordarte que el doctor quedó desactualizado.

La consola (`cmcourier console`) es la respuesta: una TUI que mantiene el **estado de la sesión de operación** y usa ese estado para poner guardas donde la CLI no puede.

## Lo que la consola agrega y una flag no puede

Una flag es sin memoria. La consola tiene una sesión, y en esa sesión sabe:

| Estado de sesión | Para qué lo usa |
|------------------|-----------------|
| Credenciales probadas, por alias, con timestamp | Bloquea el lanzamiento si falta alguna o si caducó (45 min) |
| Veredicto del doctor + si quedó desactualizado | Advierte antes de lanzar sobre una config que el doctor no vio |
| Overrides de sesión, en dos niveles | Permite ajustar sin editar el YAML productivo |
| Corrida activa y su pausa | Habilita pausar, reanudar y re-autenticar en caliente |
| Entorno declarado (`environment: prd`) | Badge permanente + confirmación tipeada |

Nada de eso es información nueva: toda estaba disponible antes, desparramada entre el YAML, el entorno y la memoria del operador. Lo que cambia es que ahora hay **un solo lugar** que la tiene junta y puede razonar sobre ella.

## Draft y applied: dos niveles, no uno

El modelo de overrides de `[3] CONFIG` es lo menos obvio de la consola y lo más importante.

Lo que el operador tipea es un **borrador**. Cuando aprieta `a`, el borrador se **valida** y se promueve a **aplicado**. Sólo lo aplicado cuenta: es lo que el doctor va a validar y lo que la corrida va a usar.

```
tipeás  ──►  BORRADOR  ──[a: valida]──►  APLICADO  ──[w: parchea]──►  YAML
                 │                           │
                 │                           └─► lo que corre la corrida
                 └─► no afecta nada todavía
```

¿Por qué no aplicar directo mientras se tipea? Porque un campo a medio escribir es un estado inválido y no querés que la sesión pase por ahí. Un `workers` en el que ya tipeaste `1` camino a `12` es un `1`, y si eso se aplicara solo, el doctor se marcaría desactualizado con cada tecla. Separar el borrador hace que la promoción sea **un acto deliberado con una validación adentro**: `prep_workers` fuera de 1–32, `bucket_size` fuera de 10–1000 o `workers` fuera de 1–64 se rechazan ahí, con un mensaje, y no a mitad de una corrida contra producción.

El corolario es la advertencia de `[5]`: si tenés borrador sin aplicar, el launcher te lo dice y corre con lo aplicado. Es tentador tratarlo como una molestia; es exactamente lo contrario — es el sistema negándose a adivinar qué quisiste decir.

### Y el tercer nivel: `w`

`w` (135) escribe lo aplicado al YAML. Es opcional a propósito: la mayoría de los overrides son de una corrida (bajé workers porque hoy la red está mal) y no merecen ensuciar el archivo. Cuando sí merecen — descubriste que `prep_workers: 8` es el número correcto para esta instalación — `w` te ahorra el `vim` y, sobre todo, te ahorra el error de tipeo en el `vim`.

Por eso `w` es tan conservador: parchea el **texto** línea a línea en vez de re-serializar con PyYAML (que perdería todos los comentarios), toca sólo siete escalares, y antes de reemplazar el original recarga el resultado y lo compara contra lo que debería quedar. Si el YAML usa formas que el parche no sabe tocar — flow style, anchors, claves duplicadas — se niega y te manda a editarlo a mano, con el archivo intacto y sin backup a medias. Un editor de YAML completo necesitaría `ruamel.yaml` y un modelo de quoting propio; para siete escalares, no vale la deuda.

## Por qué el trigger es del launcher y no del YAML

La consola te deja elegir el pipeline en `[5] CORRER` (127) — `csv`, `rvabrep`, `local_scan`, `single_doc` — como un override de sesión. Y a diferencia de los otros siete, ese override **nunca se persiste con `w`**.

La distinción no es caprichosa. Los siete escalares son **propiedades de la instalación**: cuántos workers aguanta este servidor CMIS, cuánto ancho de banda tolera esta red, si esta máquina tiene cores para `prep_workers: 8`. Son estables, y tiene sentido que vivan en el archivo.

El pipeline es una **propiedad de la corrida**. El mismo banco, con el mismo YAML, corre `csv` cuando le pasan una lista de documentos, `rvabrep` cuando quiere barrer un rango completo y `single_doc` cuando está diagnosticando un caso puntual. Escribir `trigger.kind: local_scan` en el YAML porque hoy hiciste un scan local es sembrar una sorpresa para el próximo que lo abra.

Hay además una razón mecánica: el `trigger` del YAML es un modelo Pydantic ya validado — el CSV existe, la carpeta existe. El override del launcher construye ese mismo modelo, con la misma validación, en el momento de lanzar. Es un valor por corrida que atraviesa el mismo camino que el del archivo; simplemente no sobrevive a la sesión.

## Pausa cooperativa y re-auth: no quemar documentos

Un 401 de CMIS a mitad de corrida es, en el fondo, un problema de credenciales, no de documentos. Pero el pipeline sin consola no puede distinguirlos: para él, un 401 es un fallo de upload como cualquier otro, y después de agotar los reintentos marca el documento `S5_FAILED`. Una sesión que expiró en la hora tres de una corrida de cinco puede quemar cientos de documentos perfectamente buenos, que después hay que reintentar a mano desde `[7]`.

La consola corta eso (132). Cuando un worker recibe 401, el pipeline avisa a la consola, que **pausa la corrida entera** y lleva al operador a `[2]`. La pausa es **cooperativa**, no un `kill`: los uploads en vuelo terminan normalmente y ningún worker toma trabajo nuevo. Nadie pierde estado, nada queda a medias.

El operador carga la credencial nueva, la prueba, vuelve a `[6]` y aprieta `r`. Antes de abrir la compuerta, la consola **empuja la credencial al uploader** — la sesión arranca fría y el próximo POST rehace el warmup. El documento que comió el 401 se reintenta él mismo. No se perdió nada.

Dos detalles que valen:

- Si el operador reanuda sin haber probado la credencial nueva, la consola pide confirmación. Es un modal, no un bloqueo: puede haber razones para reanudar a ciegas, pero no por distracción.
- El límite de `--max-duration` **sigue corriendo mientras la corrida está pausada**. Es wall-clock, no tiempo de trabajo. Si pausaste veinte minutos para conseguir una credencial, son veinte minutos menos de ventana.

La misma pausa cooperativa es la que usa `x` (cancelar con drain) y la que dispara `--max-duration`. Un solo mecanismo — el `CancellationToken` con su compuerta — sirve a los tres, y por eso los tres dejan el batch reanudable.

## Techo manual vs AIMD: quién gana y por qué

Con `auto_tune.enabled: true`, el AIMD ajusta el pool de S5 solo, leyendo el p95 de latencia. Funciona, y funciona bien — ver [`aimd-auto-tuning.md`](aimd-auto-tuning.md) para el detalle de por qué crece `1.25×` y encoge `0.75×`.

Pero el AIMD sólo observa **una** señal: la latencia que le devuelve el servidor CMIS. Hay cosas que no ve y que el operador sí:

- El equipo de infraestructura pidió bajar la presión sobre el CMIS durante los próximos veinte minutos.
- La red del banco tiene otro proceso importante corriendo y esta migración no es la prioridad.
- El operador está mirando un gráfico de Grafana que CMCourier no conoce.

Para esos casos, `+` y `-` en `[6]` (133) mueven un **techo manual** en caliente, sin confirmación — es reversible, y pedir confirmación por algo reversible entrena al operador a apretar Enter sin leer.

La regla es simple: **el techo gana**. El pool efectivo es `min(lo que pide el AIMD, el techo manual)`. Si el AIMD quiere 8 y el techo está en 3, corren 3.

Lo interesante es qué pasa con el AIMD debajo. **Sigue corriendo, y sigue viendo el valor cappeado.** Podría parecer un error — ¿no habría que "congelarlo" mientras hay techo manual? — pero es deliberado, y es lo correcto: el AIMD mide la latencia que produce el pool que *realmente está corriendo*. Si con 3 workers el p95 está cómodo, el AIMD registra que hay margen; cuando el operador sube el techo a 10, el controlador **retoma desde el estado real** en vez de reconstruir su estimación desde cero. Un AIMD congelado despertaría con una idea de la capacidad basada en un mundo que ya no existe.

Dos límites que conviene saber:

- **Sin AIMD, `+` no sube nada.** El pool de threads de S5 se dimensiona en `cmis.workers` y no crece más allá; el techo manual sólo puede bajar. Para subir hay que reiniciar la corrida con otro `workers`, o encender el auto-tune.
- **El piso es 1** (2 en modo dual-lane, un slot por carril: un lane sin slots es un lane que nunca drena).

Y el techo es **de la corrida**. La próxima arranca sin él, porque la razón por la que lo pusiste era circunstancial por definición.

## Tasa por ventana vs acumulada, y por qué la ETA exige `total`

La cabecera de `[6]` muestra dos tasas (134), y la diferencia entre ellas es la diferencia entre historia y presente.

La **acumulada** es `documentos / elapsed` desde el arranque. Es honesta y es inútil para operar: en la hora cuatro de una corrida, mil documentos nuevos mueven el promedio un pelo. Si tocás el techo de workers y la tasa real se duplica, la acumulada te va a mostrar una subida lánguida durante media hora.

La de **ventana de 60 segundos** es la que responde. Es la que te dice si lo que acabás de hacer — subir workers, reanudar tras una pausa, dejar que el AIMD escale — sirvió. Mientras no haya dos muestras separadas por un segundo muestra `—`, y durante una pausa decae a cero, que es exactamente lo que está pasando.

Ninguna de las dos reemplaza a la otra: la acumulada es la que usás para estimar el lote completo, la de ventana es la que usás para decidir en los próximos treinta segundos.

### La ETA

La ETA de corrida (`ETA 0:12:30 de 5000`) aparece **sólo si pusiste `total`** en `[5]`. Sin total, la cabecera dice `ETA — (sin total)`.

No es una limitación que se pueda arreglar con más ingeniería: es una consecuencia de cómo funciona S0. Los triggers se traen **por olas**, en chunks de `batch_size`, precisamente para que una migración de 20 millones de filas no tenga que caber en memoria (ver el contrato de memoria de `batch_size`). Eso significa que a mitad de corrida el pipeline **genuinamente no sabe** cuántos documentos faltan: sabe cuántos procesó y que la fuente todavía devuelve filas.

Se podría contar el total al arrancar — un `SELECT COUNT(*)` sobre la RVABREP productiva. Sería un escaneo de veinte millones de filas antes de subir el primer documento, para calcular un número que el operador puede aportar gratis con `--total`. La decisión fue no mentir: si no hay total, no hay ETA, y se dice por qué.

Cuando el operador **sí** pone `--total` — que es el caso normal en una ventana de mantenimiento, donde el lote está acotado a propósito — la ETA sale de la tasa observada y del faltante conocido. Y como la corrida se puede pausar, la ETA también pasa a `—` durante la pausa: extrapolar una tasa de cero no da un número, da un infinito.

## Lo que la consola NO es

- **No es un editor de YAML.** `w` toca siete escalares. Lo estructural — fuentes, conexiones, mapping, tracking — sigue siendo del archivo, y con razón: son decisiones que merecen un diff y una revisión.
- **No es un scheduler.** Para corridas desatendidas está `background`, con su lock y su exit code 75.
- **No reemplaza a la CLI.** Todo lo que la consola hace, lo hacen los comandos. La pestaña `[8]` llama al mismo `cli/sync_ops.py` que `cmcourier sync`; `[4]` llama al mismo `run_doctor`. La consola es una capa de operación sobre la misma maquinaria, no una segunda implementación — si divergieran, tendríamos dos verdades.
- **No coordina entre máquinas.** El lock de config es por estación (`fcntl.flock` sobre un archivo derivado del path). Dos consolas en la misma máquina colisionan; dos máquinas distintas, no. Para eso está `tracking.as400_sync` con su claim atómico — y por eso el launcher advierte cuando ve batches `in_progress` ajenos en el tracking: es lo único que puede ver de la otra estación.

## Ver también

- [`aimd-auto-tuning.md`](aimd-auto-tuning.md) — el controlador que el techo manual acota
- [`heavy-light-lanes.md`](heavy-light-lanes.md) — por qué el piso del techo es 2 en modo dual-lane
- [`idempotency-and-retries.md`](idempotency-and-retries.md) — qué garantiza que una corrida cancelada sea reanudable
- [`diagrams/console-flow.md`](../diagrams/console-flow.md) — la máquina de estados y la cadena de guardas
- [`diagrams/sync-subsystem.md`](../diagrams/sync-subsystem.md) — qué hay detrás de `[8]`
- [`reference/cli.md`](../reference/cli.md#console--consola-de-operación-123135) — flags, pestañas y teclas
- [`how-to/probar-la-consola.md`](../how-to/probar-la-consola.md) — el recorrido guiado, paso a paso
- [`adr/008-textual-tui.md`](../adr/008-textual-tui.md) — por qué Textual y no logs a stdout
