# 158 — Prueba de carga de TODOS los tipos configurados

## Por qué

Antes de una migración de miles, el operador quiere una respuesta a una
pregunta concreta: **¿todos los tipos documentales que configuré suben
bien a Content Manager con sus metadatos?** Hoy `[0] PRUEBA` prueba UN
código por vez (spec 141). Con 40+ tipos, probarlos de a uno es
impracticable — y justo el error que buscás (un metadato que CM rechaza
en un tipo puntual) es el que se te escapa si no los probás todos.

## Qué

### REQ-001 — Un tiro por cada tipo, en la misma pasada

Sección nueva en `[0] PRUEBA`: **"Probar todos los tipos"**. Sube UN
documento por cada tipo configurado y muestra, en tiempo real, cuáles
suben bien y cuáles no con su razón. Reusa la maquinaria de 141
(`run_practice_upload`, `build_properties`, `validate_values`): no pasa
por tracking ni por idempotencia, y los documentos llevan el prefijo
`PRUEBA-` para distinguirlos de una migración real.

Alcance de "todos los tipos": los `IDCM` que un migración real tocaría —
los referenciados por `MapeoRVI_CM.csv` que resuelven en el manifest.
Un toggle "incluir tipos no mapeados" prueba además el resto del manifest
con al menos una propiedad `usar`. Default: sólo los mapeados (probar
tipos que nadie migra es basura en CM).

### REQ-002 — Los metadatos se piden UNA vez por campo distinto

El operador NO tipea los metadatos por documento. Se toma la **unión de
los nombres canónicos** de las propiedades `usar` de todos los tipos a
probar (`canonical_name` de `usable_properties`), se le pide **un valor
por cada campo distinto**, y ese valor se reusa en todos los tipos que
usan ese campo. Diez campos distintos ⇒ diez preguntas, aunque se suban
3000 documentos. Para cada tipo, sus propiedades se arman con el subconjunto
de esos valores que el tipo efectivamente usa, mapeando canónico → id de
wire de ESE tipo (`cmis_property_ids`).

La pantalla muestra, al lado de cada campo, en cuántos tipos se usa —
para que el operador entienda qué está definiendo.

### REQ-003 — Un solo PDF, generado al vuelo, con la info de la prueba

Se genera **un único** PDF sintético al vuelo y se reusa como cuerpo de
todos los uploads (mismos bytes; lo que cambia por tipo es el nombre, la
carpeta, el object type y los metadatos). El PDF lleva adentro, como
texto legible, la marca de la prueba:

```
PRUEBA DE CARGA — CMCourier
No es un documento de migración real.
Generado: <fecha y hora ISO>
Operador:  <operator>
Estación:  <station>
Entorno:   <environment>
```

Así, cualquiera que lo abra en CM ve de qué se trata y de cuándo, sin
depender del nombre del archivo. La marca es genérica (el mismo PDF para
todos): el tipo puntual va en el nombre (`PRUEBA-<IDCM>-<ts>.pdf`) y en
los metadatos, no adentro del PDF reusado.

### REQ-004 — Resultado en vivo, tipo por tipo

Una tabla que se llena a medida que cada upload responde:

```
IDCM   TIPO                         ESTADO   RAZÓN
DC01   PT95 - Escritura…            ✔ OK     201 Created
TC18   Tarjeta de crédito           ✖ FALLA  400 · propiedad BAC_X no existe en el tipo
PP16   Préstamo personal            ✔ OK     201 Created
```

La razón de una falla sale de la respuesta CRUDA de CM (status + el
mensaje del body), igual que el tiro único de 141 — es exactamente el
dato que permite arreglar la config. Al terminar: un resumen
`N OK · M FALLA` y, si hubo fallas, que NO se anuncie en verde.

Corre en un worker (no bloquea la UI), con un tope de concurrencia bajo y
un `stop` cooperativo — es una prueba, no una corrida de carga.

### REQ-005 — Limpieza

Cada upload que sale bien deja su `cm_object_id`. Al terminar, un botón
**"borrar los de prueba"** los borra todos de CM en un tiro (reusa el
borrado del tiro único de 141, en lote). Un fallo de borrado individual
no aborta el resto; se reporta cuáles quedaron. Los `PRUEBA-` que no se
puedan borrar se listan para que el operador los limpie a mano.

### REQ-006 — Docs

`docs/explanation/operations-console.md` (la sección de `[0] PRUEBA`),
una how-to de operador ("verificar que todos los tipos suben antes de
migrar"), y `CHANGELOG.md`.

## Fuera de alcance

- Resolver los metadatos desde las fuentes reales (`field_sources`). Acá
  el operador DICTA los valores: la prueba verifica que el **esquema** de
  metadatos sube a CM, no la resolución — para eso está `types check` y la
  migración real.
- Escribir en tracking o coordinar con AS400. Es una prueba, invisible
  para el `pipeline`.
