# 124 — Consola, Fase 2: overrides draft/applied + launcher + lock + auditoría

## Por qué

Con el shell (123) andando, faltan las tres capacidades que convierten
la consola en un launcher real. El informe UX v2 fijó las decisiones
duras: los overrides del mock lanzaban sin guardar (C4), el auto-doctor
con `sys.exit(2)` hacía imposible el "lanzar igual" (C2), streaming
rechaza resume (C1), y la auditoría no tenía columnas donde vivir (C3
— y hay que escribirlas DESDE F2 o los batches quedan huérfanos).

## Qué

**REQ-001 — Overrides de sesión con modelo draft/applied (C4).**
`SessionOverrides` (dataclass, todos los campos opcionales = usar el
YAML): `mode`, `prep_workers`, `bucket_size`, `workers`,
`auto_tune_enabled`, `max_bandwidth_mbps`, `unmask_pii`. La pantalla
CONFIG edita un **draft**; `a` valida rangos y lo promueve a
**applied**. El launcher y el resumen efectivo leen SOLO applied. Con
draft ≠ applied, el launcher avisa "tenés overrides sin guardar".
Promover marca el doctor stale. `apply_overrides(config, ov)` produce
el `PipelineConfig` efectivo vía `model_copy` anidado.

**REQ-002 — Launcher por `trigger.kind` (A7).** CORRER muestra el
pipeline del YAML (read-only) y los campos que ese kind acepta:
`--total` y `--max-duration` para csv/rvabrep/local_scan;
`shortname` + `system` + `cif` para single_doc. Credenciales
requeridas derivadas del YAML (`as400_required`, 123).

**REQ-003 — Resume solo en modo batched (C1).** El modo "reanudar un
batch" aparece únicamente cuando el modo EFECTIVO es `batched`. Un
batch es reanudable cuando `get_batch_details` muestra
`FAILED + PENDING > 0` (A4), no por su estado. En streaming, el
launcher lo dice: "resume requiere mode: batched". `--max-duration`
aplica también en resume (M3).

**REQ-004 — El gate del doctor es de la consola (C2).** El pipeline se
construye SIEMPRE sin auto-doctor (la consola ya lo corrió o el
operador decidió saltearlo). Cadena de guardas al lanzar:
1. credenciales listas (frescas, TTL) — si no, ir a [2];
2. doctor no-aprobado → modal "lanzar igual / ir a doctor" que aclara
   que se saltea el pre-flight embebido;
3. `environment: prd` → confirmación tipeada "PRD" (modal danger);
4. corridas `in_progress` en el tracking → advertencia con los ids
   (interlock distribuido de facto, A6-Should).

**REQ-005 — Lock de config (A6).** Al lanzar se adquiere
`acquire_config_lock(config_path)` y se mantiene hasta el final de la
corrida. `LockHeldError` → modal explicando que otra instancia LOCAL
tiene esta config (el lock es por estación — etiquetado honesto).

**REQ-006 — Auditoría persistida (C3).** `migration_batch` gana
columnas nullable vía migración idempotente (`ALTER TABLE` si faltan):
`operator`, `station`, `pipeline_kind`, `environment`, `config_hash`,
`overrides_json`, `doctor_verdict`, `outcome`. El tracking store gana
`record_batch_audit(batch_id, …)` y `set_batch_outcome(batch_id,
outcome)` (encoladas). El run manager las escribe por cada batch de la
corrida: outcome `completed` | `cancelled`. Las DBs existentes migran
solas al abrir.

**REQ-007 — Run manager.** `ConsoleRunManager`: construye el config
efectivo + `Secrets` de sesión, re-aplica observability (el toggle de
PII es de proceso — M7), arma pipeline + orchestrator (misma lógica de
modo/resume que `_run_with_optional_tui`), `TUIDataProvider` (vía un
builder compartido `build_data_provider`), `DeadlineWatchdog` si hay
max-duration, y corre en un worker thread no-daemon. Al terminar:
audita, libera el lock y notifica a la app (`run_active`,
chip del header, resumen). MONITOR completo llega en F3; en F2 el tab
muestra el estado mínimo (batch, corriendo/terminado).

## Escenarios

**E1 —** Editar workers sin promover y lanzar → la corrida usa el YAML
(no el draft) y el launcher avisó "sin guardar".
**E2 —** Promover workers=999 → error de validación; workers=12 →
applied, doctor stale.
**E3 —** Config streaming → el launcher no ofrece resume.
**E4 —** `environment: prd` → lanzar exige tipear PRD.
**E5 —** Tras una corrida, `migration_batch` tiene operator, config
hash, overrides JSON, doctor verdict y outcome.
**E6 —** Segunda consola sobre el mismo YAML lanzando → LockHeldError
→ modal informativo, sin corrida.
