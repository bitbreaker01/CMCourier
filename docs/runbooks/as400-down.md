> [← Volver al índice](../INDEX.md) · [Runbooks](README.md)

# Runbook: AS400 inalcanzable

> **Severidad**: P1 · **Tiempo estimado**: 20 min · **Última revisión**: 2026-05-15

## Síntoma

Tres formas en que se manifiesta:

- **En arranque**: `cmcourier doctor --check connections` falla en `as400_connectivity` (o en `as400_sync` si tenés tracking distribuido activado).
- **En S0/S1 (trigger acquisition / indexing)**: `TriggerError` o `IndexingError` envueltos en un `pyodbc.OperationalError` cuyo mensaje suele decir `Communication link failure`, `SQL30081N`, `Connection refused` o tira un timeout sin mensaje claro.
- **Sin error explícito**: las queries simplemente cuelgan. El TUI tab `PREP` se congela en `S0` o `S1`, sin `failed` ni `done` moviéndose. AS400 está respondiendo TCP pero el ODBC se traba (típico de saturación del subsystem).

## Diagnóstico rápido

```bash
# 1. ¿AS400 acepta TCP? (driver ODBC suele esconder esto)
nc -zv <as400-host> <as400-port>
# o ping si tu red lo permite
ping -c 3 <as400-host>

# 2. ¿La auth funciona? Query mínima que no toca tablas reales:
cmcourier as400-query \
    --config sample/config.yaml \
    "SELECT 1 FROM RVILIB.RVABREP FETCH FIRST 1 ROW ONLY"   # una tabla a la que el perfil tenga permiso (143: SafeNet/i rechaza SYSDUMMY1)
# Esto exige AS400_USERNAME y AS400_PASSWORD en el environment.
# Si dice "ConfigurationError: AS400_USERNAME and AS400_PASSWORD must be set"
# → no es el server, son las credenciales.

# 3. Validación canónica
cmcourier doctor --config sample/config.yaml --check connections

# 4. ¿Está el driver ODBC?
odbcinst -q -d
# Tenés que ver el driver que figura en `indexing.source.connection.driver`
# (típicamente "IBM i Access ODBC Driver" o similar).
```

## Mitigación inmediata

1. **`Ctrl+C` en cualquier corrida activa**. No tiene sentido seguir golpeando un AS400 caído — el TUI se queda colgado y el writer thread acumula nada.
2. **Si tenés CSVs cacheados de la NIARVILOG / RVABREP**: switcheá temporalmente la fuente a CSV. En el YAML:
   ```yaml
   indexing:
     source:
       kind: "csv"
       csv_path: "sample/cache/niarvilog-latest.csv"
   ```
   Cuidado con la frescura: si el CSV es viejo, vas a perder triggers nuevos. Esto te desbloquea para cerrar batches en vuelo, no para abrir batches productivos a ciegas.
3. **Si tu trigger source es CSV pero la pipeline igual habla con AS400** (porque `metadata.sources[*].kind == "as400"`): no podés switchear tan fácil. Desactivá esas fuentes temporalmente comentando los `field_sources` que dependen de `as400:*` — pero **solo** si los campos que dependen tienen `default_value`. Sin default, S3 va a tirar `SourceFailedError` para cada doc. El default no se valida contra nada (149), así que un marcador tipo `"000000"` sirve para desbloquear la corrida: `types check` lo informa con un INFO y después lo buscás en CM para corregirlo.

## Resolución

Atacá según lo que mostró el diagnóstico:

1. **Servidor caído / inalcanzable** (paso 1 falla):
   - Contactá al admin AS400. Pedile ventana estimada de recuperación.
   - Si hay VPN/firewall en el medio, validá que la sesión no se cortó: `ip route get <as400-host>` y revisá tablas de NAT/ACL.
2. **Credenciales rechazadas** (paso 2 devuelve auth error):
   - `AS400_USERNAME` / `AS400_PASSWORD` pueden estar expiradas. AS400 fuerza rotación periódica.
   - Re-exportá las nuevas credenciales o actualizá tu secret manager. Validá con `as400-query` antes de re-correr la pipeline.
3. **Driver ausente o roto** (paso 4 no muestra el driver esperado):
   - Reinstalá el ODBC driver. En Debian/Ubuntu suele ser un `.deb` provisto por IBM.
   - Después de instalar, verificá `/etc/odbcinst.ini` y que el nombre del driver matchee exactamente el de `indexing.source.connection.driver` (case-sensitive).
4. **Queries colgadas sin error** (síntoma del cuelgue silencioso):
   - El subsystem QZDASOINIT del AS400 puede estar saturado. Admin AS400 lo confirma con `WRKACTJOB SBS(QUSRWRK)`.
   - Pedile que reinicie el subsystem o que aumente la cantidad de jobs prestart.
   - Una vez liberado, el `pyodbc` desde tu lado se recupera solo al siguiente intento — no hay state local que limpiar.

## Verificación

```bash
# 1. Doctor pasa
cmcourier doctor --config sample/config.yaml --check connections
# Esperás: as400_connectivity PASS, as400_sync PASS (si está enabled)

# 2. Query simple devuelve
cmcourier as400-query \
    --config sample/config.yaml \
    "SELECT 1 FROM RVILIB.RVABREP FETCH FIRST 1 ROW ONLY"   # una tabla a la que el perfil tenga permiso (143: SafeNet/i rechaza SYSDUMMY1)

# 3. Si el batch estaba a medias, reanudá
cmcourier csv-trigger-pipeline run \
    --config sample/config.yaml \
    --batch-id <batch-id> \
    --resume
```

Si reanudaste y el batch arrancó pero S1 vuelve a tirar `TriggerError`, AS400 **no** está realmente recuperado — el admin te mintió o tu sesión sigue con state viejo. Volvé al paso 1 del diagnóstico.

## Post-mortem

- Anotá la duración del outage y la causa raíz reportada por el admin AS400.
- Si fue saturación del subsystem, considerá bajar la frecuencia con que CMCourier consulta. La query batched es por trigger (S1), no hay mucho margen — pero podés bajar `batches_in_flight` a 1 para reducir concurrencia.
- Si fue expiración de credenciales, agendá rotación proactiva antes de que vuelva a pasar.
- Si el tracking distribuido AS400 (`tracking.as400_sync.enabled=true`) está prendido, revisá `tracking.as400_sync.retry_attempts` y `retry_base_delay_s` — durante el outage cada write a NIARVILOG reintenta hasta `retry_attempts` veces. Multiplicá eso por la cantidad de docs en vuelo y vas a entender por qué el shutdown fue lento.

## Ver también

- [`how-to/as400-sync.md`](../how-to/as400-sync.md) — config del tracking distribuido.
- [`reference/`](../reference/) — schema completo de `As400ConnectionConfig` y `As400SyncConfig`.
- [`explanation/architecture-overview.md`](../explanation/architecture-overview.md) — por qué AS400 está en dos lugares de la pipeline (S0/S1 y tracking).
