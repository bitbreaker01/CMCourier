# 141 — `[0] PRUEBA`: subir UN documento sintético a un código de Content Manager

## Por qué

Antes de disparar una corrida de miles de documentos, el operador quiere
hacer UN tiro de prueba contra un código de Content Manager: confirmar
que el código está mapeado, que los metadatos que exige son los que él
cree, que el tipo y la carpeta existen en el CM, y — sobre todo — ver
la respuesta CRUDA del servidor cuando algo falla. Hoy no hay forma de
subir un documento suelto con metadatos tipeados a mano: `single-doc run`
corre el pipeline entero (RVABREP + fuentes + archivo real) y el adapter
descarta la `httpx.Response` (`cmis_uploader.py:566`), truncando el body
de error a 1024 caracteres. El operador termina probando con `curl` y
copiando propiedades a mano.

## Qué

**REQ-001 — Mapping indexado por código CM.** `MappingService` gana
`get_by_cm_code(id_cm: str) -> tuple[CMMapping, ...]` (comparación
exacta tras `strip()`; vacío si no hay filas) y `cm_codes() ->
tuple[str, ...]` (ordenados, sin repetidos) para sugerir. Varias filas
`IDRVI` pueden apuntar al mismo `IDCM` con distinto `CMISType` /
`CMISFolder`: se devuelven todas, en orden de aparición. El índice se
construye en la misma pasada que el de `IDRVI`; no cambia el modo
consolidado ni el split ni el descarte de filas con `Requerido` falso
(la pantalla usa SÓLO los requeridos — decisión del operador).

**REQ-002 — Archivo sintético multi-formato.**
`services/mock/synthetic_file.py` (puro, sin I/O):
`SyntheticFormat = Literal["pdf", "tiff", "jpeg", "png"]`,
`SyntheticFile(content: bytes, mime_type: str, extension: str)` y
`build_synthetic_file(fmt, size_bytes, marker) -> SyntheticFile`.
`pdf` reusa `build_synthetic_pdf` (tamaño exacto). `tiff`/`jpeg`/`png`
se generan con Pillow (dependencia ya presente): una imagen con el
`marker` dibujado y ruido determinístico (semilla = `marker`) hasta
aproximar `size_bytes` dentro de ±15 % — si el formato no llega
(JPEG muy chico), se devuelve el mínimo alcanzable y `SyntheticFile`
lo dice en `size_note: str` (`""` si cumplió). Mimes:
`application/pdf`, `image/tiff`, `image/jpeg`, `image/png`; extensiones
`.pdf .tif .jpg .png`. `size_bytes` entre 1 KB y 50 MB; fuera de rango
`ValueError`.

**REQ-003 — Upload con respuesta cruda y borrado.** Nuevo Protocol
`services/practice_upload.py::PracticeUploadPort` con
`upload_raw(file: StagedFile, folder_path, object_type_id,
document_name, mime_type, properties) -> RawResponse` y
`delete_object(object_id: str) -> RawResponse`.
`RawResponse(status_code: int, reason: str, headers: Mapping[str, str],
body: str, elapsed_ms: int, curl: str)` — `body` COMPLETO (sin
truncar), `curl` es el equivalente con la contraseña enmascarada (reusa
`_build_curl_equivalent`). `RawResponse.ok` = 2xx; `object_id` =
`cmis:objectId` parseado del body o `None`. `CmisUploader` los
implementa: `upload_raw` hace UN solo POST `createDocument` con el
multipart de `_build_multipart_for_upload` — sin reintentos, sin
re-auth, sin backoff: un 401 / 4xx / 5xx se DEVUELVE como `RawResponse`,
no se lanza. Sólo los errores de transporte (`httpx.HTTPError`,
timeout) se lanzan como `CMISServerError` con `context["curl"]`.
`delete_object` es POST `cmisaction=delete` a `root?objectId=…`
(Browser Binding) con `allVersions=true`. `IUploader` (el puerto del
pipeline) NO cambia: la consola usa `CmisUploader` directo, como el
doctor (`cli/doctor.py:_build_uploader`, que se reusa).

**REQ-004 — Caso de uso puro.** `services/practice_upload.py`:
`PracticeDraft(cm_code, mapping: CMMapping, values: dict[str, str],
fmt: SyntheticFormat, size_bytes: int)`; `validate_values(mapping,
values) -> dict[str, str]` (requerido vacío → `"requerido"`; un
`Metadato` sin `CMISPropertyId` en el mapping → `"sin CMISPropertyId
en el mapping"`); `build_properties(mapping, values) -> dict[str, str]`
traduce `Metadato → CMISPropertyId` (`mapping.cmis_property_ids`; si el
mapping no tiene ids — modo consolidado — se usa el nombre tal cual,
como hace `MetadataService`); `document_name(cm_code, fmt, now) ->
"PRUEBA-<IDCM>-<YYYYmmdd-HHMMSS>.<ext>"`; `run_practice_upload(draft,
uploader, *, workdir: Path, now) -> PracticeResult(name, folder,
object_type, size_bytes, size_note, response: RawResponse)`: genera el
archivo en `workdir` (tempfile, se borra al terminar — `finally`),
arma `StagedFile(path, size, page_count=1)` y llama `upload_raw`.
`folder` / `object_type` se resuelven como el pipeline
(`mapping.cmis_folder or mapping.cm_folder`,
`mapping.cmis_type or mapping.cm_object_type`).

**REQ-005 — Pestaña `0·PRUEBA`.** `_TABS` gana `"prueba"` al FINAL
(ids existentes no se mueven); teclas `0` / `F10`. `PracticePane`
(`cli/console/practice_pane.py`):
- Fila 1: `Input#code` (placeholder "código CM, p. ej. CN01") + botón
  `#validate` "validar (↵)". `↵` en el input valida. Sin mapping
  cargado en la config → la pestaña muestra "esta config no tiene
  `mapping:`" y nada más.
- Validar: `get_by_cm_code`. No existe → error en `#code-err` con hasta
  5 sugerencias (`difflib.get_close_matches` sobre `cm_codes()`).
  Existe → panel `#target` con `IDRVI`, `CMISType`, `CMISFolder` (si
  hay varias filas, `Select#row` para elegir; cambiarla re-renderiza) y
  el formulario `#meta` con un `Input#m-<n>` por `Metadato` requerido
  (label = nombre; `#e-<n>` para el error). Si `creds_ready(required=
  ("cmis",))`, además se consulta en un worker `get_type_definition` y
  `verify_folder_exists` y se muestra `tipo ✓ / ✗ <motivo>` y
  `carpeta ✓ / ✗`; sin credenciales: "sin credenciales CMIS — validado
  sólo contra el mapping ([2])". Un ✗ NO bloquea el upload (el punto es
  ver qué responde el servidor).
- Fila 3: `Select#fmt` (pdf/tiff/jpeg/png, default pdf) + `Input#size`
  (texto `parse_size`, default `200kb`) + botón `#upload` "generar y
  subir (s)". Tecla `s` (sólo con la pestaña activa).
- Subir: exige credenciales CMIS (si no, salta a `[2]` como hace `[5]`)
  y `not run_active` ("hay una corrida activa"). `validate_values` con
  errores → marca campos, no sube. En `prd` pide `confirm_text="PRD"`
  (mismo mecanismo de `[5]`); en staging confirma con título "Subir
  documento de prueba" y el cuerpo `<name> → <folder> (<type>)`.
  Corre `run_practice_upload` en `run_worker(thread=True)`; mientras
  tanto `#upload` deshabilitado y "subiendo…".
- Resultado en `#result` (`TextArea` de sólo lectura, `read_only=True`,
  con scroll): primera línea `HTTP <status> <reason> · <elapsed> ms ·
  <name> (<size>)`, luego `## headers` (uno por línea), `## body`
  (JSON indentado si parsea; crudo si no), `## curl` (el equivalente).
  Nada se enmascara: los valores los tipeó el operador en esta sesión.
  `size_note` se muestra como aviso amarillo si no está vacío.
- Historial de la sesión `#history`: una línea por intento
  `<hora> <IDCM> <name> → <status> [<objectId>]`, más reciente arriba,
  máximo 20. Cada intento con `object_id` tiene botón `#del-<n>`
  "borrar"; tecla `d` borra el último con `object_id`. Borrar pide
  confirmación (`danger=True`, en `prd` con `confirm_text="PRD"`),
  llama `delete_object` en worker y vuelca la `RawResponse` en
  `#result`; si `ok`, la línea del historial queda `(borrado)` y pierde
  el botón.
- El documento NO pasa por tracking ni idempotencia: no aparece en
  `[7]` ni en `migration_log`. Se dice en la pantalla, en una línea de
  pista.
- `HelpScreen.HELP`: `[0] ↵ validar código · s generar y subir · d
  borrar el último subido`; cabecera `F1–F10`, `1-9,0 / F1-F10`.

**REQ-006 — Documentación.** `docs/how-to/probar-la-consola.md`
sección "Probar un código antes de la corrida (`[0]`)" con ejercicio
guiado (`CN01` sobre `sample/config-local.yaml`, PDF de 200 KB, leer la
respuesta, borrar). `docs/reference/cli.md`: tab `0·PRUEBA`, teclas
`0`/`F10`, `s`, `d`. Tutorial 04 y `console-flow.md`: nodo `[0]`.
`docs/explanation/operations-console.md`: párrafo "Un tiro antes de la
corrida" (por qué no pasa por tracking, por qué sin reintentos).

## Escenarios

**E1 —** `get_by_cm_code("CN01")` sobre `sample/MapeoRVI_CM.csv` +
`MetadatosCM.csv` devuelve una tupla no vacía con `id_corto == "CN01"`;
`get_by_cm_code("ZZ99")` → `()`; `cm_codes()` está ordenado y sin
repetidos. El índice por `IDRVI` no cambia (tests existentes verdes).

**E2 —** `build_synthetic_file("pdf", 200_000, "x")` → exactamente
200 000 bytes, `%PDF` al inicio, mime `application/pdf`. Para
`tiff`/`jpeg`/`png` con 300 KB: Pillow abre el resultado, el formato
coincide, y `abs(len - 300_000) <= 45_000` o `size_note != ""`. Mismo
`marker` → mismos bytes. `size_bytes=500` y `60 MB` → `ValueError`.

**E3 —** `upload_raw` contra un servidor fake (`httpx.MockTransport`):
201 con body JSON → `RawResponse.ok`, `object_id` parseado, `body`
completo aunque tenga 10 000 caracteres, `curl` sin la contraseña;
409 → `ok is False`, `object_id is None`, NO lanza, UN solo request
(sin reintentos); `httpx.ConnectError` → `CMISServerError` con
`context["curl"]`. `delete_object("abc")` manda
`cmisaction=delete` y `objectId=abc`.

**E4 —** `validate_values` con un requerido vacío → `{"BAC_Nombre":
"requerido"}`; `build_properties` traduce a `CMISPropertyId` y, sin
ids, deja el nombre. `run_practice_upload` con un uploader fake: el
tempfile existe DURANTE `upload_raw` y no existe después, incluso si
`upload_raw` lanza.

**E5 — Pilot.** Con `sample/config-local.yaml`: `goto "0"`, tipear
`CN01`, `↵` → `#target` visible con la carpeta del CSV y un `Input` por
requerido; tipear `ZZ99` → `#code-err` contiene "no está en el mapping"
y alguna sugerencia.

**E6 — Pilot.** Sin credenciales CMIS, `s` → salta a `[2]` con el
aviso. Con credenciales (fake) y un requerido vacío → `#e-<n>` dice
"requerido" y no hubo request. Con todo cargado → confirmación → el
uploader fake recibe `properties` con los `CMISPropertyId`, `mime_type`
según `#fmt`, y `#result` empieza con `HTTP 201`; `#history` tiene una
línea con el `objectId` y un botón `#del-0`.

**E7 — Pilot.** `d` → confirmación → `delete_object` con ese
`objectId`; `#result` muestra la respuesta del delete y la línea del
historial dice `(borrado)`.

**E8 — Pilot.** Con `run_active` → `s` avisa "hay una corrida activa"
y no sube. Config sin `mapping:` → la pestaña sólo muestra el aviso.

**E9 —** Ayuda: `HELP` menciona `[0]`, `s`, `d`, `F10`; `_TABS[-1] ==
"prueba"`; `BINDINGS` tiene `0` y `f10`.

## Notas de implementación

- Reusar `cli/doctor.py:_build_uploader` (hacerlo público:
  `build_uploader`) para construir el `CmisUploader` con
  `state.creds.to_secrets()` y `effective_config()`.
- `parse_size` está en `services/mock/sizing.py`.
- Página única para las imágenes: `page_count=1` siempre.
- El worker de validación contra el servidor y el de upload no deben
  solaparse: un solo `_busy` en el pane; mientras está ocupado, `↵` y
  `s` avisan "esperá".
- Funciones ≤ 50 líneas; el pane se parte en módulo puro
  (`services/practice_upload.py`) + widget (`practice_pane.py`).
- No tocar `IUploader` ni los mocks de test del pipeline.
