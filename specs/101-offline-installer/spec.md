# 101 — Instalador offline para servidores air-gapped

## Por qué

El servidor donde corre la migración del banco **no tiene internet** —
no puede hacer `pip install` contra PyPI. CMCourier + todas sus
dependencias tienen que llegar al servidor ya descargadas, en un único
`.zip` autocontenido.

El script de PowerShell que arma ese bundle (`build-offline-bundle.ps1`)
existía solo en la máquina del operador — **nunca estuvo en el repo**.
Eso es un riesgo: la capacidad de desplegar el producto vivía fuera del
control de versión.

## Qué

### Scripts (`installer/`)

Los dos scripts viven en `installer/` — **no** en `scripts/`. Razón:
`scripts/` es tooling interno (excluido del export); el instalador
offline es un **entregable** que el destinatario necesita, así que va
en su propio directorio incluido en el export.

* **`installer/build-offline-bundle.ps1`** — corre en Windows con
  internet; arma un bundle para un Windows Server x86_64 air-gapped.
* **`installer/build-offline-bundle.sh`** — versión Linux; corre en
  Linux con internet; arma un bundle para un Linux x86_64 air-gapped.

Ambos: leen `uv.lock`, buildean el wheel del proyecto (`uv build`),
descargan todos los wheels de dependencias (`pip download`), copian
config + datos de referencia, generan el instalador
(`install.bat` / `install.sh`) + `INSTALL.txt`, y comprimen a `.zip`.

El bundle incluye `docs/reference/config-reference.yaml` para que el
operador tenga la superficie de config completa aunque `sample/` no
exista (es gitignored).

### Documentación

* `docs/how-to/build-offline-installer.md` — how-to: el flujo de dos
  máquinas, prerequisitos, el gotcha de la versión de Python, qué hay
  en el bundle, cómo instala el operador.

### Empaquetado de export

* El ZIP de `scripts/export-bundle.sh` ahora va a `releases/` con
  nombre versionado (`cmcourier-export-<version>.zip`) — directorio
  convencional para entregables, commiteado.
* `releases/` se agrega a `.exportignore` (un export nuevo no anida
  exports viejos).
* `.gitignore`: `dist-offline/` (output del instalador offline —
  wheels, binario, grande) se ignora.
* `installer/` **no** está en `.exportignore` — el instalador SÍ va al
  export.

## Criterios de aceptación

1. `installer/build-offline-bundle.{ps1,sh}` están en el repo.
2. La versión Linux pasa `bash -n` (syntax check).
3. `installer/` entra al ZIP de export; `scripts/` no.
4. El how-to documenta el flujo completo.

## Riesgos / notas

* **Versión de Python**: los wheels son ABI-específicos. El bundle
  sirve para una sola versión de Python — la del servidor destino.
  Cross-download es confiable en Windows, frágil en Linux (tags
  `manylinux`). Documentado fuerte en el how-to.
* **Binarios en git**: el `releases/*.zip` se commitea por pedido del
  operador. Tener presente que cada export agrega un blob binario al
  historial — a futuro conviene GitHub Releases en vez del repo.
* El `.ps1` es el script provisto por el operador, con las rutas de
  ejemplo ajustadas de `scripts\` a `installer\` y el agregado del
  `config-reference.yaml` al bundle.
