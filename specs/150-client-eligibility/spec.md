# 150 — Elegibilidad: sólo los clientes con producto activo

## Por qué

Directiva de negocio: Content Manager no tiene espacio para todo RVABREP,
así que sólo se migran los documentos de clientes **con producto activo**
— alguna cuenta, algún certificado de depósito, alguna tarjeta o algún
afiliado activo. El banco produce un CSV con el `Shortname` y el `CIF` de
esos clientes.

Lo que NO se quiere es lo que el sistema hacía antes del censo (148):
filtrar a oscuras. Un documento que no se migra por esta directiva tiene
que aparecer en el censo diciéndolo con todas las letras, igual que
cualquier otra exclusión.

## Qué

### REQ-001 — Bloque `eligibility:` con perilla

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
    - {field: BAC_CIF,       column: CIF}
```

- `enabled: false` (default) ⇒ comportamiento idéntico al pre-150: no se
  evalúa nada, no se emite ninguna razón. La perilla apagada es
  byte-equivalente a que el bloque no exista.
- `source` usa la misma sintaxis `"<kind>:<alias>"` que `field_sources`,
  contra un alias declarado en `metadata.sources`. El validador de kind
  que ya existe aplica igual.
- `match_any`: lista no vacía de `{field, column}`. `field` tiene que ser
  una clave de `metadata.field_sources`; `column`, una columna de la
  fuente. **Basta con que UNA matchee** para considerar al cliente
  activo — el CSV trae las dos columnas y una fila a la que le falte una
  no debería costar la exclusión del cliente entero.
- Validación al cargar el YAML: `enabled` sin `source`, alias no
  declarado, `match_any` vacío, o un `field` que no existe en
  `field_sources` ⇒ error de config.

### REQ-002 — Una lista rota NO es "nadie está activo": la corrida no arranca

Con `enabled: true`, antes de procesar el primer documento se verifica
que la fuente **abra, tenga las columnas declaradas, y tenga al menos una
fila**. Si algo de eso falla, la corrida **aborta** con un mensaje que
nombra la fuente y el problema concreto.

Esto es lo central de la spec. Si la lista faltara y el pipeline siguiera,
produciría un censo impecable diciendo que 200.000 documentos se
excluyeron por cliente inactivo: un reporte prolijo y completamente
falso. **Una lista vacía no puede significar "no migres nada"** — eso es
un accidente disfrazado de decisión.

No se falla documento por documento: no se arranca. El chequeo vive en el
preflight de la corrida y también como check `eligibility_list` del
`doctor` (grupo `mapping`, SKIP con la perilla apagada).

### REQ-003 — Dónde corre, y qué cuesta

Corre en S2, **inmediatamente después de resolver la identidad** (147) y
antes de `get_mapping`: para saber si el cliente está activo hay que
saber primero quién es el cliente.

Consecuencia que se documenta explícitamente: **este filtro ahorra
espacio en Content Manager, no tiempo de proceso.** Un documento de un
cliente inactivo paga igual toda la cadena de resolución (afiliado hijo →
padre → shortname → CIF) antes de poder descartarse. Lo hace tolerable el
memo por corrida de 147: el primer documento de un cliente inactivo paga
los saltos, los demás del mismo cliente van gratis. Las búsquedas contra
la lista se memoizan con la misma clave `(fuente, columna, valor)`.

### REQ-004 — `CLIENT_NOT_ACTIVE` y la precedencia

`ReasonCode.CLIENT_NOT_ACTIVE` nuevo, balde **EXCLUIDO** — es una
decisión de negocio, no un error ni una falta de configuración. El
documento se registra y no avanza a S3.

Precedencia entre exclusiones, por orden del pipeline (que además va de
más barato a más caro). La primera que aplica, gana:

| # | Razón | Cuándo |
|---|-------|--------|
| 1 | `DELETED_AT_SOURCE` | S1, gratis |
| 2 | `EXCLUDED_BY_FILTER` | S1, gratis |
| 3 | `ALREADY_UPLOADED` | S1, una lectura |
| 4 | `IDENTITY_UNRESOLVED` | S2, con lookups |
| 5 | `CLIENT_NOT_ACTIVE` | S2, después de la identidad |

El renglón 4 importa: un documento cuya identidad no se resuelve **nunca
llega a evaluarse contra la lista**. Sale como `IDENTITY_UNRESOLVED`
(BLOQUEADO), no como inactivo. Es correcto — no sabemos si su cliente
está activo porque no sabemos quién es.

### REQ-005 — Qué lista se usó: auditoría

El CSV de activos es una foto de un momento. Dentro de seis meses alguien
va a leer el censo y preguntar *"¿activo según qué lista?"*.

Se registran en `migration_batch` la ruta de la fuente, su fecha de
modificación y su cantidad de filas, con el patrón aditivo de las
columnas de auditoría de la spec 124. `batch show` los muestra junto al
conteo de `CLIENT_NOT_ACTIVE`.

### REQ-006 — Docs

`docs/how-to/` guía de la elegibilidad (armar el CSV, prender la perilla,
leer el resultado en el censo, y la advertencia de que no ahorra tiempo);
`docs/reference/config-schema.md` + `config-reference.yaml`;
`docs/explanation/pipeline-stages.md` (S2 evalúa elegibilidad);
`docs/how-to/operator/read-the-batch-census.md` (el código nuevo en el
balde EXCLUIDO: no hay nada que hacer, es la directiva de negocio);
`docs/reference/cli.md` si cambia algún `--help`; `CHANGELOG.md`.

## Fuera de alcance

- **Una segunda lista de afiliados activos.** Confirmado con el operador:
  un afiliado activo es uno de los criterios que hacen activo al cliente,
  y eso ya viene resuelto dentro del mismo CSV. No existe el caso de un
  cliente activo con un afiliado puntual que no deba migrarse.
- **Filtrar antes de resolver la identidad.** Imposible por definición:
  no se puede filtrar por cliente sin saber el cliente.
