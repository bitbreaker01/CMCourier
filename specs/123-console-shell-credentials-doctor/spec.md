# 123 — Consola de operación, Fase 1: shell + credenciales de sesión + doctor interactivo

## Por qué

La TUI actual solo monitorea una corrida ya lanzada. Todo lo demás —
credenciales, pre-flight, parámetros, lanzar — vive en la shell y el
YAML. El operador (bancario, corridas de horas, a veces air-gapped)
necesita una consola completa en la terminal. El diseño está congelado
en el mock v2 (artifact `cmcourier-control`) + los dos informes UX
adversariales (memoria `tui-console/mock-and-ux`).

Fase 1 de 3: el esqueleto navegable + los dos flujos que habilitan todo
lo demás (credenciales de sesión y doctor).

## Qué

**REQ-001 — Comando `cmcourier console --config <yaml>`.** Abre la
consola Textual. Requiere TTY (mismo contrato que `--tui`). Exit 0 al
salir con `q`, exit 2 ante config inválida.

**REQ-002 — Shell de 7 tabs.** INICIO · CREDENCIALES · CONFIG · DOCTOR
· CORRER · MONITOR · BATCHES (CONFIG/CORRER/MONITOR/BATCHES quedan como
placeholders honestos en F1: "disponible en la fase N"). Navegación:
teclas `1-7` (y `F1-F7` con `priority=True`, inmunes al foco en
inputs), `?` ayuda con leyenda de stages, `q` salir con confirmación.
Footer Textual con bindings por pantalla.

**REQ-003 — Campo `environment` en el schema.** Nuevo campo top-level
`environment: Literal["staging","prd"] = "staging"` en `PipelineConfig`.
La consola lo muestra como badge permanente en el header (staging
verde / `⚠ PRODUCCIÓN` en rojo). No afecta a los comandos existentes.

**REQ-004 — Credenciales de sesión.** Pantalla CREDENCIALES con
usuario/contraseña CMIS y AS400: viven en un `SessionCredentials`
en memoria (pre-cargado desde env si `CMIS_USERNAME`… ya están
exportadas), producen un `Secrets` para doctor/pipeline, y NUNCA se
escriben a disco. Comportamiento (contrato UX):
- Editar un campo invalida el estado "probada" y marca el doctor stale.
- `probar conexión` corre en un worker (no bloquea la UI) y muestra
  ok·latencia / error accionable.
- AS400: contador visible "intento N de 3"; el 3° intento pide
  confirmación explícita (lockout del perfil en el iSeries).
- Toggle mostrar/ocultar contraseña.
- TTL (informe UX v2, M2): una prueba caduca a los 45 min — el chip
  degrada a "vencida, reprobar" y la credencial deja de contar como
  lista para el gate de lanzamiento.
- La derivación "¿hace falta AS400?" sale del YAML (indexing.source,
  fuentes de metadata, as400_sync) — no del trigger.kind (A7).

**REQ-005 — Wrappers públicos de chequeo puntual.** `cli/doctor.py`
expone `check_cmis(config, secrets)` y `check_as400(config, secrets)`
(delgados sobre los checks existentes) para el botón "probar conexión".

**REQ-006 — Doctor interactivo.** Pantalla DOCTOR: selector de grupo
con los grupos REALES **importados** de `_CHECK_GROUPS` (nunca
re-tipeados — hallazgo A8 del informe v2), `d` corre en un worker,
resultados como lista navegable (↑↓, `enter` expande el detalle),
resumen `N ok · N warn · N fail`. Estado **stale** cuando cambian
credenciales (F2 le sumará los overrides). El re-run de un check
individual queda FUERA (la API `run_doctor` es atómica y los skips
dependen de la corrida completa — decisión del informe v2).

**REQ-007 — INICIO.** Estado de conexiones, veredicto del doctor,
"siguiente paso" que se actualiza con la máquina de estados
(credenciales → doctor → lanzar).

### Fuera de alcance (F2/F3)

Overrides de sesión, launcher, lock, monitor, batches, re-auth en
caliente, auto-lock por inactividad.

## Escenarios

**E1 —** `cmcourier console --config sample/config-local.yaml` abre la
consola; `2` va a CREDENCIALES; `q` + confirmar sale con exit 0.
**E2 —** Sin credenciales, DOCTOR corre y `cmis_connectivity` FALLA con
mensaje que apunta a [2]; con credenciales probadas, pasa.
**E3 —** Probar CMIS ok → editar la contraseña → el chip vuelve a
"sin probar" y el doctor (si estaba aprobado) queda stale.
**E4 —** `environment: prd` en el YAML → badge rojo `⚠ PRODUCCIÓN`.
**E5 —** Los comandos existentes ignoran `environment` (default
staging, sin cambios de comportamiento).
