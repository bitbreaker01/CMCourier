# 143 — Prueba de conexión: consulta configurable o derivada del sitio

## Por qué

El probe del doctor (y del botón "probar" de `[2] CREDENCIALES`) corría
`SELECT 1 FROM SYSIBM.SYSDUMMY1` contra TODA conexión `as400`. En el
iSeries del cliente corre **SafeNet/i** (exit program `SAFENET` en
`PCSECLIB`), que whitelistea objetos por perfil: el login se acepta y la
consulta a `SYSDUMMY1` se rechaza con `PWS9801 - Function rejected by
user exit program SAFENET in PCSECLIB` (verificado 2026-09-08 en la
tarjeta de la consola). Resultado: la conexión figura FAIL aunque las
credenciales y la tabla real del pipeline funcionen. Una pseudo-tabla
"canónica" no sirve cuando el permiso se da tabla por tabla: la prueba
tiene que tocar **la tabla que el pipeline va a usar de verdad** o la
que el operador diga.

## Requisitos

- **REQ-001** `As400ConnectionConfig` y `MssqlConnectionConfig` aceptan
  `probe_query: str | None` (default `None`). El editor `[2] editar`
  lo muestra como campo (deriva de `model_fields`) y `[9] YAML` lo
  acepta. Vacío = no configurado.
- **REQ-002** Sin `probe_query`, la prueba se deriva del sitio que usa
  la conexión (`ConnectionRef.probe_sql`, calculado en
  `PipelineConfig.connection_refs()`):
  - `indexing` (as400, `query`): `SELECT 1 FROM (<query>) AS T FETCH FIRST 1 ROW ONLY`
  - `metadata:<alias>` as400: `table` → `SELECT 1 FROM <table> FETCH FIRST 1 ROW ONLY`; `query` → envuelta como arriba
  - `metadata:<alias>` mssql: `table` → `SELECT TOP 1 1 FROM <table>`; `query` → `SELECT TOP 1 1 FROM (<query>) AS T`
  - `tracking.as400_sync`: `SELECT 1 FROM <library>.<table> FETCH FIRST 1 ROW ONLY`
- **REQ-003** Orden: `probe_query` explícita → derivada del sitio → sólo
  `ping()` (conexión sin sitio ni `probe_query`). `SYSIBM.SYSDUMMY1`
  desaparece del código. El mensaje del PASS/FAIL dice qué consulta se
  usó y de dónde salió (`probe_query` / `derivada de <sitio>`); sin
  consulta, el PASS dice "conectó · sin tabla asignada todavía".
- **REQ-004** `docs/reference/config-reference.yaml` documenta
  `probe_query` en ambos kinds; CHANGELOG `[Unreleased]`.
