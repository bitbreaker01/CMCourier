#!/usr/bin/env bash
# Siembra el SQL Server local (mssql-compose.yml) con la DB `cmcourier` y la
# tabla dbo.clientes (CIF, Nombre_Cliente, Short_Name) cargada desde
# sample/clients.csv — el MISMO dataset que usa la fuente `csv:clients` del
# config local, así el pipeline resuelve BAC_Nombre_Cliente igual por
# cualquiera de las dos fuentes y la comparación es 1:1.
#
# Idempotente: crea la DB si no existe y recarga la tabla completa.
# No necesita driver ODBC en el host — corre sqlcmd ADENTRO del contenedor.
set -euo pipefail

CONTAINER="${MSSQL_CONTAINER:-cmcourier-staging-mssql}"
SA_PASSWORD="${MSSQL_SA_PASSWORD:-CmCourier!2026}"
CSV="${CLIENTS_CSV:-$(dirname "$0")/../../sample/clients.csv}"
DB="cmcourier"

if [[ ! -f "$CSV" ]]; then
  echo "no encuentro $CSV (generá el corpus local primero)" >&2
  exit 1
fi

sqlcmd() {
  docker exec -i "$CONTAINER" /opt/mssql-tools18/bin/sqlcmd -C -S localhost -U sa -P "$SA_PASSWORD" -b "$@"
}

echo "esperando a SQL Server en $CONTAINER..."
for _ in $(seq 1 30); do
  if sqlcmd -Q "SELECT 1" -o /dev/null 2>/dev/null; then break; fi
  sleep 2
done

sqlcmd -Q "IF DB_ID('$DB') IS NULL CREATE DATABASE [$DB];"
sqlcmd -d "$DB" -Q "
IF OBJECT_ID('dbo.clientes') IS NOT NULL DROP TABLE dbo.clientes;
CREATE TABLE dbo.clientes (
  CIF            VARCHAR(20)  NOT NULL PRIMARY KEY,
  Nombre_Cliente NVARCHAR(200) NOT NULL,
  Short_Name     NVARCHAR(100) NOT NULL
);"

# CSV → INSERTs por lotes de 500 (el límite de VALUES por INSERT es 1000).
python3 - "$CSV" <<'PY' | sqlcmd -d "$DB" -i /dev/stdin
import csv, sys
rows = list(csv.DictReader(open(sys.argv[1], newline="", encoding="utf-8")))
q = lambda s: "'" + s.replace("'", "''") + "'"
for i in range(0, len(rows), 500):
    chunk = rows[i:i + 500]
    values = ",\n".join(
        f"({q(r['CIF'])}, N{q(r['Nombre_Cliente'])}, N{q(r['Short_Name'])})" for r in chunk
    )
    print(f"INSERT INTO dbo.clientes (CIF, Nombre_Cliente, Short_Name) VALUES\n{values};\nGO")
PY

sqlcmd -d "$DB" -Q "SELECT COUNT(*) AS clientes FROM dbo.clientes;"
echo "listo: $DB.dbo.clientes sembrada desde $CSV"
