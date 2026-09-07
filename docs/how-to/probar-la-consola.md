# Cómo probar la consola de operación en local

> [← Volver al índice](../INDEX.md) · [How-to](README.md)

Esta guía te lleva de cero a tener la **consola interactiva**
(`cmcourier console`) corriendo contra un Alfresco real en tu máquina,
y te dice exactamente qué apretar en cada pantalla para ejercitar todo:
credenciales, doctor, overrides, lanzar una corrida y verla en el
monitor, y operar los batches.

Todo es local y descartable. No toca producción ni el YAML committeado.

---

## 0. Pre-requisitos

- Docker + Docker Compose.
- El repo instalado en modo dev (`uv sync` ya corrido).
- Una terminal de verdad (la consola necesita TTY — no funciona por un
  pipe ni dentro de un editor sin terminal integrada).

---

## 1. Levantar el entorno local (una vez)

```bash
cd scripts/staging
bash generate-keystore.sh          # solo la primera vez
docker compose -f alfresco-compose.yml -f alfresco-compose.local.yml up -d

# Alfresco tarda varios minutos en estar sano. Esperalo:
until curl -fsS -u admin:admin \
  "http://127.0.0.1:8080/alfresco/api/-default-/public/cmis/versions/1.1/browser?cmisselector=repositoryInfo" \
  >/dev/null; do echo "esperando a Alfresco…"; sleep 10; done
echo "Alfresco arriba."

bash register-model.sh             # registra el modelo documental (re-correr tras un down -v)
```

El corpus de prueba (CSV de RVABREP + archivos sintéticos + YAML de
trabajo) ya vive en `sample/`. El YAML es **`sample/config-local.yaml`**.

> ¿Se cayó algo? Ver [`local-staging-simulation.md`](local-staging-simulation.md)
> para el detalle del entorno y los troubleshooting conocidos.

---

## 2. Abrir la consola

Las credenciales de Alfresco en el compose son `admin` / `admin`. La
consola las **pre-carga** si ya están en el entorno, así que exportalas
antes (y así también ves el flujo real de "probar conexión"):

```bash
cd /home/gmaker/projects/CMCourier
export CMIS_USERNAME=admin CMIS_PASSWORD=admin
uv run cmcourier console --config sample/config-local.yaml
```

Deberías ver la barra superior con **`STAGING`** en verde, el reloj a la
derecha, y ocho pestañas (`1`-`8` o `F1`-`F8`). Abajo, el footer con las teclas (azul =
global, gris = de la pantalla actual).

En cualquier momento: **`?`** abre la ayuda completa (incluye la leyenda
de stages S0–S7), y **`q`** sale (pide confirmación si hay una corrida).

---

## 3. El recorrido, pantalla por pantalla

### `1` INICIO
El panorama: config cargada, entorno, estado de conexiones, veredicto
del doctor, y un **"siguiente paso"** que se va tildando a medida que
avanzás (credenciales → doctor → lanzar).

### `2` CREDENCIALES
1. El usuario CMIS ya viene pre-cargado (`admin`). Escribí la
   contraseña (`admin`) en el campo. El botón **`ver`** la muestra/oculta.
2. Apretá **`↵`** (Enter) sobre el campo, o el botón **probar conexión**.
   En un segundo el chip pasa a **`ok · NN ms`**.
3. **Probá el estado de error a propósito**: borrá la contraseña y probá
   → "credencial vacía". Escribí cualquier cosa mal → verás el 401 real
   del server, con el cuerpo del error tal cual (sin romper la consola).
4. **Probá la invalidación**: con la conexión en `ok`, editá un carácter
   del campo → el chip vuelve a "sin probar" y, si ya habías corrido el
   doctor, queda marcado como desactualizado.

> Con este YAML ves **una sola tarjeta** (CMIS) y la pista "esta config usa
> solo CMIS": la fuente es CSV mirror y no hay conexiones del registro. Las
> tarjetas se derivan de las conexiones que el YAML **usa** (131) — una por
> alias, título `<alias> · <kind> · <host>` y debajo los sitios que la usan
> (`indexing`, `metadata:clientes`, `tracking`). Ver la sección 5 para verla
> con SQL Server. Sólo las tarjetas `as400` llevan el contador de intentos
> (lockout del perfil al 3°); las env vars de precarga son
> `<ALIAS>_USERNAME` / `<ALIAS>_PASSWORD` (`AS400_*` para una conexión inline).

### `4` DOCTOR
1. El selector tiene tres niveles: **`all`** (todos los checks), **un
   grupo** (probá `connections`) o **un check individual** (cada uno
   aparece con su nombre; la ayuda `?` lista todos con su grupo).
2. Apretá **`d`**. Corre en vivo (no congela la UI).
3. Navegá los checks con **`↑↓`** y expandí el detalle de cualquiera con
   **`↵`**. Los FAIL/WARN se expanden solos.
4. Con `all` deberías ver **9 pass / 0 fail** (los 2 de AS400 salen
   `salteados`). Probá un check suelto, p. ej. `log_dir_writable`: corre
   solo ese.
5. Volvé a **`2`**, cambiá la contraseña, volvé a **`4`**: el banner dice
   **"desactualizado"** — el doctor sabe que la config cambió.
6. El doctor valida la **config efectiva** (YAML + overrides guardados +
   el pipeline elegido en `5`), no el YAML crudo.

### `3` CONFIG (overrides de sesión)
1. Cambiá `cmis.workers` a `12`, o el `mode` a `batched`. El campo vacío
   = "usar el YAML".
2. **Sin apretar nada**, andá a `5` CORRER: el resumen dice que hay
   **overrides SIN GUARDAR** y usa el valor del YAML, no el tuyo.
3. Volvé a `3` y apretá **`a`** (guardar). Ahora sí el resumen los toma,
   y el doctor queda desactualizado.
4. Probá un valor inválido (`workers` = 999) + `a` → error de validación.
   Nada llega a una corrida sin pasar por esa validación.

### `5` CORRER (lanzar)
1. El **pipeline** se elige acá, no hace falta editar el YAML: el
   selector arranca en el del YAML (`csv`) y podés cambiarlo a `rvabrep`,
   `local_scan` o `single_doc`; cada uno muestra sus parámetros (ruta del
   CSV y columnas, filtros, carpeta a escanear, shortname/system/cif).
   Es un **override de sesión**: el YAML no se toca. Probá `local_scan`
   con la ruta vacía → el lanzamiento queda bloqueado con el motivo;
   volvé a `csv` y el bloqueo desaparece.
2. Poné `--total` en `25` para una corrida corta.
3. Mirá **"Config efectiva"**: la primera línea dice el pipeline y si es
   del YAML o un override; después entorno, workers, overrides,
   credenciales, doctor.
4. Apretá **`r`**. Si el doctor no está aprobado, aparece un modal
   "lanzar igual / ir a doctor" (la opción segura tiene el foco). Si el
   entorno fuera `prd`, además te haría **tipear "PRD"**.
5. La consola salta al monitor.

### `6` MONITOR
- Cabecera con **subidos / fallidos (con desglose por tipo) /
  throughput / elapsed**, el cuello de botella marcado sobre los stages,
  y debajo los paneles PREP/UPLOAD en vivo.
- **`x`** cancela con drain (los uploads en vuelo terminan, el batch
  queda reanudable) — y **te quedás en la consola** para ver el resumen.
- **`p`** pausa (con confirmación) y **`r`** reanuda (132). La pausa es
  cooperativa: lo que está en vuelo termina, ningún worker toma trabajo
  nuevo. La cabecera dice `PAUSADA` y la barra superior `⏸ pausada`.
  Ojo: `--max-duration` sigue corriendo mientras está pausada.
- Al terminar aparece la tarjeta de cierre con el resultado.

#### Pausa y re-autenticación CMIS en caliente (132)

Si la sesión CMIS expira a mitad de corrida (el server devuelve 401 dos
veces seguidas para un upload), la consola **no quema docs**: pausa la
corrida sola, muestra una notificación roja y te lleva a `[2]` con la
pista "sesión CMIS rechazada — corrida PAUSADA". El flujo:

1. En `[2]`, cargá usuario/contraseña nuevos en la tarjeta `cmis` y
   probá la conexión (el chip verde importa: si reanudás sin probar, la
   consola te pide confirmación).
2. Volvé a `[6]` y apretá **`r`**: la credencial nueva se empuja al
   uploader (sesión fría, próximo POST re-hace el warmup) y la compuerta
   se abre. El doc que recibió el 401 se reintenta **él mismo**, no se
   marca fallido.
3. Si la credencial nueva también es rechazada, la corrida vuelve a
   pausarse (segundo episodio). Al tercer 401 consecutivo del mismo doc,
   ése se marca `S5_FAILED` y aparece en `[7]` para retry.

Para probarlo en local con Alfresco: lanzá una corrida larga (`total`
alto), cambiá la contraseña del usuario en Alfresco (o revocá la sesión
desde el admin) y mirá cómo `[6]` pasa a `PAUSADA · esperando
credenciales CMIS`. Sin consola (CLI headless, TUI clásica) el 401 sigue
fallando el doc como siempre.

### `7` BATCHES
- Tabla de todos los batches con su **auditoría**: quién lo lanzó, el
  entorno, subidos/fallidos, outcome.
- **`↑↓`** movés la fila, **`↵`** abre el detalle (incluye los
  documentos fallidos), **`R`** reintenta los fallidos (te lleva al
  launcher en modo reanudar), **`E`** te dice cómo exportar el reporte.
- Corré una segunda vez con `--total 200` para generar uploads nuevos y
  ver la reconciliación.

### `8` SYNC (SQLite ↔ AS400 NIARVILOG)
Es la versión interactiva de `cmcourier sync status | recover | resolve`.
- **En el entorno local aparece deshabilitada**, con el motivo en claro:
  el YAML tiene `tracking.as400_sync.enabled: false`. Es lo esperado —
  acá no hay AS400. Si el YAML lo habilita pero faltan las credenciales
  AS400, te manda a `2`.
- Con un AS400 real: **`s`** (o el botón) corre el estado — limpia los
  `in_progress` vencidos y prueba la conectividad. **Recuperar**:
  siempre `simular` primero (dry-run); `aplicar` recién se habilita si
  la simulación del **mismo batch_id** encontró filas, y pide confirmación
  (en `prd`, tipear `PRD`). **Resolver**: un `txn`, la preferencia
  (`as400 manda` es read-only; `local manda` escribe en AS400 y requiere
  `cm_object_id` + confirmación). Todo corre en background y el resultado
  se acumula en el panel de abajo.

---

## 4. Verificar por fuera (opcional)

Que lo que ves en la consola sea real, contado contra Alfresco:

```bash
# documentos por carpeta destino (las queries CMIS-QL van por Solr y
# tardan en indexar; contá por children, no por SELECT):
python3 - <<'PY'
import json, urllib.request, base64
auth = base64.b64encode(b"admin:admin").decode()
base = "http://127.0.0.1:8080/alfresco/api/-default-/public/cmis/versions/1.1/browser/root/cmcourier-staging"
req = urllib.request.Request(base + "?cmisselector=children&maxItems=100",
                             headers={"Authorization": "Basic " + auth})
folders = [o["object"]["properties"]["cmis:name"]["value"]
           for o in json.load(urllib.request.urlopen(req))["objects"]]
total = 0
for f in folders:
    r = urllib.request.Request(f"{base}/{f}?cmisselector=children&maxItems=1",
                               headers={"Authorization": "Basic " + auth})
    total += json.load(urllib.request.urlopen(r))["numItems"]
print(f"documentos reales en Alfresco: {total}")
PY

# y el tracking:
python3 -c "import sqlite3; print('S5_DONE:', sqlite3.connect('sample/local-tracking.db').execute(\"SELECT COUNT(DISTINCT rvabrep_txn_num) FROM migration_log WHERE status='S5_DONE'\").fetchone()[0])"
```

Los dos números deben coincidir.

---

## 5. SQL Server local como fuente de metadata (130)

Mismo banco, pero `BAC_Nombre_Cliente` sale de una tabla SQL Server en vez
del CSV. Sirve para probar el registro de conexiones (129), el adapter
`mssql` (130) y las tarjetas de credenciales por alias de la consola.

```bash
# 1) driver ODBC de Microsoft en el host (una vez; requiere el repo de MS):
sudo ACCEPT_EULA=Y apt-get install -y msodbcsql18
odbcinst -q -d            # debe listar [ODBC Driver 18 for SQL Server]

# 2) SQL Server 2022 en Docker + tabla dbo.clientes desde sample/clients.csv
cd scripts/staging
docker compose -f mssql-compose.yml up -d
bash mssql-seed.sh        # idempotente: recrea la tabla (1406 filas)
cd ../..

# 3) credenciales: el alias de conexión es `clientes_sql`, así que las
#    env vars son CLIENTES_SQL_USERNAME / CLIENTES_SQL_PASSWORD
export CMIS_USERNAME=admin CMIS_PASSWORD=admin
export CLIENTES_SQL_USERNAME=sa CLIENTES_SQL_PASSWORD='CmCourier!2026'

# 4) doctor: el check nuevo y la fuente concreta
uv run cmcourier doctor --config sample/config-local-mssql.yaml --check mssql_connectivity
uv run cmcourier doctor --config sample/config-local-mssql.yaml --check metadata_sources

# 5) la consola con este YAML — en [2] CREDENCIALES aparece la tarjeta
#    `clientes_sql · mssql · 127.0.0.1` (usada por: metadata:clientes) además
#    de CMIS, pre-cargada desde CLIENTES_SQL_* si las exportaste
uv run cmcourier console --config sample/config-local-mssql.yaml
```

En la consola:

- **INICIO** lista `cmis` y `clientes_sql` con su estado; el primer paso
  no se tilda hasta que **ambas** estén probadas (y `[5]` dice
  `credenciales ✘` hasta entonces).
- En `[2]`, probá `clientes_sql` con una clave mala: el chip queda en
  `falló` con el error real del driver y **sin** contador de intentos (eso
  es sólo AS400). Corregila y probá de nuevo → `ok · NN ms`.
- La barra superior muestra un ✔/✘ por conexión (`cmis ✔ · clientes_sql ✔`).

Qué mirar:

- `mssql_connectivity` en **PASS** con `clientes_sql@127.0.0.1` en el
  mensaje; sin las env vars da **FAIL** nombrando `CLIENTES_SQL_USERNAME`.
- `metadata_sources` en **PASS** con `clientes` (la fuente) — el doctor
  cuenta filas contra `dbo.clientes`.
- Una corrida desde `[5]` produce los mismos `BAC_Nombre_Cliente` que con
  `config-local.yaml` (los datos son los mismos, sólo cambia la fuente).
- El test de integración real: `CMCOURIER_MSSQL_LIVE=1 uv run pytest
  tests/integration/adapters/test_mssql_live.py -q` (se salta sin la env var).

Este YAML usa su propia SQLite (`sample/local-mssql-tracking.db`) para no
mezclar corridas con el banco CSV. Bajar el SQL Server:
`docker compose -f scripts/staging/mssql-compose.yml down` (`-v` borra los datos;
re-correr `mssql-seed.sh` después).

---

## 6. Resetear entre pruebas

```bash
bash scripts/staging/wipe-alfresco-docs.sh
TRACKING_DB=sample/local-tracking.db bash scripts/staging/wipe-local-state.sh
```

## 7. Bajar el entorno

```bash
cd scripts/staging
docker compose -f alfresco-compose.yml -f alfresco-compose.local.yml down      # conserva datos
docker compose -f alfresco-compose.yml -f alfresco-compose.local.yml down -v   # wipe total (re-registrar modelo)
```

---

## Qué NO hace todavía (por diseño de la v1)

- **Techo manual de workers en caliente** desde el monitor: pendiente
  (requiere que el AIMD respete un `min(user_cap, aimd_cap)`).
- **ETA por ventana** en el monitor: usa el throughput acumulado del
  provider actual.
- **Editar y guardar el YAML completo** desde la consola: próxima
  iteración; hoy los overrides son de sesión y las fuentes (y sus
  conexiones) son las que declara el YAML.

El resto del mock v2 (ver el artifact de diseño) está implementado.
