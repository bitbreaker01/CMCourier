# 130 — Fuente de metadata SQL Server (`kind: mssql`)

## Por qué

El operador tiene metadatos en SQL Server además del CSV y de DB2/AS400.
Hoy sólo existen dos `kind` de fuente de metadata (`csv`, `as400`) y el
único adapter de base de datos es `As400DataSource`. Con el registro de
conexiones (129) ya hay dónde declarar un SQL Server; falta el adapter que
lo hable y el `kind` que lo enganche.

`As400DataSource` es ODBC genérico salvo por la connection string
(`SYSTEM=` es de IBM i), el nombre del evento de log y el sentinel
`pyodbc` del módulo que parchean los tests. Duplicarlo sería copiar 200
líneas de contrato `IDataSource`, pool thread-local (106) y normalización
de filas (074) — y arreglar bugs dos veces. Se extrae una base.

## Qué

**REQ-001 — Base ODBC compartida.** Nuevo
`adapters/sources/odbc_base.py` con `OdbcDataSource(IDataSource)`:
implementa `query`, `query_stream`, `get_by_fields`, `get_by_fields_in`
(chunks de 1000), `get_all`, `count`, `close`, el
`ThreadLocalConnectionPool` y `_normalize_row`. Deja abstractos
`_build_connection_string()`, `_driver_module()` (devuelve el módulo
`pyodbc` del subclase — cada módulo concreto conserva su sentinel global
`pyodbc` y su `_import_pyodbc()` para que `monkeypatch.setattr(modulo,
"pyodbc", fake)` siga funcionando) y el atributo `LABEL` (`"as400"` /
`"mssql"`) que da el `kind` del evento `cmcourier.metrics.network`
(`as400_query` / `mssql_query`) y el prefijo de los mensajes de error.
`As400DataSource` pasa a heredar de ahí sin cambiar su API pública ni el
comportamiento observable; `_normalize_row` sigue importable desde
`adapters/sources/as400.py`.

**REQ-002 — `MssqlDataSource`.** `adapters/sources/mssql.py`, misma firma
keyword-only que AS400 más `encrypt: bool` y
`trust_server_certificate: bool`. Connection string:
`DRIVER={<driver>};SERVER=<host>,<port>;DATABASE=<db>;UID=..;PWD=..;Encrypt=yes|no;TrustServerCertificate=yes|no;`.
Errores `pyodbc.Error` → `IndexingError("MSSQL query failed" / "MSSQL
connection failed")` con `sqlstate` extraído igual que AS400. `table`
acepta identificadores con esquema (`dbo.clientes`) y `query` se envuelve
como `(query) AS T` (T-SQL lo admite).

**REQ-003 — `kind: mssql` en fuentes de metadata.** Nuevo
`MssqlMetadataSourceConfig(kind="mssql", alias, connection: str,
table | query)` en la unión `MetadataSourceConfig`. La conexión es
SIEMPRE por alias del registro (no hay forma inline: es nuevo, no hay
compat que respetar). `FieldSourceItem.source_type` acepta el prefijo
`mssql:<alias>`; `services/metadata.py` lo resuelve con el mismo camino de
lookup/prefetch que `csv:` y `as400:` (un helper único
`split_lookup_source_type()` reemplaza los `startswith` duplicados).
`config/wiring.py` y `cli/doctor.py:_open_metadata_source` construyen
`MssqlDataSource` con `secrets.require(alias)`.

**REQ-004 — Doctor.** Nuevo check `mssql_connectivity` (grupo
`connections`, después de `as400_connectivity` en `CHECK_NAMES`): por cada
`ConnectionRef` de kind `mssql`, credenciales presentes + `SELECT 1`. SKIP
si la config no declara ninguna. `metadata_sources` ya prueba la fuente
concreta (tabla/query).

**REQ-005 — Banco local.** `scripts/staging/mssql-compose.yml` (SQL Server
2022 en `127.0.0.1:1433`) y `scripts/staging/mssql-seed.sh` (crea
`cmcourier.dbo.clientes` desde `sample/clients.csv`, sin driver en el
host). `sample/config-local-mssql.yaml`: el config local con
`BAC_Nombre_Cliente` resuelto por `mssql:clientes` (conexión
`clientes_sql`) en lugar de `csv:clients`. Guía `docs/how-to/probar-la-consola.md` → sección "SQL
Server local" (instalación de `msodbcsql18`, env vars
`CLIENTES_SQL_*`, doctor, corrida). Test de integración REAL
`tests/integration/adapters/test_mssql_live.py` marcado
`@pytest.mark.mssql_live`, que se salta si `CMCOURIER_MSSQL_LIVE` no está
seteado.

**REQ-006 — Documentación.** `docs/reference/config-schema.md` (fuente
`mssql`, prefijo `mssql:`), `config-reference.yaml`, `docs/reference/cli.md`
(nuevo check), `README.md` (SQL Server como fuente de metadata).

## Escenarios

**E1 —** `MssqlDataSource(host="h", port=1433, database="d", driver="ODBC
Driver 18 for SQL Server", username="u", password="p", encrypt=True,
trust_server_certificate=True, table="dbo.clientes")._build_connection_string()`
es exactamente `DRIVER={ODBC Driver 18 for SQL Server};SERVER=h,1433;DATABASE=d;UID=u;PWD=p;Encrypt=yes;TrustServerCertificate=yes;`.
`table` + `query` juntos → `ConfigurationError`.

**E2 —** Con el `pyodbc` falso de `tests/integration/adapters/test_as400.py`
(parcheado en `adapters.sources.mssql`), `get_by_fields({"CIF": "1"})`
ejecuta `SELECT * FROM dbo.clientes WHERE CIF = ?` con `["1"]`;
`get_by_fields_in` con 1500 valores hace dos queries; un `pyodbc.Error`
con SQLSTATE `28000` se convierte en `IndexingError` con `sqlstate ==
"28000"`; el log de red emite `kind == "mssql_query"`.

**E3 —** Los tests existentes del adapter AS400 (`test_as400*.py`,
`test_as400_normalize.py`, pool) quedan en verde sin modificarse tras la
extracción de la base.

**E4 —** YAML con `connections: {clientes_sql: {kind: mssql, host: h,
database: cmcourier}}`, fuente `{kind: mssql, alias: clientes,
connection: clientes_sql, table: dbo.clientes}` y `source_type:
"mssql:clientes"` carga; `build_pipeline` con `CLIENTES_SQL_*` construye
un `MssqlDataSource` en el registro bajo `clientes`; sin credenciales
levanta `ConfigurationError` con `missing_vars == ["CLIENTES_SQL_USERNAME",
"CLIENTES_SQL_PASSWORD"]`; `source_type: "mssql:nadie"` falla en el
servicio de metadata con "unknown mssql alias"; una fuente `mssql`
apuntando a una conexión `as400` falla en `load_config`.

**E5 —** Doctor: config sin conexiones `mssql` → `mssql_connectivity` SKIP;
con una y credenciales faltantes → FAIL nombrando las env vars; con una y
`pyodbc` falso que responde → PASS. `CHECK_NAMES` incluye
`mssql_connectivity` en el grupo `connections`.

**E6 (live, opcional) —** Con `CMCOURIER_MSSQL_LIVE=1` y el contenedor
sembrado: `count() == 1406`, `get_by_fields({"CIF": "396302"})` devuelve
`Nombre_Cliente == "MARIA13"`, y `cmcourier doctor --config
sample/config-local-mssql.yaml --check metadata_sources` da PASS.

**E7 (hallazgo durante la implementación) —** El CLI carga los secrets CON
la config: `cmcourier doctor --check mssql_connectivity` con
`CLIENTES_SQL_USERNAME` / `CLIENTES_SQL_PASSWORD` exportadas ejecuta la
probe (no reporta "credentials missing"). Antes `app.py` y `sync.py`
llamaban `load_secrets()` sin config, así que ningún alias del registro
(129) recibía sus env vars fuera de los tests.

## Notas de implementación

- Bug de 129 arreglado acá (E7): `load_secrets(config)` en `app.py`
  (single-doc, doctor, run) y `commands/sync.py`.
- `docs/reference/cli.md` documenta ahora la tabla de checks individuales
  (126 había habilitado `--check <nombre>` sin documentarlo).
- El test live (E6) quedó escrito pero NO ejecutado: el host no tiene
  `msodbcsql18`. Corre con `CMCOURIER_MSSQL_LIVE=1` una vez instalado.

## Hallazgos del antagonista (129–131) aplicados

- **I4** El test live (E6) llamaba `get_by_fields_in(field, values)` sin el
  `fixed_filters` posicional y cargaba `sample/config-local-mssql.yaml`,
  que está gitignoreado: en otro clon fallaba antes de tocar SQL Server.
  Ahora pasa `{}` y genera la config en `tmp_path` sobre los fixtures del
  repo (`connections.clientes_sql` + fuente `mssql:clientes`).
- **M2** `table` de las fuentes `as400` y `mssql` se interpola crudo en el
  `SELECT ... FROM` del prefetch: ahora se valida como 1–3 identificadores
  separados por punto (`_validate_qualified_table`, misma regla de 049).
- **M3** `source_type: "<kind>:<alias>"` con un `kind` distinto al de la
  fuente declarada en `metadata.sources` (p. ej. `mssql:` sobre un CSV)
  se rechaza al cargar (`MetadataConfigModel`); un alias no declarado
  sigue siendo asunto del resolver (084).
