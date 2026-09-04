# 120 — Higiene de respuestas del uploader: decode perezoso y lookup 409 liviano

## Por qué

Dos hallazgos menores de la auditoría, ambos en `cmis_uploader.py`:

### A. `resp.text` se decodifica incondicionalmente (P11)

En `get_type_definition` (`:399`), `verify_folder_exists` (`:446`) y
`_lookup_existing_object_id` (`:642`), el body completo se decodifica a
`str` ANTES de chequear el status — incluso en respuestas 200, solo
para pasarlo por `_truncate` (cap 1024) que después no se usa. En el
lookup de 409 eso significa decodificar la respuesta de hasta 5000
hijos… para tirarla.

### B. El lookup de 409 pide 5000 objetos completos (P12)

`_lookup_existing_object_id` (`:627-630`) pide
`cmisselector=children&maxItems=5000` sin filtro de propiedades: cada
hijo llega con TODAS sus propiedades CMIS. Para carpetas grandes es una
respuesta JSON enorme, parseada entera, por cada 409 — cuando lo único
que se necesita son `cmis:name` y `cmis:objectId`.

## Qué

**REQ-001 — Decode solo en el path de error.** Helper `_http_error(resp)`
que construye la excepción (`CMISServerError` ≥500 /
`CMISClientError` ≥400) decodificando y truncando el body ahí adentro.
Los tres sitios pasan a `if resp.status_code >= 400: raise
_http_error(resp)` — una respuesta 200 nunca decodifica el body a str
(el `.json()` posterior trabaja sobre bytes).

**REQ-002 — Lookup 409 con filtro de propiedades.** La request de
children agrega `filter=cmis:name,cmis:objectId` y `succinct=true`
(CMIS 1.1 browser binding): cada hijo viaja con dos propiedades en
formato compacto. El parser existente ya maneja `succinctProperties`.

**REQ-003 — Sin cambio de contrato.** Mismas excepciones con el mismo
`response_body` truncado en los paths de error; misma resolución de
objectId en el 409.

## Escenarios

**E1 — Una respuesta 200 no decodifica `.text`.**
**E2 — Un 500 levanta `CMISServerError` con el body truncado.**
**E3 — La request del lookup 409 lleva `filter` y `succinct`.**
**E4 — El lookup resuelve el objectId desde `succinctProperties`.**
