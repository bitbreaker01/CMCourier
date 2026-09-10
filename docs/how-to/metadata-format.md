# How-to: Normalizar el valor de un metadato con `format` (146)

> [← Volver al índice](../INDEX.md) · [How-to](README.md)

Hasta 146 el valor de una propiedad viajaba **crudo** desde la fuente
hasta Content Manager: lo que hubiera en la columna era lo que se subía,
y el formato dependía de qué fuente ganó la carrera. El bloque `format:`
te deja decidir a VOS cómo se escribe, una vez, y que valga siempre.

Ausente ⇒ comportamiento byte-idéntico al pre-146. No hay nada que migrar.

## Las dos ubicaciones

El mismo modelo, dos momentos distintos:

```yaml
metadata:
  field_sources:
    BAC_Num_Cuenta:
      sources:
        - source_type: rvabrep
          lookup_value_column: index2
          format:                          # (a) POR FUENTE
            trim: true
            strip_leading_zeros: true
            pad_left: {width: 9, char: "0"}
          validation:
            allowed_pattern: '^\d{9}$'     # ahora se puede ser estricto
      default_value: "0"
      format:                              # (b) POR CAMPO
        pad_left: {width: 9, char: "0"}
```

- **(a) Por fuente** — corre **entre buscar el valor y validarlo**.
  Normaliza la vestimenta ANTES del patrón: es lo que te permite escribir
  patrones estrictos sin perder valores buenos.
- **(b) Por campo** — corre sobre el **valor ganador** y sobre
  **`default_value`**, justo antes de devolverlo. Es la garantía de
  salida: gane la fuente que gane, a CM llega el mismo formato.

Las dos son opcionales e independientes.

## El orden es fijo

No es una lista de pasos configurable — un orden libre vuelve el YAML
imposible de leer y de auditar. Las claves se aplican SIEMPRE así:

| # | Clave | Qué hace |
|---|-------|----------|
| 1 | `trim` | `value.strip()` |
| 2 | `case` | `"upper"` / `"lower"` |
| 3 | `strip_leading_zeros` | saca los ceros de la izquierda |
| 4 | `pad_left` | rellena a la izquierda hasta `width` (relleno default `"0"`) |
| 5 | `pad_right` | rellena a la derecha hasta `width` (relleno default `" "`) |
| 6 | `truncate` | se queda con los **primeros** `N` caracteres |

Reglas de borde que conviene tener presentes:

- **`strip_leading_zeros` nunca devuelve vacío**: `"00000"` → `"0"`. Un
  vacío significa "esta fuente no dio" y borraría un valor legítimo.
- **`pad_*` nunca recorta**: si el valor ya es más largo que `width`,
  vuelve intacto. Para recortar está `truncate`, que es explícito.
- **El resultado vacío corta la fuente**: si después de formatear queda
  `""`, esa fuente se trata como "no dio" y se pasa a la siguiente.
- `truncate` menor que un `pad_*.width` **falla al cargar el YAML**:
  rellenar para después cortar no tiene lectura sensata.

---

## Caso 1 — La cuenta de 9 dígitos que llega con dos vestimentas

**El problema.** La misma cuenta aparece en RVI como `1000` o como
`000001000`. Con esto:

```yaml
    BAC_Num_Cuenta:
      sources:
        - source_type: rvabrep
          lookup_value_column: index2
          validation:
            allowed_pattern: '^\d{9}$'
      default_value: "0"
```

el `1000` no valida, la fuente se descarta y el pipeline cae al
fallback: **se pierde un valor bueno por un tema de relleno**. Y el
`default_value: "0"` tampoco salva nada — se valida contra el patrón de
la PRIMERA fuente, así que muere con `DefaultValidationFailedError`.

La única salida pre-146 era aflojar el patrón a `^\d{1,9}$`: renunciar a
validar el largo, y que a CM llegue `1000` en unos documentos y
`000001000` en otros.

**El arreglo.**

```yaml
    BAC_Num_Cuenta:
      sources:
        - source_type: rvabrep
          lookup_value_column: index2
          format:
            trim: true
            strip_leading_zeros: true
            pad_left: {width: 9, char: "0"}
          validation:
            allowed_pattern: '^\d{9}$'
      default_value: "0"
      format:
        pad_left: {width: 9, char: "0"}
```

Qué pasa ahora, paso a paso:

| Lo que entrega la fuente | Después del `format` de fuente | ¿Valida `^\d{9}$`? | Lo que llega a CM |
|---|---|---|---|
| `"1000"` | `"000001000"` | sí | `000001000` |
| `"000001000"` | `"000001000"` | sí | `000001000` |
| `" 000001000 "` | `"000001000"` | sí | `000001000` |
| `"ABC"` | `"000000ABC"` | no → fuente descartada | cae al default |
| (nada) | — | — | default `"0"` → `format` de campo → `"000000000"` |

Fijate en la última fila: el default **se formatea y DESPUÉS se valida**.
Ese `"0"` que antes mataba la corrida ahora llega como `"000000000"` y
pasa el `^\d{9}$` sin tocar el patrón.

**Verificalo antes de correr:**

```bash
cmcourier types check
```

Si `pad_left.width` supera el `max_length` que declara CM para esa
propiedad, sale un WARNING: `field_sources.BAC_Num_Cuenta formatea a 9 y
el max_length de CM es 6`. Cuando hay `format`, ese largo reemplaza al
que se deducía del `allowed_pattern` — nunca vas a ver los dos warnings
para la misma propiedad.

---

## Caso 2 — La columna `CHAR(20)` de AS400 rellena con espacios

**El problema.** DB2 for i devuelve una columna `CHAR(n)` **siempre** con
el largo declarado. Una cuenta `1000` en un `CHAR(20)` sale como:

```
"1000                "
```

Pre-146 eso viajaba tal cual a Content Manager: 16 espacios de basura
adentro de la propiedad. Peor todavía, una fila SIN dato entrega 20
espacios — un valor que no es `None` ni `""`, así que el resolver lo
tomaba como bueno, cortaba la cadena de fallback y **no probaba la
fuente siguiente**.

**El arreglo.** Un `trim` por fuente resuelve las dos cosas:

```yaml
    BAC_Num_Cuenta:
      sources:
        - source_type: "as400:cuentas"       # CHAR(20), viene rellena
          lookup_value_column: NROCTA
          lookup_key_column: CIF
          format:
            trim: true
        - source_type: rvabrep               # el fallback de siempre
          lookup_value_column: index2
      default_value: "0"
```

- `"1000                "` → `trim` → `"1000"` → a CM va `1000`, sin
  espacios.
- `"                    "` (fila sin dato) → `trim` → `""` → **la fuente
  se trata como "no dio"** y la cadena sigue con `rvabrep`. Esa es la
  regla del resultado vacío, y es exactamente lo que querés acá.

Si además la propiedad de CM es de largo fijo y querés escribirla
rellena, agregá el `format` por campo — el `pad_right` rellena con
espacio por default:

```yaml
      format:
        pad_right: {width: 20, char: " "}
```

Ojo con esto último: `types check` te va a avisar si esos 20 caracteres
no entran en el `max_length` que declara CM.

---

## Ver también

- [`../reference/config-schema.md`](../reference/config-schema.md) — `ValueFormatModel`, `PadLeftModel` / `PadRightModel`, la tabla de orden y los validadores.
- [`../reference/config-reference.yaml`](../reference/config-reference.yaml) — el bloque comentado, listo para copiar.
- [`cm-type-manifest.md`](cm-type-manifest.md) — `types check`, de dónde salen los WARNINGs de `max_length` y `choices`.
