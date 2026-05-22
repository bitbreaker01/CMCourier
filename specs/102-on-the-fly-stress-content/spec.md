# 102 — Generación de contenido sintético on-the-fly para pruebas de stress

## Por qué

El plan de pruebas de stress (T01–T05) exige migrar hasta **13.000.000
de documentos / ~20 TB** contra el destino CMIS para encontrar su *knee
point*. Hoy no se puede correr esa prueba sin pisar dos paredes:

1. **No existe el corpus.** El RVABREP real no tiene tantas filas, y
   materializar 13M archivos físicos con `cmcourier mock generate`
   significa **20 TB de disco en el servidor de origen** — que no
   tenemos.
2. **Aunque tuviéramos el disco, sería contraproducente.** Si el origen
   lee 20 TB de su propio disco físico, el cuello de botella pasa a ser
   el I/O local del origen. La prueba mediría **nuestra propia lentitud
   leyendo archivos**, no la capacidad del destino. El *knee point* que
   reportaríamos sería falso.

La conversación de diseño ya cerró el modelo correcto:

* El **RVABREP** (qué migrar) es un artefacto **estático**, pre-generado
  una vez con `cmcourier mock rvabrep --rows 13000000 --image-mix
  pdf:100`. Esto NO entra en el alcance de este spec — ya existe (039).
* El **contenido** (el archivo que se sube) se genera **on-the-fly**,
  just-in-time, justo antes de que S3 lo necesite, y se descarta después
  de S5. Nunca se materializan 20 TB.

Verificación del código que habilita el camino rápido:

```
# pdf_assembler.py:152-175  — _passthrough_native_pdf_traced
if document.is_pdf:
    return self._passthrough_native_pdf_traced(document)
    # ... shutil.copy2(src, dst) — NO reabre, NO parsea el PDF
```

S4 con una fila PDF es una **copia**. El destino CMIS guarda el binario
sin interpretarlo. Por lo tanto el PDF generado solo necesita ser
**estructuralmente válido**, no un PDF renderizado con imágenes.

## Qué

Un proveedor de contenido sintético que, para cada documento del
pipeline, produce un **PDF de una sola página** de tamaño controlado,
generado on-the-fly y descartado tras el upload.

### Requisitos

**REQ-001 — PDF de una página, estructuralmente válido.** El contenido
es un PDF mínimo válido (header, catalog, un page object, un content
stream). Debe poder copiarse por S4 y almacenarse por CMIS sin error. NO
necesita renderizar imágenes ni texto real.

**REQ-002 — Distribución de tamaños configurable.** El operador
configura, en el YAML, la proporción por clase de tamaño y las bandas de
bytes de cada clase. Ejemplo (defaults alineados al plan §3.3):

```yaml
stress_content:
  size_mix:
    small:  { weight: 60, min: 50kb,  max: 1mb }
    medium: { weight: 30, min: 1mb,   max: 10mb }
    large:  { weight: 10, min: 10mb,  max: 40mb }
```

Los pesos se renormalizan (misma semántica que `ImageMix`).

**REQ-003 — Tamaño determinístico por documento.** La clase de tamaño y
el tamaño exacto de un documento se **derivan determinísticamente del
`txn_num`** (`Random(seed_global ^ hash(txn_num))`), NO se sortean fresco
en cada corrida. El mismo `txn_num` produce el mismo tamaño siempre.
Esto es obligatorio para la reproducibilidad (plan §9.3) y para que un
`--resume` no le cambie el peso a un documento a mitad de prueba.

**REQ-004 — Performance: la generación NO puede ser el cuello de
botella.** Requisito de diseño no negociable:

* **Prohibido** el loop de búsqueda de tamaño del `MockContentWriter`
  actual (hasta 10 iteraciones de `img2pdf.convert` + encode PIL por
  archivo). Eso sirve para fixtures de test, no para velocidad de línea.
* El costo de generación por documento debe ser **O(tamaño)** —
  esencialmente un `memset`/`memcpy` de relleno + un parche chico —
  acotado por el ancho de banda de memoria, que está **órdenes de
  magnitud por encima** de cualquier tasa de upload por red. Si la
  generación es solo relleno de buffer, es **físicamente imposible** que
  le gane al upload (limitado por red).
* Esqueleto del PDF pre-construido **una vez** al arranque (cache por
  clase de tamaño); por documento solo se materializa el relleno y se
  parchea la región única (REQ-006).
* **Criterio de aceptación:** un microbenchmark standalone que demuestre
  que el throughput de generación supera con holgura (≥10×) la tasa de
  upload máxima plausible. Se mide y se documenta antes de T02.

**REQ-005 — Sin I/O de disco físico del origen como cuello de botella.**
El diseño no debe forzar 20 TB de escrituras/lecturas en el disco físico
del servidor de origen. Ver "Decisión de diseño abierta".

**REQ-006 — Contenido no byte-idéntico.** Cada documento embebe una
región única derivada de su `txn_num` (~64 bytes) dentro del content
stream. Defiende contra una eventual deduplicación por hash de contenido
en el destino, que enmascararía la escritura real. Costo: parchear unos
bytes — gratis.

**REQ-007 — Limpieza acotada.** Si el contenido toca disco (staging), se
borra después de S5. En ningún momento coexisten más archivos que los
in-flight. Cero acumulación de 20 TB.

**REQ-008 — Coherencia con el RVABREP.** El generador respeta la fila:
usa su `txn_num` y asume fila PDF (`image_type=O`, `file_name` `.PDF`,
`total_pages=1`). El RVABREP debe haberse generado con `--image-mix
pdf:100`; una fila TIFF/JPEG con este generador activo es error de
configuración y se reporta como tal.

### No-soluciones descartadas

> ❌ **Reusar `MockContentWriter` tal cual.** Su loop de búsqueda de
> banda (`content.py:106-159`, hasta 10× `img2pdf.convert` + encode PIL
> de bytes aleatorios por archivo) es **exactamente** el cuello de
> botella que este spec existe para evitar. Para una imagen "grande"
> genera decenas de MB de bytes aleatorios y los JPEG-encodea. A 13M
> archivos es inviable.

> ❌ **Pre-materializar los 20 TB con `mock generate`.** Es el problema
> que originó este spec (ver "Por qué").

> ❌ **Bytes random sin estructura PDF.** El destino los guardaría
> igual, pero se pierde el mime-type realista y se arriesga que S4 o los
> comandos de diagnóstico fallen al inspeccionar el archivo. Un PDF
> mínimo válido cuesta lo mismo en CPU — no hay razón para no hacerlo
> bien.

### Decisión de diseño — staging memory-backed (REQUERIMIENTO REQ-005)

`StagedFile` es `path` + `size_bytes` (`models.py:402-406`): basado en
disco. Para que el contenido nunca pegue contra el disco físico del
origen se evaluaron tres caminos:

| Opción | Velocidad | Blast radius |
|--------|-----------|--------------|
| A. Just-in-time a disco físico | Limitada por disco origen | Mínimo |
| **B. Staging memory-backed (tmpfs / RAM-disk)** ✅ | Velocidad de memoria | **Cero** cambio de contrato |
| C. Stream in-memory hasta S5 (cambia `IUploader`) | Velocidad de memoria | Alto — toca el uploader productivo |

**Decisión: Opción B.** El código escribe a un directorio de staging
normal; el operador monta ese directorio sobre un filesystem respaldado
por RAM (tmpfs en Linux; RAM-disk en Windows Server). Logra velocidad de
memoria **sin tocar el contrato del uploader productivo** — riesgo cero
de regresión en S5.

Implicancias para el diseño/tareas:

* El código NO asume ni gestiona el tmpfs — lo monta el operador. El
  spec solo exige que nada del path generación→S3→S4→S5 asuma que el
  staging está en disco físico (hoy ya no lo asume; verificar).
* El runbook de las pruebas y `doctor` deben **documentar y validar** el
  montaje memory-backed: dimensionar la RAM contra el pico de archivos
  in-flight (`workers` × tamaño máximo de la banda `large`) más un
  margen. Un tmpfs que se llena bajo carga es una falla de prueba.
* REQ-007 (limpieza tras S5) es doblemente crítico acá: si el contenido
  no se borra, el tmpfs se llena y tumba la corrida.

### Fuera de alcance

* Generación del RVABREP — ya existe (`mock rvabrep`, spec 039).
* Control de tiempo de ejecución (`--max-duration`) — **spec hermano**.
* Pin de concurrencia / apagado de `auto_tune` para T03/T05 — **spec
  hermano**.
* Desglose de la tasa de error por tipo (timeout / 4xx / 5xx / app) —
  **spec hermano**.

## Escenarios

**E1 — Distribución respetada sobre el corpus.**
Dado un RVABREP de 100.000 filas PDF y `size_mix` 60/30/10,
cuando corre el pipeline,
entonces la proporción de documentos por clase de tamaño cae dentro de
±2% de 60/30/10, verificable en los logs de generación.

**E2 — Determinismo por `txn_num`.**
Dado un documento con `txn_num` `TAB3K7Q`,
cuando se genera su contenido en dos corridas distintas,
entonces el tamaño en bytes es idéntico en ambas.

**E3 — Resume no altera el tamaño.**
Dado un batch cortado a la mitad y reanudado con `--resume`,
cuando un documento ya subido se reintenta,
entonces su contenido pesa exactamente lo mismo que en el primer intento.

**E4 — La generación no es el cuello de botella.**
Dado el microbenchmark de generación,
cuando se mide el throughput de generación aislado,
entonces supera ≥10× la tasa de upload pico medida en T02/T03.

**E5 — Sin acumulación de disco.**
Dada una corrida de 500.000 documentos,
cuando termina,
entonces el espacio de staging usado es ≈0 (solo quedan, a lo sumo, los
in-flight de los últimos workers).

**E6 — Fila no-PDF rechazada.**
Dado un RVABREP con una fila `image_type=B` (TIFF) y el generador
sintético activo,
cuando el pipeline procesa esa fila,
entonces se reporta un error de configuración claro (el RVABREP debió
generarse con `--image-mix pdf:100`).
