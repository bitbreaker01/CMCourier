# How-to: Resolver la identidad del cliente con una cadena de saltos (147)

> [← Volver al índice](../INDEX.md) · [How-to](README.md)

RVABREP no siempre trae la identidad del cliente. A veces viene sólo el
shortname, a veces sólo el CIF, a veces ninguno de los dos y lo único que
hay en la fila es un **afiliado hijo**. Conseguir el CIF a partir de eso
puede exigir tres saltos contra dos tablas distintas:

```
afiliado hijo → afiliado padre → shortname → CIF → nombre del cliente
   (rvabrep)      (AFILIADOS)     (CLIENTES)  (CLIENTES)  (CLIENTES)
```

Hasta 147 nada de eso era expresable. Había un self-healing de CIF con la
idea correcta y tres límites duros: sólo curaba el CIF (el nombre del campo
estaba **hardcodeado**), sólo daba **un salto**, y el valor curado **no
llegaba a AS400** — el tracking guardaba el CIF resuelto y `CTENUM` el
crudo, así que las dos tablas discrepaban sobre el mismo documento.

Esta guía arma la cadena de punta a punta.

## Cuándo aplica

- La fila RVABREP no trae el CIF (o no trae el shortname, o ninguno).
- Necesitás más de un salto para llegar: el dato vive detrás de otro dato
  que también hay que buscar.
- Te rompió un `22003` del lado del banco, o encontraste filas con
  `CTENUM = 0` que no corresponden a ningún cliente.

Si tu RVABREP ya trae la identidad limpia, **no necesitás nada de esto**.
El bloque `identity:` es opcional entero, y sin declararlo el pipeline se
comporta exactamente como antes de 147.

## Pre-requisitos

- Las tablas de lookup declaradas en `metadata.sources` con su alias.
- Las credenciales de esas conexiones en el environment (ver
  [`reference/config-schema.md`](../reference/config-schema.md#secrets-env-vars-configloaderpysecrets)).
- Saber la **precisión REAL** de la columna `CTENUM` de tu tabla de log
  (`RVIMGLOG` en la instalación del operador, `NIARVILOG` por default).
  Si no la sabés, el paso 4 te la dice.

---

## 1. Declarar un campo por cada eslabón

La regla es una sola: **cada salto es un campo**. No hay sintaxis especial
de cadena — la cadena emerge de que un campo declare que su clave de
búsqueda es el valor ya resuelto de otro.

```yaml
metadata:
  sources:
    - kind: as400
      alias: afiliados
      as400_connection: rvi                # alias de `connections:`
      table: AFILIADOS
    - kind: as400
      alias: clientes
      as400_connection: rvi
      table: CLIENTES

  field_sources:

    # Salto 0 — el punto de partida sale de la fila RVABREP, gratis.
    BAC_Afiliado_Hijo:
      sources:
        - source_type: rvabrep
          lookup_value_column: index1

    # Salto 1 — hijo → padre.
    BAC_Afiliado_Padre:
      sources:
        - source_type: "as400:afiliados"
          lookup_value_source: "field.BAC_Afiliado_Hijo"
          lookup_key_column:   AFIHIJ
          lookup_value_column: AFIPAD

    # Salto 2 — padre → shortname. Con un atajo: si RVABREP YA trajo el
    # shortname, la primera fuente gana y los saltos ni se pagan.
    BAC_Shortname:
      sources:
        - source_type: trigger
          lookup_value_column: shortname
        - source_type: "as400:clientes"
          lookup_value_source: "field.BAC_Afiliado_Padre"
          lookup_key_column:   CUSAFI
          lookup_value_column: CUSSHN
      format:
        trim: true                          # CHAR(n) de DB2 viene con espacios (146)

    # Salto 3 — shortname → CIF.
    BAC_CIF:
      sources:
        - source_type: "as400:clientes"
          lookup_value_source: "field.BAC_Shortname"
          lookup_key_column:   CUSSHN
          lookup_value_column: CUSCIF
      format:
        trim: true
        strip_leading_zeros: true

    # Salto 4 — CIF → nombre. Es metadata común, pero cuelga de la misma
    # cadena y por eso tampoco se vuelve a resolver en S3.
    BAC_Nombre_Cliente:
      sources:
        - source_type: "as400:clientes"
          lookup_value_source: "field.BAC_CIF"
          lookup_key_column:   CUSCIF
          lookup_value_column: CUSNOM
      format:
        trim: true

    BAC_Sistema:
      sources:
        - source_type: trigger
          lookup_value_column: system_id
```

Tres cosas que conviene tener claras acá:

- **El orden de declaración no importa.** El resolver arma el grafo de
  dependencias y lo ordena topológicamente. Podés declarar `BAC_CIF`
  primero y `BAC_Afiliado_Hijo` último; resuelve igual.
- **Sólo se resuelve lo que hace falta.** Los campos pedidos por el mapping
  más sus dependencias transitivas. Un campo que nadie pide ni nadie
  depende de él no dispara ni una consulta.
- **Un eslabón que no resuelve no aborta el documento.** La fuente que
  dependía de él se saltea (igual que si hubiera devuelto vacío) y la
  cadena sigue con la próxima fuente. Recién si NINGUNA fuente dio y no hay
  `default_value`, el campo queda sin resolver.

## 2. Declarar el bloque `identity:`

Los campos de arriba son metadata. El bloque `identity:` es el que dice
**cuál de ellos es la identidad** — el que alimenta el tracking, el log de
AS400 y la clave del mapeo:

```yaml
identity:
  shortname:
    field: BAC_Shortname
    on_missing: fail
  cif:
    field: BAC_CIF
    on_missing: fail
    max_digits: 9            # la precisión REAL de CTENUM (paso 4)
  system_id:
    field: BAC_Sistema
    on_missing: warn
```

`on_missing` decide qué pasa cuando la cadena no dio nada:

| Valor | Qué pasa |
|-------|----------|
| `fail` (default) | El documento falla como **`S2_FAILED`**, con un error que nombra el slot **y la cadena completa que se intentó**, fuente por fuente y con el motivo de cada descarte. |
| `warn` | WARNING con la misma cadena, el slot queda en `""` y el documento sigue. |
| `default` | Se usa `default_value` (que pasa a ser obligatorio). |

Los tres slots son opcionales **y son independientes**. Podés declarar sólo
`cif` y dejar shortname y sistema como estaban: un slot ausente se lee del
trigger, sin cadena, exactamente como antes de 147.

### Por qué `max_digits` importa tanto

Antes de 147 el valor que iba a `CTENUM` se calculaba así:

```python
int(cif) if cif.isdigit() else 0        # ← el bug
```

Eso validaba el **tipo** y nunca la **magnitud**. Dos consecuencias, las
dos vistas en producción:

1. Un CIF más largo que la precisión de `CTENUM` reventaba con `22003` del
   lado del banco — **1194 filas** en una sola corrida del operador.
2. Un CIF no numérico se escribía como **cliente `0`**, en silencio, en el
   log del banco.

`max_digits` compara el LARGO y no convierte nada: si el valor no entra, se
aplica `on_missing` y el operador se entera. Y cuando aun así no queda un
número válido para `CTENUM`, lo que va al wire es **`NULL`** — que es lo que
"no sé qué cliente es" significa en una columna numérica. Si querés el `0`,
lo declarás vos: `on_missing: default` + `default_value: "0"`. Explícito,
en el YAML, auditable.

## 3. Verificar que el YAML carga

Los errores de forma de la cadena **fallan al CARGAR**, nunca en runtime:

```console
$ cmcourier doctor --config config.yaml --check mapping
```

Rompe al cargar, con el problema completo en el mensaje, si:

- un `field.<NOMBRE>` apunta a un campo que no existe en `field_sources`;
- hay un **ciclo** (`A -> B -> C -> A`, con el ciclo entero en el texto);
- un `identity.<slot>.field` no es una clave de `field_sources`;
- pusiste `max_digits` en un slot que no es `cif`;
- pusiste `on_missing: default` sin `default_value`.

Eso es deliberado: una cadena mal escrita tiene que romper el lunes al
cargar la config, no el viernes a mitad de un batch de producción.

## 4. Verificar el ancho REAL de la columna

```console
$ cmcourier doctor --config config.yaml --check as400_column_widths
```

El check lee `QSYS2.SYSCOLUMNS` para la librería y tabla que tenés
configuradas en `tracking.as400_sync` (que **no** son necesariamente
`RVILIB.NIARVILOG` — en la instalación del operador la tabla es
`RVIMGLOG`) y compara la definición real contra lo que el pipeline manda:

| Columna | Contra qué se compara |
|---------|-----------------------|
| `CTENUM` | la precisión numérica real vs. `identity.cif.max_digits` |
| `CTECIF` | el largo declarado vs. el `format` del campo de `identity.shortname` |
| `IDNBAC` | vs. el `IDCM` más largo del Modelo Documental |
| `TIPIDN` | vs. el `CMISType` más largo del Modelo Documental |
| `OBJIDN` | informativo: lo devuelve CM, no hay cota configurable |
| `EERRMSG` | vs. los 1024 a los que el adaptador trunca el error |

Un FAIL nombra **la definición real y el valor configurado**, que es todo
lo que necesitás para decidir si agrandás la columna o bajás `max_digits`:

```
[FAIL] as400_column_widths  RVILIB.RVIMGLOG: CTENUM es DECIMAL(7,0) y el
       pipeline manda hasta 9 digitos — identity.cif.max_digits (9)
```

SKIP cuando `tracking.as400_sync.enabled` es `false`: sin tabla no hay
definición que leer.

> **CCSID 1208.** Las columnas en UTF-8 declaran su largo en **BYTES**, no
> en caracteres. Un `VARCHAR(30)` CCSID 1208 aguanta 30 bytes: un nombre
> con acentos entra en menos posiciones de las que parece. El check lista
> en `details` qué columnas están en 1208 justamente para que lo tengas
> presente al leer los anchos.

## 5. Correr y verificar

```console
$ cmcourier csv-trigger-pipeline run --config config.yaml --limit 5
```

Qué mirar:

- **Un `S2_FAILED` con `identity.<slot>`** en el mensaje significa que la
  cadena se cortó. El propio mensaje trae la traza completa: qué fuente se
  probó, en qué orden y por qué se descartó cada una (nunca los VALORES —
  son PII). Ahí ves cuál de los tres saltos falló.
- **`memo_hits` en el reporte** te dice cuántos saltos se ahorraron. Con
  muchos documentos del mismo cliente tiene que ser alto: la cadena se
  paga una vez por cliente y por corrida, no una vez por documento.
- **Tracking y `CTECIF`/`CTENUM` tienen que coincidir.** Es la garantía que
  147 agrega: las tres escrituras salen del mismo `ResolvedIdentity`. Si
  ves divergencia, es un bug — reportalo.

```console
$ cmcourier batch show --config config.yaml --batch-id <id>
```

## Lo que esto le cuesta a S2

**S2 ahora puede hacer red.** Antes era un `dict.get()`; con una cadena
declarada, paga los lookups. Tres cosas lo acotan:

1. La cadena se resuelve **una vez por documento**, al inicio de S2.
2. Cada salto se **memoiza por corrida** con clave
   `(campo, source_type, columna clave, columna valor, valor de clave)`:
   todos los documentos del mismo cliente pagan el primero y nada más.
3. Lo que S2 resolvió entra como **semilla de S3**, que por eso no vuelve a
   consultar ni el campo ni sus eslabones intermedios.

Con `prefetch_enabled: true` (el default) las tablas de lookup se indexan
enteras en memoria al arrancar, y entonces los saltos no son red en
absoluto. Eso conviene cuando la tabla entra en memoria; para tablas
grandes, apagalo y dejá que trabajen el memo y la semilla.

## Ver también

- [`reference/config-schema.md`](../reference/config-schema.md#identity-identity-147) — el schema completo de `identity:` y de `field.<NOMBRE>`
- [`reference/config-reference.yaml`](../reference/config-reference.yaml) — el YAML anotado, clave por clave
- [`metadata-format.md`](metadata-format.md) — `format:`, que es lo que normaliza el valor de cada eslabón (146)
- [`explanation/pipeline-stages.md`](../explanation/pipeline-stages.md#s2--mapping) — dónde encaja la resolución dentro de S2
- [`as400-sync.md`](as400-sync.md) — el log de AS400 que recibe `CTECIF`/`CTENUM`
- [`reference/cli.md`](../reference/cli.md#doctor) — la tabla completa de checks del `doctor`
