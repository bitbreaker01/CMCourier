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
derecha, y siete pestañas. Abajo, el footer con las teclas (azul =
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

> AS400 acá aparece como **no requerida** (la fuente de este YAML es CSV
> mirror). La consola lo deriva del YAML, no del tipo de pipeline.

### `4` DOCTOR
1. Elegí un grupo en el selector (probá `connections`) o dejá `all`.
2. Apretá **`d`**. Corre en vivo (no congela la UI).
3. Navegá los checks con **`↑↓`** y expandí el detalle de cualquiera con
   **`↵`**. Los FAIL/WARN se expanden solos.
4. Deberías ver **9 pass / 0 fail** (los 2 de AS400 salen `salteados`).
5. Volvé a **`2`**, cambiá la contraseña, volvé a **`4`**: el banner dice
   **"desactualizado"** — el doctor sabe que la config cambió.

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
1. El pipeline es el del YAML (read-only). Poné `--total` en `25` para
   una corrida corta.
2. Mirá **"Config efectiva"**: entorno, workers, overrides, credenciales,
   doctor. El **comando equivalente** de abajo refleja exactamente eso.
3. Apretá **`r`**. Si el doctor no está aprobado, aparece un modal
   "lanzar igual / ir a doctor" (la opción segura tiene el foco). Si el
   entorno fuera `prd`, además te haría **tipear "PRD"**.
4. La consola salta al monitor.

### `6` MONITOR
- Cabecera con **subidos / fallidos (con desglose por tipo) /
  throughput / elapsed**, el cuello de botella marcado sobre los stages,
  y debajo los paneles PREP/UPLOAD en vivo.
- **`x`** cancela con drain (los uploads en vuelo terminan, el batch
  queda reanudable) — y **te quedás en la consola** para ver el resumen.
- Al terminar aparece la tarjeta de cierre con el resultado.

### `7` BATCHES
- Tabla de todos los batches con su **auditoría**: quién lo lanzó, el
  entorno, subidos/fallidos, outcome.
- **`↑↓`** movés la fila, **`↵`** abre el detalle (incluye los
  documentos fallidos), **`R`** reintenta los fallidos (te lleva al
  launcher en modo reanudar), **`E`** te dice cómo exportar el reporte.
- Corré una segunda vez con `--total 200` para generar uploads nuevos y
  ver la reconciliación.

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

## 5. Resetear entre pruebas

```bash
bash scripts/staging/wipe-alfresco-docs.sh
TRACKING_DB=sample/local-tracking.db bash scripts/staging/wipe-local-state.sh
```

## 6. Bajar el entorno

```bash
cd scripts/staging
docker compose -f alfresco-compose.yml -f alfresco-compose.local.yml down      # conserva datos
docker compose -f alfresco-compose.yml -f alfresco-compose.local.yml down -v   # wipe total (re-registrar modelo)
```

---

## Qué NO hace todavía (por diseño de la v1)

- **Pausa / re-autenticación en caliente**: si la sesión CMIS expira a
  mitad de corrida, la salida es cancelar con `x` (drain) y reanudar el
  batch — la consola te lo dice. (Requiere que el `CancellationToken`
  deje de ser one-way; queda para una iteración futura.)
- **Techo manual de workers en caliente** desde el monitor: pendiente
  (requiere que el AIMD respete un `min(user_cap, aimd_cap)`).
- **ETA por ventana** en el monitor: usa el throughput acumulado del
  provider actual.

El resto del mock v2 (ver el artifact de diseño) está implementado.
