# 146 — Formato declarativo del valor de un metadato

## Por qué

Hoy el valor de una propiedad viaja **crudo** desde la fuente hasta el
wire. `MetadataService._resolve_one` (`services/metadata.py:311-336`)
hace exactamente esto y nada más:

```python
for sc in fsc.sources:
    value = self._fetch_from_source(...)
    if value is None or value == "":
        continue
    if not _validates(value, sc.validation):
        continue
    return value
```

No hay un solo lugar donde normalizar. Eso rompe dos cosas que el
operador ya tiene en producción:

- **El mismo dato llega con dos vestimentas.** Una cuenta de 9 dígitos
  aparece en RVI como `1000` o como `000001000`. Con
  `allowed_pattern: '^\d{9}$'` el `1000` no valida, la fuente se descarta
  y el pipeline cae al fallback: se pierde un valor bueno por un tema de
  relleno. La única salida hoy es aflojar el patrón a `^\d{1,9}$`, o sea
  renunciar a validar el largo.
- **Las columnas `CHAR` de AS400 vienen rellenas con espacios.** Un
  `CHAR(20)` con la cuenta `1000` entrega `"1000                "`. Eso
  hoy llega tal cual a Content Manager.

Y del lado de la escritura no hay ninguna garantía: el formato del valor
que se sube depende de qué fuente ganó la carrera. El operador quiere lo
contrario — decidir ÉL cómo se escribe, una vez, y que valga siempre.

## Qué

### REQ-001 — Bloque `format:` en dos lugares

El mismo modelo, dos ubicaciones, dos momentos distintos:

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

- **(a) Por fuente** (`FieldSourceItem.format`): corre entre buscar el
  valor y validarlo. Normaliza la vestimenta ANTES del patrón, que es lo
  que permite escribir patrones estrictos sin perder valores buenos.
- **(b) Por campo** (`FieldConfig.format`): corre sobre el valor ganador
  y sobre `default_value`, justo antes de devolverlo. Es la garantía de
  salida: gane la fuente que gane, a CM llega el mismo formato.

Las dos son opcionales e independientes. Ausentes ⇒ comportamiento
byte-idéntico al pre-146.

### REQ-002 — Las transformaciones y su orden

`ValueFormatModel` (nuevo, en `config/schema.py`, `extra: forbid`), todos
los campos opcionales. Se aplican SIEMPRE en este orden fijo — no es una
lista de pasos configurable, porque un orden libre vuelve el YAML
imposible de leer y de auditar:

| # | Clave | Tipo | Qué hace |
|---|-------|------|----------|
| 1 | `trim` | `bool` (default `False`) | `value.strip()` |
| 2 | `case` | `"upper"` \| `"lower"` \| `None` | `upper()` / `lower()` |
| 3 | `strip_leading_zeros` | `bool` (default `False`) | quita ceros a la izquierda |
| 4 | `pad_left` | `{width: int > 0, char: str len 1 = "0"}` | rellena a la izquierda hasta `width` |
| 5 | `pad_right` | `{width: int > 0, char: str len 1 = " "}` | rellena a la derecha hasta `width` |
| 6 | `truncate` | `int > 0` | se queda con los primeros `N` caracteres |

Reglas de borde, todas obligatorias:

- **`strip_leading_zeros` nunca devuelve vacío.** `"00000"` → `"0"`, no
  `""`. Un vacío significa "esta fuente no dio" y borraría un valor
  legítimo.
- **`pad_*` nunca recorta.** Si el valor ya es más largo que `width`, se
  devuelve intacto. Para recortar está `truncate`, que es explícito.
- **El resultado vacío corta la fuente.** Si después de formatear el
  valor queda `""` (el caso `CHAR` todo espacios con `trim: true`), la
  fuente se trata como "no dio" y se pasa a la siguiente. Ese es el
  comportamiento correcto y hoy no existe: hoy ese valor de espacios
  viaja al wire.
- **Validador de schema**: `truncate` menor que `pad_left.width` o que
  `pad_right.width` es un error de config (rellenar para después cortar
  no tiene lectura sensata). Falla al cargar el YAML, no en runtime.
- Un `format:` sin ninguna clave seteada es válido y es un no-op.

### REQ-003 — Dónde se enchufa en el resolver

`services/metadata.py`: `ValueFormat` (dataclass frozen, dominio del
servicio, espejo de `ValueFormatModel`), `apply_format(value, fmt) -> str`
pura, y `FieldSourceConfig.format` / `SourceConfig.format`.

`_resolve_one` pasa a:

```
para cada fuente:
    value = fetch(...)
    si value is None: seguir
    value = apply_format(value, sc.format)      # (a)
    si value == "": seguir
    si no valida(value, sc.validation): seguir
    devolver apply_format(value, fsc.format)    # (b)

# ninguna fuente dio
si no hay default: SourceFailedError
default = apply_format(fsc.default_value, fsc.format)   # (b) también al default
si no valida(default, first_validation): DefaultValidationFailedError
devolver default
```

Notar el orden en el default: **se formatea y DESPUÉS se valida**. Eso
arregla de paso la trampa actual (el `default_value` se valida contra el
patrón de la PRIMERA fuente): con `format` por campo, un
`default_value: "0"` se convierte en `"000000000"` y pasa un
`^\d{9}$` que hoy lo mataría con `DefaultValidationFailedError`.

El `format` por campo corre DESPUÉS de la validación de la fuente. Si el
operador escribe un `format` de campo que rompe su propio patrón, es
decisión suya y `types check` lo avisa (REQ-004) — el resolver no lo
adivina.

`config/wiring.py` mapea los dos modelos nuevos a sus dataclasses. El
cache de metadata (037) guarda el valor YA formateado: es el valor que va
al wire.

### REQ-004 — `types check` cruza el formato con `max_length`

En `services/manifest_check.py`, para cada propiedad `usar`:

- **Largo declarado por el formato**: `truncate`, si no el mayor de
  `pad_left.width` / `pad_right.width`. Es un largo EXACTO cuando hay
  `pad_*` (el valor sale con ese largo o más) y un tope cuando hay
  `truncate`.
- Si ese largo supera el `max_length` que declara CM → WARNING
  `field_sources.{name} formatea a {n} y el max_length de CM es {m}`.
  Prioriza sobre el WARNING actual deducido del `allowed_pattern`: si hay
  `format`, el patrón ya no manda sobre el largo de salida (nunca los
  dos para la misma propiedad).
- Si la propiedad tiene `format` con `case: upper` y CM declara `choices`
  no vacío donde ninguna opción está en mayúsculas → WARNING (el valor
  formateado nunca va a matchear una opción válida).

### REQ-005 — Docs

- `docs/reference/config-schema.md` y `docs/reference/config-reference.yaml`:
  el bloque `format` en las dos ubicaciones, la tabla de orden, las
  reglas de borde.
- `docs/how-to/` — sección nueva en la guía de metadata (o how-to propio
  si no hay una): el caso de la cuenta con ceros a la izquierda de punta
  a punta, y el caso `CHAR` de AS400 con espacios.
- `docs/reference/cli.md`: sólo si cambia algún `--help`.
- `CHANGELOG.md` `[Unreleased] ### Added`.

## Fuera de alcance

- `digits_only` y `replace: {pattern, with}` (regex arbitrario): un regex
  mal escrito corrompe datos en silencio y no hay forma de que el check
  lo detecte. Si aparece el caso real, se discute con su propia spec.
- Formato por clase documental: `field_sources` es global por propiedad.
  Si mañana el mismo metadato necesita dos formatos según el tipo, eso es
  otra spec y probablemente vive en el manifest, no en el YAML.
