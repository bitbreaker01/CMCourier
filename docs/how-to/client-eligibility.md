# How-to: Migrar sólo los clientes con producto activo (150)

> [← Volver al índice](../INDEX.md) · [How-to](README.md)

Content Manager no tiene espacio para todo RVABREP. La directiva de negocio
es clara: **sólo se migran los documentos de clientes con producto activo** —
alguna cuenta, algún certificado de depósito, alguna tarjeta o algún afiliado
activo. El banco produce un CSV con el `Shortname` y el `CIF` de esos
clientes.

Lo que NO se quiere es lo que el sistema hacía antes del censo (148):
**filtrar a oscuras**. Un documento que no se migra por esta directiva tiene
que aparecer en el censo diciéndolo con todas las letras, igual que cualquier
otra exclusión.

## Cuándo aplica

- Tenés la lista de clientes activos del banco y querés que el pipeline la
  respete.
- Estás mirando un censo y querés saber cuántos documentos quedaron afuera
  por esta directiva, y de qué códigos.

Si migrás todo RVABREP, **no necesitás nada de esto**. El bloque
`eligibility:` es opcional entero, y con su default (`enabled: false`) no se
abre ninguna fuente ni se evalúa ningún documento: el pipeline se comporta
exactamente como antes de 150.

## Pre-requisitos

- El CSV de activos, accesible desde la máquina que corre la migración.
- El bloque `identity:` (147) declarado, o al menos los campos de
  `metadata.field_sources` que vayas a usar como criterio.

---

## 1. Armar el CSV de activos

Un CSV con una fila por cliente activo. Las columnas que importan son las que
vas a declarar en `match_any`:

```csv
Shortname,CIF
ACMESA01,123456789
BANCOSA02,987654321
```

Dos reglas que salen de la lista real:

- **Puede tener filas con una celda vacía.** Un cliente cuyo `Shortname` no
  vino no arruina nada: `match_any` es un OR y el `CIF` alcanza.
- **No puede estar vacía.** Una lista sin filas no significa "nadie está
  activo" (ver el paso 4).

## 2. Declarar la fuente y prender la perilla

La lista entra por el mismo registro `metadata.sources` que cualquier otro
lookup, con su alias:

```yaml
metadata:
  sources:
    - kind: csv
      alias: clientes_activos
      csv_path: C:\ruta\clientes-activos.csv

eligibility:
  enabled: true                               # la perilla
  source: "csv:clientes_activos"
  match_any:                                  # cualquiera que matchee ⇒ activo
    - {field: BAC_Shortname, column: Shortname}
    - {field: BAC_CIF, column: CIF}
```

- `field` es el nombre canónico de `metadata.field_sources` cuyo valor **ya
  resuelto** se busca. Si además es un slot de `identity:`, sale gratis: la
  resolución de identidad ya lo dejó listo.
- `column` es la columna de la lista donde se lo busca.
- **Basta con que UNA matchee.** Un valor vacío nunca cuenta como match.

Todo esto se valida **al cargar el YAML**: sin `source`, con `match_any`
vacío, con un alias no declarado o con un `field` que no existe en
`field_sources`, el pipeline no arranca.

**Y se valida aunque la perilla esté apagada.** Un alias mal escrito es un
error de config esté la perilla donde esté; verificarlo no abre ninguna fuente
ni cambia ningún comportamiento, y te evita prender la perilla en producción
para recién ahí descubrir el typo. Lo único que no se valida es el bloque que
no dice nada: sin `eligibility:`, con `eligibility: {}` o con
`eligibility: {enabled: false}` a secas, no hay lista declarada y no hay nada
que verificar.

Lo que sí queda detrás de la perilla es el **comportamiento**: con
`enabled: false` la lista no se abre ni una vez, no corre el preflight del
paso 4 y no se evalúa ningún documento.

## 3. Verificar antes de lanzar

```
cmcourier doctor -c config.yaml --check eligibility_list
```

PASS te dice cuántos clientes activos tiene la lista y cuántos criterios de
match declaraste. Con la perilla apagada, SKIP.

## 4. Una lista rota NO es "nadie está activo"

Éste es el punto de la spec, y conviene entenderlo antes de la primera
corrida de producción.

Con `enabled: true`, **antes de procesar el primer documento** se verifica
que la fuente abra, tenga las columnas declaradas y tenga **al menos una
fila**. Si algo de eso falla, la corrida **aborta** con un mensaje que nombra
la fuente y el problema concreto.

No se falla documento por documento: **no se arranca**.

¿Por qué tan drástico? Porque si el pipeline siguiera con una lista vacía,
produciría un censo impecable diciendo que 200.000 documentos se excluyeron
por cliente inactivo: **un reporte prolijo y completamente falso**. Una lista
vacía no puede significar "no migres nada" — eso es un accidente disfrazado
de decisión. Una lista rota no significa "nadie está activo": significa que
**no podemos responder la pregunta**.

Si de verdad querés migrar todo, la forma de decirlo es `enabled: false`, no
un CSV vacío.

## 5. Leer el resultado en el censo

```
cmcourier batch show -c config.yaml <batch-id>
```

Los documentos excluidos aparecen así:

```
BALDE      RAZON              ID_RVI  DOCS
EXCLUIDO   CLIENT_NOT_ACTIVE  CC03    18432
```

`EXCLUIDO` significa **no hay nada que hacer**: es la directiva de negocio
funcionando. Ver
[`operator/read-the-batch-census.md`](operator/read-the-batch-census.md).

Debajo del censo, `batch show` te dice **qué lista** decidió eso:

```
Lista de activos (150): C:\ruta\clientes-activos.csv
  modificada: 2026-08-31T09:15:00 · filas: 412339
```

El CSV de activos es una foto de un momento. Dentro de seis meses alguien va
a leer el censo y preguntar *"¿activo según qué lista?"*: ésa es la respuesta.

## Advertencia: esto ahorra ESPACIO, no TIEMPO

Vale la pena decirlo explícito porque la intuición dice lo contrario:

> **El filtro de elegibilidad ahorra espacio en Content Manager, no tiempo de
> proceso.**

Un documento de un cliente inactivo paga igual **toda la cadena de
resolución** (afiliado hijo → padre → shortname → CIF) antes de poder
descartarse. No hay forma de evitarlo: para saber si el cliente está activo
hay que saber primero quién es el cliente, y eso es exactamente lo que cuesta
la cadena.

Lo que lo hace tolerable es el **memo por corrida** de 147: el primer
documento de un cliente inactivo paga los saltos, y todos los demás
documentos de ese mismo cliente van gratis. Las búsquedas contra la lista se
memoizan con la misma disciplina, con la clave `(fuente, columna, valor)` —
hits y misses por igual, porque el cliente inactivo es justamente el caso
caro.

## Precedencia con las otras exclusiones

La primera razón que aplica, gana. En orden de pipeline (que además va de más
barato a más caro):

| # | Razón | Cuándo |
|---|-------|--------|
| 1 | `DELETED_AT_SOURCE` | S1, gratis |
| 2 | `EXCLUDED_BY_FILTER` | S1, gratis |
| 3 | `ALREADY_UPLOADED` | S1, una lectura |
| 4 | `IDENTITY_UNRESOLVED` | S2, con lookups |
| 5 | `CLIENT_NOT_ACTIVE` | S2, después de la identidad |

El renglón 4 importa: **un documento cuya identidad no se resuelve nunca
llega a evaluarse contra la lista.** Sale como `IDENTITY_UNRESOLVED`
(BLOQUEADO), no como inactivo. Es correcto — no sabemos si su cliente está
activo porque no sabemos quién es.

## Fuera de alcance

- **Una segunda lista de afiliados activos.** Confirmado con el operador: un
  afiliado activo es uno de los criterios que hacen activo al cliente, y eso
  ya viene resuelto dentro del mismo CSV.
- **Filtrar antes de resolver la identidad.** Imposible por definición: no se
  puede filtrar por cliente sin saber el cliente.

## Ver también

- [`reference/config-schema.md`](../reference/config-schema.md) — el bloque
  `eligibility:` campo por campo.
- [`how-to/identity-chain.md`](identity-chain.md) — la cadena que resuelve
  quién es el cliente.
- [`explanation/pipeline-stages.md`](../explanation/pipeline-stages.md) — qué
  hace S2.
