# 135 — Escribir los overrides de sesión al YAML desde `[3] CONFIG`

## Por qué

Los overrides de `[3]` son de sesión: cerrás la consola y se van. Cuando
el operador encontró el `cmis.workers` o el `max_bandwidth_mbps` que
funciona, hoy tiene que abrir el YAML a mano y copiarlo. La consola ya
sabe el valor y sabe validarlo — que lo escriba ella.

## Qué

**REQ-001 — Parche textual verificado.** `persist_overrides(config_path,
config, overrides) -> PersistResult` escribe SÓLO los escalares de `[3]`
(`processing.mode`, `processing.prep_workers`,
`processing.streaming.bucket_size`, `cmis.workers`,
`cmis.auto_tune.enabled`, `cmis.max_bandwidth_mbps`,
`observability.unmask_pii`) editando el texto del YAML línea a línea:
reemplaza el valor de la clave si existe (conservando su comentario
inline), la agrega al final del bloque si el bloque existe, o agrega el
bloque al final del archivo. Todo lo demás del archivo — comentarios,
orden, espaciado — queda byte-idéntico. `trigger` (127) NO se persiste:
es una elección del launcher.

**REQ-002 — Sólo si el resultado es exacto.** Antes de tocar el archivo
se escribe un temporal en el mismo directorio, se carga con
`load_config` y se compara (`==` de modelos pydantic) contra
`apply_overrides(config, overrides)`. Si difiere (flow style `{a: 1}`,
anchors, claves duplicadas, lo que sea) o no valida, el temporal se
borra y se levanta `PersistError` con motivo — el YAML original no se
toca. Si coincide: backup `config.yaml.bak-YYYYmmdd-HHMMSS` (copia) y
`os.replace` atómico.

**REQ-003 — Consola.** En `[3]`, tecla `w` y botón "escribir en el YAML
(w)". Precondiciones: overrides aplicados no vacíos (si el borrador está
sucio: "primero guardalos con a"); confirmación de peligro que muestra
la ruta y los valores que van a cambiar. Tras escribir: `app.config` se
recarga desde disco, los overrides de sesión se limpian (ya viven en el
YAML), `[3]` refresca sus placeholders `(yaml) …`, el doctor se marca
stale y se notifica con la ruta del backup. Una corrida activa no cambia
(usa su config efectiva ya construida).

**REQ-004 — Guía y ayuda.** `?` lista `[3] w escribir al YAML`; la guía
documenta el flujo y el backup; la viñeta "Editar y guardar el YAML" se
va de "Qué NO hace todavía" (con la aclaración de que es SÓLO lo que se
edita en `[3]`, no un editor libre).

## Escenarios

**E1 —** YAML con `cmis:\n  workers: 4  # ojo` y overrides
`workers=8` → la línea queda `  workers: 8  # ojo`; el resto del archivo
byte-idéntico; `load_config` devuelve `workers == 8`.

**E2 —** YAML sin bloque `processing:` y override `mode=streaming` →
se agrega `processing:\n  mode: streaming` al final; valida.

**E3 —** YAML con `cmis: {base_url: x, repo_id: y}` (flow style) y
override `workers=8` → `PersistError`, archivo intacto, sin backup.

**E4 —** Consola: `w` con overrides aplicados → confirm → el YAML cambia,
`state.overrides.is_empty()`, aparece `config.yaml.bak-*` y el
placeholder de `cmis.workers` en `[3]` dice `(yaml) 8`. `w` con borrador
sucio → notificación de error, sin modal.

## Notas de implementación

- Alternativa descartada: round-trip con PyYAML (pierde TODOS los
  comentarios — `config-reference.yaml` es mayormente comentarios) o
  agregar `ruamel.yaml` (round-trip con comentarios, pero dependencia
  nueva con quoting propio). El parche textual + verificación semántica
  cubre el 100 % de los YAML del proyecto sin dependencia nueva, y falla
  CERRADO en los raros.
- Indentación: se detecta la del bloque a partir de su primera clave
  hija; si el bloque está vacío o es flow style, se cae al fallo cerrado
  de REQ-002.
- Valores: `bool` → `true`/`false`; `float` → `repr` corto; `str` sin
  comillas salvo que PyYAML lo interprete distinto (se verifica igual
  por REQ-002).
