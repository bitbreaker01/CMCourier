# 147 — Resolución de identidad: cadenas configurables para shortname y CIF

> **Depende de 146** (bloque `format:`). El `pad_left` del CIF y el
> `trim` de las columnas `CHAR` de AS400 son de 146; acá se usan.

## Por qué

RVABREP no siempre trae la identidad del cliente. A veces viene sólo el
shortname, a veces sólo el CIF, a veces ninguno de los dos y lo único
disponible es un número de tarjeta o un afiliado hijo. Conseguir el CIF
puede exigir tres saltos: hijo → padre → shortname → CIF → nombre. Hoy
nada de eso es expresable.

Existe un self-healing de CIF (`services/metadata.py:249`) con la idea
correcta y tres límites duros:

1. **Sólo cura el CIF.** El nombre del campo está hardcodeado
   (`"BAC_CIF"`). El shortname no se cura nunca.
2. **Un solo salto.** `cif_override` es una variable suelta, y
   `lookup_value_source` sólo acepta `trigger.*` / `rvabrep.*`: no hay
   forma de encadenar un lookup sobre un valor ya resuelto.
3. **El valor curado no llega a AS400.** `metadata.py:281` sólo
   reconstruye el trigger cuando es un `ClientTrigger`; en el camino de
   producción es un `RvabrepRowTrigger` con fila inmutable, así que
   `audit_row()` sigue devolviendo el crudo. `try_claim`
   (`staged.py:1383`) usa `audit_row()`, mientras el tracking escribe el
   curado (`staged.py:1123`): **las dos tablas divergen sobre el mismo
   documento**.

Y del lado del wire, `int(cif) if cif.isdigit() else 0`
(`recovery.py:238`, `as400_niarvilog.py:587`) valida el TIPO y nunca la
MAGNITUD: un CIF más largo que la precisión de `CTENUM` rompe con
`22003` (1194 filas en la corrida del operador), y uno no numérico
escribe **`0`** en el log del banco sin decir nada.

## Qué

### REQ-001 — Encadenar lookups: `lookup_value_source: "field.<NAME>"`

Tercer scope de `FieldSourceItem.lookup_value_source`, junto a
`trigger.<attr>` y `rvabrep.<col>`: **`field.<CANONICAL_NAME>`** — la
clave de búsqueda es el valor ya resuelto de otro campo.

```yaml
BAC_Shortname:
  sources:
    - source_type: trigger                       # 1º: si ya vino, gratis
      lookup_value_column: shortname
    - source_type: "as400:clientes"              # 2º: si no, se busca
      lookup_value_source: "field.BAC_Afiliado_Padre"
      lookup_key_column:   CUSAFI
      lookup_value_column: CUSSHN
```

- El resolver arma el grafo de dependencias entre campos, lo ordena
  topológicamente y resuelve en ese orden.
- **Ciclos y referencias a campos inexistentes fallan al CARGAR el
  YAML**, con el ciclo completo en el mensaje. Nunca en runtime.
- Una dependencia que no resolvió deja su dependiente sin esa fuente: la
  fuente se saltea (igual que un valor vacío) y se pasa a la siguiente de
  la cadena. No aborta.
- El orden topológico reemplaza al `cif_override` hilvanado a mano;
  `_resolve_one` recibe el dict de lo ya resuelto.

### REQ-002 — Bloque `identity:` en el YAML

Reemplaza el hardcode de `"BAC_CIF"`:

```yaml
identity:
  shortname:
    field: BAC_Shortname
    on_missing: fail            # fail | warn | default
  cif:
    field: BAC_CIF
    on_missing: fail
    max_digits: 9               # precisión REAL de CTENUM
    default_value: null
  system_id:
    field: BAC_Sistema
    on_missing: warn
```

- Los tres slots son opcionales. Slot ausente ⇒ comportamiento actual
  (se lee del trigger, sin cadena).
- `on_missing`: `fail` (default) falla el documento con un error que
  nombra el slot y **la cadena completa que se intentó**; `warn` deja el
  valor vacío y sigue; `default` usa `default_value`.
- `max_digits` (sólo `cif`, opcional): si el valor resuelto tiene más
  dígitos, se aplica `on_missing`. **El `.isdigit() else 0` desaparece**:
  un valor no numérico deja de convertirse en `0` silencioso.

### REQ-003 — Se resuelve al inicio de S2, sin etapa nueva

```
S1 INDEXAR
  ↓
S2 MAPEAR
   ├─ resolver identidad   ← NUEVO (mismo motor de field_sources)
   └─ get_mapping(system_id, id_rvi)
  ↓
S3 METADATA (reutiliza lo ya resuelto, no re-consulta)
  ↓
S4 ARMAR → S5 SUBIR
```

- `ResolvedIdentity(shortname, cif, system_id)` frozen, se cuelga del
  `StageItem` y viaja con él.
- S2 usa `identity.system_id` para la clave del mapeo cuando el slot está
  declarado; si no, sigue con `trigger_system_id(trigger)`.
- **S3 no repite trabajo**: los campos de identidad ya resueltos entran
  como semilla del dict de resolución. Un `BAC_CIF` resuelto en S2 no se
  vuelve a consultar en S3.
- S2 pasa a poder hacer red. Se documenta en
  `docs/explanation/pipeline-stages.md`; las fallas de identidad son
  `S2_FAILED` con el motivo, no un estado nuevo.

### REQ-004 — Un solo origen para las tres escrituras

`ResolvedIdentity` reemplaza a `trigger.audit_row()` en los tres
consumidores, que hoy pueden discrepar:

| Consumidor | Hoy | Con 147 |
|---|---|---|
| `MigrationRecord` (SQLite) | `audit_row()` + parche `healed_cif` | `ResolvedIdentity` |
| `CTECIF`/`CTENUM` (RVIMGLOG) | `audit_row()` crudo | `ResolvedIdentity` |
| clave del mapping (S2) | `trigger_system_id()` | `ResolvedIdentity.system_id` |

`healed_trigger` / `healed_cif` quedan deprecados (se mantienen un ciclo
por compatibilidad de hooks y tests, marcados en el docstring).

### REQ-005 — Memo por corrida

La cadena se repite idéntica para todos los documentos de un mismo
cliente. Memo en memoria por `(campo, valor_de_clave)` con vida de
corrida, contador de hits expuesto en el reporte. Ortogonal al
`prefetch` de tablas y al cache de metadata (037), que siguen igual.

### REQ-006 — El `doctor` verifica el ancho real de la columna

Check nuevo `as400_column_widths` (grupo `tracking`, SKIP sin AS400): lee
`QSYS2.SYSCOLUMNS` para la tabla configurada y compara la precisión real
de `CTENUM` contra `identity.cif.max_digits`, más el largo de `CTECIF`,
`IDNBAC`, `TIPIDN`, `OBJIDN` y `EERRMSG` contra lo que el pipeline manda.
FAIL con la definición real y el valor configurado. Esto se detecta el
lunes en el preflight, no el viernes con un `22003`.

### REQ-007 — Docs

`docs/how-to/` guía nueva con la cadena de tres saltos de punta a punta;
`docs/reference/config-schema.md` + `config-reference.yaml` (`identity:`,
`field.<NAME>`); `docs/explanation/pipeline-stages.md` (S2 hace red);
`CHANGELOG.md` `[Unreleased]`.

## Fuera de alcance

- **Etapa S1.5 propia** (estados, monitor, reintento aislado). Evaluado y
  descartado por costo: migración de `migration_log`, estados nuevos en
  toda la máquina de recovery, consola y docs. Si la resolución de
  identidad resulta cara o falla seguido, se reconsidera con su spec.
- **Colapsar errores repetidos en el reporte** (1194 líneas idénticas de
  `22003`). Es un problema real y transversal a todo el sistema, no de
  esta spec.
- **Escribir de vuelta a RVABREP** el dato enriquecido. Acá sólo se lee.
