# 092 — Comando `cmcourier diagnose`

## Por qué

Operador productivo reportó prep lento (350s para 200 docs pesados,
~1.75s/doc). Pedidos ad-hoc en PowerShell para extraer métricas de
los `metrics-*.jsonl` fallaban silenciosamente (errores ignorados
con `-ErrorAction SilentlyContinue`, paths incorrectos, JSON parse
errors no reportados).

**Necesitamos un diagnóstico versionado**, no PowerShell improvisado.

## Qué

### Nuevo comando

```powershell
# Analizar el batch más reciente
cmcourier diagnose --config <yaml> --latest

# Analizar un batch específico
cmcourier diagnose --config <yaml> --batch <uuid>

# Listar los batches disponibles para analizar
cmcourier diagnose --config <yaml> --list
```

### Output ejemplo

```
════════════════════════════════════════════════════════════════════════
  Batch f4a8e2-…  — diagnose report
════════════════════════════════════════════════════════════════════════
  Pipeline:           csv-trigger
  Total docs:         200
  Wall time:          470.3 s
  Throughput:         0.43 docs/s

  Latencia por stage:

    Stage   Count   Avg ms   P50 ms   P95 ms    Sum s     %
    ────────────────────────────────────────────────────────────
    S1        200      5.0      4.0     12.0      1.0   0.3%
    S2        200      1.0      1.0      3.0      0.2   0.1%
    S3        200     80.0     45.0    280.0     16.0   3.4%
    S4        200   1650.0   1400.0   3800.0    330.0  70.2% ←
    S5        200    610.0    450.0   1820.0    122.0  26.0%
    ────────────────────────────────────────────────────────────

  ▶ Bottleneck detectado: S4

  ▶ Sugerencias:
      S4 (assembly) is dominant with abnormal latency.
      Likely causes:
        1. Source disk slow (SMB share, HDD, or under AV scan)
        2. Process pool overhead exceeds work for small files
        3. img2pdf recompressing uncommon TIFF compression
      Quick checks:
        - Get-PhysicalDisk | Select FriendlyName, MediaType
        - Get-MpPreference | Select-Object -ExpandProperty ExclusionPath
        - If mostly small PDFs: try processing.s4_use_processes: false
```

### Cambios

1. **`src/cmcourier/cli/commands/diagnose.py`** (nuevo):
   - Lee `observability.log_dir/metrics-*.jsonl`
   - Parsea los eventos `batch_summary`
   - Calcula tabla por stage con `%` del wall time
   - Detecta cuello (stage con > 50% del total)
   - Emite sugerencias contextuales por patrón

2. **`src/cmcourier/cli/app.py`**: registra `diagnose_command` en el
   grupo `main`.

### Diseño explícito

- **Fail-loud**: si no hay `metrics-*.jsonl`, sale con código != 0 y
  mensaje claro. Sin silenciar errores.
- **No depende de SQLite**: solo lee los JSONL. Si la tracking DB se
  perdió, el comando sigue funcionando.
- **Sugerencias por patrón, no por hardcoded rules**: S4 con alta
  latencia → sospechas de disco. S5 con alta latencia → sospechas de
  CMIS server. Etc. Cada sugerencia incluye el comando concreto que
  el operador puede correr para verificar.

### Tests

* `tests/unit/cli/commands/test_diagnose.py`:
  - Detección de cuello cuando un stage supera 50% del wall
  - Sin cuello cuando 3+ stages se reparten parejo
  - Sugerencias específicas por stage
  - Manejo de archivos faltantes / vacíos / JSON malformado
  - Selección por `--latest` y `--batch <id>`

## Criterios de aceptación

1. `cmcourier diagnose --config X --latest` corre sin errores cuando
   hay logs presentes.
2. Cuando no hay logs, sale con código != 0 y mensaje explicativo
   (no silencioso como los PowerShell ad-hoc).
3. La tabla por stage incluye `count`, `avg`, `p50`, `p95`, `sum_s`,
   `%` y marca el cuello con `←`.
4. Las sugerencias por stage son **accionables** (incluyen comandos
   concretos), no genéricas.
5. `pytest -m unit` pasa.

## Riesgos

* **Depende del formato actual de `batch_summary`**. Si cambia
  el schema interno del `MetricsRecorder`, el comando deja de
  funcionar. Mitigación: tests parsean el shape real, y el comando
  skipea eventos no-summary silenciosamente.
* **El cuello "detectado" es por dominancia (>50% del wall)**, no por
  análisis profundo. Es heurístico. Para análisis más fino, requiere
  spec 093 (sub-stage metrics en S4).

## Notas

- Pareja con spec 093 (próxima): métricas sub-stage en S4. Cuando
  shippee 093, `diagnose` puede mostrar dentro de S4 (s4_open_source,
  s4_read_source, s4_assemble_pdf, s4_write_staged) — pero el shape
  del summary ya soporta cualquier nombre de stage, así que el
  comando NO requiere cambios adicionales para esa spec.
- El comando es **idempotente y read-only**. No modifica nada del
  estado — solo lee logs.
