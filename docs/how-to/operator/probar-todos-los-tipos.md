# Verificar que TODOS los tipos suben antes de migrar

> [← Volver al índice](../../INDEX.md) · [How-to](../README.md) · [Operador](README.md)

Antes de disparar una migración de miles con 40+ tipos documentales, la pregunta que importa no es "¿este código sube?" sino **"¿TODOS los tipos que configuré suben bien a Content Manager con sus metadatos?"**. Probarlos de a uno con el tiro único (`s` en `[0]`) es impracticable — y el error que buscás (un metadato que CM rechaza en UN tipo puntual) es justo el que se te escapa si no los probás todos. La sección "probar todos los tipos" de `[0] PRUEBA` sube un documento por cada tipo y te muestra, tipo por tipo, cuáles suben y cuáles no con la razón CRUDA del servidor.

## Cuándo usarlo

- Terminaste de configurar el mapping / manifest y querés confirmar que el **esquema** de metadatos entra en CM antes de migrar de verdad.
- Cambiaste una propiedad `usar`/`omitir` o una carpeta destino y querés verificar que no rompiste ningún tipo.
- Un batch real falló en S5 para varios tipos y querés reproducir el rechazo sin correr el pipeline entero.

Esto prueba el **esquema**, no la resolución de metadatos: los valores los DICTÁS vos, no salen de las fuentes reales. Para verificar la resolución están `cmcourier types check` y la migración real.

## Pre-requisitos

- Consola abierta: `cmcourier console --config tu-config.yaml`.
- Credenciales CMIS frescas cargadas y probadas OK en `[2] CREDENCIALES`.
- Un `mapping:` configurado (consolidado, split o manifest). Para el toggle "incluir tipos no mapeados" hace falta el modo manifest (`mapping.type_manifest_path`).

## Pasos

1. Andá a `[0] PRUEBA` (tecla `0` o `F10`) y bajá hasta la sección **"probar todos los tipos"**.
2. Elegí el **alcance**:
   - `sólo los tipos mapeados (recomendado)` — los `IDCM` que una migración real tocaría (los referenciados por `MapeoRVI_CM` que resuelven en el manifest).
   - `incluir tipos no mapeados del manifest` — agrega el resto de los tipos del manifest con al menos una propiedad `usar`. Sólo tiene efecto en modo manifest.
3. Tocá **`cargar tipos`**. La pantalla te dice cuántos tipos va a probar y cuántos **campos distintos** tenés que completar, y arma un formulario con **un campo por metadato distinto** — no uno por documento. Al lado de cada campo dice en cuántos tipos se usa.
4. Completá los valores. Cada valor se reusa en todos los tipos que usan ese campo (un `CIF` que compartan diez tipos se tipea una sola vez).
5. Tocá **`probar todos (subir)`** y confirmá (en PRD hay que tipear `PRD`). La tabla se llena en vivo: `IDCM · TIPO · ESTADO · RAZÓN`. Un `✔ OK` es `201 Created`; un `✖ FALLA` trae el status y el mensaje del body de CM — exactamente el dato que te dice qué arreglar en la config.
6. Leé el resumen final: `N OK · M FALLA`. Si hubo aunque sea una falla, el aviso NO sale en verde.
7. Si querés cortar antes de que termine, tocá **`detener`** (corta tras el tipo en curso).

## Limpiar lo que subiste

Cada upload exitoso deja un documento `PRUEBA-<IDCM>-<ts>.pdf` en CM (con la marca de la prueba legible adentro: fecha, operador, estación, entorno). **No pasan por tracking**, así que la limpieza es tuya:

- Tocá **`borrar los de prueba`** y confirmá: borra en lote todos los que subió esta prueba.
- Un borrado individual que falla no aborta el resto — los que quedaron se listan abajo para que los limpies a mano.

## Interpretar las fallas

| Razón típica | Qué significa | Dónde se arregla |
|--------------|---------------|------------------|
| `400 · propiedad X no existe en el tipo` | El tipo no declara esa propiedad de wire | El manifest / `MetadatosCM` de ese `IDCM` |
| `400 · value ... is not valid` | El valor que dictaste no pasa la validación de CM | Reintentá con un valor válido (es prueba de esquema) |
| `404 ...` | La carpeta o el object type no existen en el server | `M·MODELO` (carpeta) o el mapping (object type) |
| `RuntimeError: ...` | No hubo respuesta (red / TLS / credenciales) | `[2] CREDENCIALES` y `[4] DOCTOR` |

## Ver también

- [`../../explanation/operations-console.md`](../../explanation/operations-console.md) — por qué la prueba no toca tracking y por qué un solo PDF reusado (158).
- [`cm-type-manifest.md`](../cm-type-manifest.md) — descubrir y revisar el manifest de tipos CM.
