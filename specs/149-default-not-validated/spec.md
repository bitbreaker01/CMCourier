# 149 — El `default_value` no se valida en runtime

## Por qué

`MetadataService._resolve_one` valida el `default_value` contra el patrón
de la **PRIMERA fuente** del campo (`services/metadata.py`,
`first_validation = fsc.sources[0].validation if fsc.sources else None`,
y el `raise DefaultValidationFailedError` que le sigue).

Está mal por tres motivos, y el operador chocó con los tres:

1. **Es magia implícita.** Nada en el YAML dice que el default se valida,
   ni contra qué. Un acoplamiento que nadie escribió y que nadie puede
   leer en el archivo.
2. **Es un acoplamiento posicional.** Reordenar las fuentes de un campo
   —algo que se hace por rendimiento o por prioridad de negocio— cambia
   en silencio contra qué se valida el default. Un booby trap.
3. **Mata documentos en producción por un error de configuración.** El
   `default_value` lo escribe una persona a mano en el YAML. Si está mal,
   es un error de config: se corrige editando el archivo, no abortando
   documentos a las tres de la mañana.

El caso que lo destapó: el operador necesita
`BAC_Num_Cuenta_Tarjeta` con `default_value: "000000"` y una fuente
validada con `^[0-9]{14,16}$`. Seis dígitos contra un patrón de catorce a
dieciséis: hoy el YAML carga sin quejarse y el primer documento que cae
al default se muere con `DefaultValidationFailedError`. La configuración
que el negocio pide es, literalmente, inexpresable.

## Qué

### REQ-001 — El default deja de validarse

En `_resolve_one`: se eliminan `first_validation` y el
`raise DefaultValidationFailedError`. Cuando ninguna fuente da y hay
`default_value`, se devuelve el default y punto.

**Lo que NO cambia**: el `format:` de campo se sigue aplicando al default
(146 REQ-003). Formatear y validar son cosas distintas — la primera es
una transformación declarada por el operador, la segunda un juicio sobre
un dato ajeno. Un `default_value: "0"` con
`format: {pad_left: {width: 9, char: "0"}}` sigue saliendo
`"000000000"`.

`DefaultValidationFailedError` queda sin uso en el runtime. Se revisan
los callers: si nada fuera del propio módulo la atrapa, se borra; si
algo la atrapa, se deja deprecada un ciclo con el motivo en el docstring.

### REQ-002 — La red de seguridad se muda a `types check`

Lo que se pierde en runtime se gana en el check, que es donde
corresponde: el operador está sentado, con tiempo, leyendo un reporte.

En `services/manifest_check.py`, para cada campo con `default_value` no
nulo y al menos una fuente con `allowed_pattern`: si el default **ya
formateado con el `format` de campo** no matchea NINGUNO de esos
patrones, se emite un **INFO**:

```
metadata.field_sources.BAC_Num_Cuenta_Tarjeta: el default '000000' no
matchea ningún allowed_pattern de sus fuentes ('^[0-9]{14,16}$')
```

INFO y no WARNING a propósito: un default deliberadamente distinto de los
datos reales —un marcador como `000000` que después se busca y se
corrige— es una técnica legítima y frecuente. El check informa, no
juzga.

### REQ-003 — Docs

`docs/how-to/metadata-format.md` y `docs/reference/config-schema.md`:
el default no se valida, se formatea; la red está en `types check`.
Revisar que ningún doc siga afirmando lo contrario. `CHANGELOG.md`
`[Unreleased] ### Fixed` explicando el caso del operador.

## Fuera de alcance

- Una `validation:` propia a nivel de campo. Si el día de mañana alguien
  quiere validar el valor final —gane la fuente que gane, o venga del
  default— eso es una llave nueva explícita en el YAML y tiene su spec.
  Lo que 149 mata es la validación IMPLÍCITA, no la idea de validar.
