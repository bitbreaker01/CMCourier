# How to: Armar el instalador offline para un servidor air-gapped

> [← Volver al índice](../INDEX.md) · [How-to](README.md)

> Cómo empaquetar CMCourier en un bundle de instalación **autocontenido**
> para un servidor sin acceso a internet (air-gapped) — el caso típico de
> un servidor de migración dentro de la red del banco.

---

## Cuándo necesitás esto

El servidor donde corre la migración **no tiene internet**: no puede hacer
`pip install` contra PyPI. Necesitás llevarle CMCourier + todas sus
dependencias ya descargadas, en un único `.zip`.

El flujo es de dos máquinas:

```
  Máquina de build (CON internet)          Servidor air-gapped (SIN internet)
  ───────────────────────────────          ──────────────────────────────────
  build-offline-bundle.{ps1,sh}    ──zip──►  install.{bat,sh}
  descarga wheels + arma el bundle           instala offline en un .venv
```

---

## Los dos scripts

Viven en `installer/` (no en `scripts/` — son un **entregable**, no tooling
interno):

| Script | Corré esto en… | Para un servidor destino… |
|---|---|---|
| `installer/build-offline-bundle.ps1` | Windows con internet | Windows Server x86_64 |
| `installer/build-offline-bundle.sh` | Linux con internet | Linux x86_64 |

Los dos hacen lo mismo: leen `uv.lock`, buildean el wheel del proyecto,
descargan todos los wheels de dependencias, copian config + datos de
referencia, generan el instalador (`install.bat` / `install.sh`) y
comprimen todo en un `.zip`.

---

## Prerequisitos en la máquina de build

- **Python** instalado (la misma versión major.minor que va a tener el
  servidor destino — ver el gotcha abajo).
- **`uv`** — el package manager. Instalar desde
  <https://docs.astral.sh/uv/>.
- **Internet** — para que `pip download` traiga los wheels.

---

## Armar el bundle

### Destino Windows

```powershell
.\installer\build-offline-bundle.ps1
.\installer\build-offline-bundle.ps1 -PythonVersion 3.11
.\installer\build-offline-bundle.ps1 -PythonVersion 3.12 -OutputDir C:\releases
```

### Destino Linux

```bash
bash installer/build-offline-bundle.sh
bash installer/build-offline-bundle.sh --python-version 3.11
bash installer/build-offline-bundle.sh --output-dir /releases --skip-build
```

| Flag | Default | Qué hace |
|---|---|---|
| `-PythonVersion` / `--python-version` | la del host de build | Versión Python del servidor destino. |
| `-OutputDir` / `--output-dir` | `dist-offline` | Dónde queda el `.zip`. |
| `-SkipBuild` / `--skip-build` | (off) | Reusa el wheel del proyecto si ya existe en `dist/`. |

La salida es:
`dist-offline/cmcourier-offline-<version>-py<X.Y>-<plataforma>.zip`

---

## ⚠️ El gotcha que te va a morder: la versión de Python

Los wheels de dependencias son **específicos del ABI de Python**. El bundle
trae wheels para **una** versión de Python — la que le pasaste por
`--python-version` (o la del host de build si no la pasaste).

**Esa versión tiene que coincidir EXACTO con la del servidor destino.**
Si el servidor tiene Python 3.11 y armaste el bundle con 3.12, el
`install` va a fallar — los wheels `cp312` no instalan en un intérprete
3.11.

- En **Windows** el script puede cross-descargar para otra versión
  (`pip download --platform win_amd64 --python-version ...`).
- En **Linux** el cross-download es frágil (depende de tags `manylinux`).
  Lo más confiable: armar el bundle en una máquina **gemela** del
  servidor — misma arquitectura, glibc compatible, misma versión de
  Python.

Verificá la versión del servidor **antes** de armar el bundle:

```bash
python3 --version          # en el servidor destino
```

---

## Qué hay adentro del bundle

```
cmcourier-offline-<version>-py<X.Y>-<plataforma>/
├── install.bat / install.sh      # el instalador offline
├── INSTALL.txt                   # instrucciones para el operador
├── requirements.txt              # dependencias pineadas (desde uv.lock)
├── wheels/                       # TODOS los .whl — cmcourier + dependencias
├── config/
│   ├── config-prod.yaml.template # config para copiar y editar
│   └── config-reference.yaml     # referencia anotada de TODA la config
├── reference-data/               # datos de referencia
└── README.md
```

---

## Instalar en el servidor air-gapped

1. Transferí el `.zip` al servidor (SFTP, share, USB — lo que tengas).
2. Extraelo.
3. Corré el instalador desde la carpeta extraída:
   - Windows: `install.bat`
   - Linux: `bash install.sh`
4. El instalador crea un `.venv` local e instala `cmcourier` **offline**
   (`pip install --no-index --find-links wheels`).
5. Verifica solo: corre `cmcourier --version` al final.

Después: copiá `config/config-prod.yaml.template` a `config-prod.yaml`,
editá las rutas y credenciales (la referencia completa está en
`config/config-reference.yaml`), y ya podés correr el pipeline. El
`INSTALL.txt` del bundle tiene el detalle.

---

## Actualizar una instalación existente

Re-corré `build-offline-bundle` con la versión nueva, llevá el `.zip` nuevo
al servidor, extraelo y corré el instalador de nuevo. El `.venv` existente
se reusa; pip solo actualiza los paquetes que cambiaron desde la nueva
carpeta `wheels/`.

---

## Cross-references

* Referencia de configuración: [`reference/config-reference.yaml`](../reference/config-reference.yaml).
* Driver ODBC AS400: ver [`how-to/as400-sync.md`](as400-sync.md).
* Scripts: `installer/build-offline-bundle.ps1` · `installer/build-offline-bundle.sh`.
