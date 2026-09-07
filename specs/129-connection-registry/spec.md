# 129 — Registro de conexiones con alias y credenciales por alias

## Por qué

Hoy el pipeline conoce exactamente DOS credenciales: `Secrets` tiene cuatro
campos fijos (`cmis_*`, `as400_*`, `config/loader.py`) y la conexión AS400 se
declara inline en tres lugares con tres nombres distintos
(`indexing.source.connection`, `metadata.sources[].as400_connection`,
`tracking.as400_sync.connection`). Todas comparten, por construcción, el
mismo par `AS400_USERNAME / AS400_PASSWORD`. No hay forma de declarar una
segunda base (otro AS400, un SQL Server) con sus propias credenciales, y la
consola refleja ese límite con dos tarjetas hardcodeadas.

El operador pidió (ronda de uso real de la consola, pregunta 2) poder
declarar N conexiones con alias — CSV, SQL Server, DB2/AS400 — cada una con
sus credenciales. Esta spec introduce el **registro**; la 130 agrega el
adapter MSSQL sobre él; la 131 vuelve dinámica la consola.

## Qué

**REQ-001 — Bloque `connections:` en el YAML.** `PipelineConfig` gana un
campo opcional `connections: dict[str, ConnectionConfig]` (default vacío).
`ConnectionConfig` es una unión discriminada por `kind`:

| kind | modelo | campos |
|------|--------|--------|
| `as400` | `As400ConnectionConfig` (el existente, gana `kind: Literal["as400"] = "as400"`) | `host`, `port=446`, `database="RVILIB"`, `driver="iSeries Access ODBC Driver"`, `table` |
| `mssql` | `MssqlConnectionConfig` (nuevo, `config/schema.py`) | `host`, `port=1433`, `database`, `driver="ODBC Driver 18 for SQL Server"`, `encrypt=True`, `trust_server_certificate=False` |

El alias es la clave del dict: `^[a-z][a-z0-9_]{0,31}$`. El alias `cmis`
está reservado (es el destino, con su propio bloque `cmis:`) y se rechaza.

**REQ-002 — Referencia por alias desde las tres inserciones.** Los campos
`indexing.source.connection`, `metadata.sources[].as400_connection` y
`tracking.as400_sync.connection` aceptan **o un objeto inline (como hoy) o
un `str` alias** del registro. Un validador de `PipelineConfig` verifica
que todo alias referenciado exista en `connections` y que su `kind` sea el
que espera el sitio (una fuente `as400` no puede apuntar a una conexión
`mssql`). Ningún YAML existente cambia de significado.

**REQ-003 — Alias implícito para conexiones inline.** Toda conexión inline
tiene alias implícito `as400`. `PipelineConfig.connection_refs()` devuelve
una tupla ordenada con UNA entrada por sitio que use una conexión:
`ConnectionRef(alias, kind, spec, site)` (sitio = `indexing` /
`metadata:<alias de fuente>` / `tracking.as400_sync`), resolviendo inline
y registro por igual — el `spec` ya viene resuelto, no hace falta un
segundo paso. `PipelineConfig.required_aliases()` deduplica los alias
conservando el orden, y `PipelineConfig.connection_ref(site)` devuelve la
entrada de un sitio concreto (o `None`). Es la ÚNICA fuente de verdad de
"qué conexiones necesita esta config" — el `as400_required()` de la
consola, el doctor y `sync_ops` derivan de ahí.

**REQ-004 — `Secrets` por alias.** `Secrets` pasa a ser
`Secrets(credentials: Mapping[str, Credential])` con
`Credential(username, password)` frozen. `cmis` siempre presente.
Conveniencias: `secrets.cmis` (Credential), `secrets.cmis_username` /
`secrets.cmis_password` (propiedades, para no tocar el uploader),
`secrets.get(alias) -> Credential | None` (None si falta o está vacía) y
`secrets.require(alias) -> Credential` que levanta `ConfigurationError`
con `missing_vars=[<ALIAS>_USERNAME, <ALIAS>_PASSWORD]`. Desaparecen
`as400_username` / `as400_password`.

**REQ-005 — Esquema de env vars uniforme.** La credencial del alias `X` se
lee de `X_USERNAME` / `X_PASSWORD` con el alias en MAYÚSCULAS. Esto hace
que `CMIS_USERNAME` y `AS400_USERNAME` (alias implícito `as400`) sigan
funcionando SIN cambios y que una conexión nueva `clientes_sql` lea
`CLIENTES_SQL_USERNAME` / `CLIENTES_SQL_PASSWORD`. `load_secrets(config)`
recibe la config y lee `cmis` (obligatoria, error si falta como hoy) más
cada alias de `config.connection_refs()` (opcionales: la ausencia se
detecta al construir, con `require()`). `load_secrets()` sin config sigue
válido y lee sólo `cmis` + `as400` (compat para `inspect`).

**REQ-006 — Consumidores migrados.** Todo sitio que hoy lee
`secrets.as400_*` pasa a `secrets.require(ref.alias)` con el `ref` del
sitio: `config/wiring.py` (`_build_rvabrep_source`,
`_build_metadata_sources`, `_build_idempotency_coordinator`,
`build_as400_recovery`), `cli/doctor.py` (`_check_as400_connectivity`,
`_check_as400_sync`, `_open_metadata_source`), `cli/sync_ops.py`
(`sync_unavailable_reason`, `build_as400_store`), `cli/commands/mock.py`,
`cli/commands/as400_query.py`, `cli/commands/inspect.py`. Los mensajes de
error que nombran env vars pasan a nombrar las del alias real. La rama
no-CSV de `_build_metadata_sources` deja de ser un `else` implícito: cada
`kind` tiene su `isinstance` y el resto levanta `ConfigurationError`.

**REQ-007 — Doctor por conexión.** `as400_connectivity` deja de mirar sólo
`indexing.source`: prueba CADA `ConnectionRef` de kind `as400` (una fila
de detalle por alias, FAIL si alguna falla, SKIP si no hay ninguna). La
prueba de metadata sources ya cubre cada fuente por su alias.

**REQ-008 — Documentación.** `docs/reference/config-schema.md` (sección
`connections:`, esquema de env vars; corregir la sección de Secrets que hoy
documenta `CMIS_USER/CMIS_PASS` y un `config/env.py` inexistentes),
`docs/reference/config-reference.yaml`, `README.md` (env vars).

## Escenarios

**E1 —** Un YAML con `connections: {rvi: {kind: as400, host: h}}` y
`indexing.source: {kind: as400, connection: rvi, query: ...}` valida;
`connection_refs()` devuelve un solo ref `rvi` de kind `as400` con sitio
`indexing`; `build_pipeline` con `Secrets` que trae `rvi` construye un
`As400DataSource`; sin `rvi` levanta `ConfigurationError` con
`missing_vars == ["RVI_USERNAME", "RVI_PASSWORD"]`.

**E2 —** Un alias inexistente (`connection: nadie`) o de kind incorrecto
(fuente `as400` → conexión `mssql`) falla en `load_config` con un mensaje
que nombra el alias y el sitio. Alias `cmis` o `Mal-Alias` en
`connections:` fallan la validación.

**E3 —** El YAML de siempre (conexiones inline, `AS400_USERNAME/PASSWORD`
en el entorno) carga y construye idéntico: `connection_refs()` devuelve
refs con alias `as400`; `load_secrets(config)` los lee de las env vars
históricas. Los tests existentes de wiring, doctor, sync, mock y
as400-query quedan en verde con las adaptaciones de firma.

**E4 —** `load_secrets(config)` con dos aliases (`rvi`, `clientes_sql`) lee
`RVI_*` y `CLIENTES_SQL_*`; `secrets.get("clientes_sql")` es `None` si
alguna de las dos env vars está vacía; `require()` levanta con las dos
env vars en `missing_vars`.

**E5 —** Doctor: config con dos conexiones `as400` (una en `indexing`, una
en `metadata`), la segunda con credenciales faltantes → `as400_connectivity`
es FAIL y el detalle nombra `metadata:<alias>` y sus env vars.
