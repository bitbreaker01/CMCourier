# Agregar un campo de configuración nuevo

> [← Volver al índice](../../INDEX.md) · [How-to](../README.md) · [Developer](README.md)

Exponer una perilla nueva del pipeline implica tocar un único módulo declarativo (`schema.py`), el consumidor que la lee, los tests, la doc de referencia, el sample YAML y el CHANGELOG. Los modelos Pydantic son `frozen=True, extra="forbid"`, así que cualquier typo o clave desconocida explota al cargar.

## Cuándo aplica

- Queremos exponer un parámetro de tuning, un toggle, un path o un umbral.
- El valor lo controla el operador desde el YAML, no por env vars (las env vars viven en `config/env.py:Secrets` — flujo distinto).
- Tiene un default sensato o es obligatorio (si es obligatorio, considerá si es realmente *config* y no un argumento de CLI).

## Pasos

### 1. Agregá el campo Pydantic en `schema.py`

Ejemplo: queremos `processing.foo_workers: int = 4` (≥ 1) para el nuevo stage hipotético.

Editar `src/cmcourier/config/schema.py`:

```python
class ProcessingConfig(BaseModel):
    model_config = _STRICT
    mode: Literal["batched", "streaming"] = "batched"
    # ... campos existentes ...
    s4_max_processes: int | None = Field(default=None, ge=1)

    # 074: paralelismo del nuevo stage Foo. Default 4 mantiene el
    # comportamiento canónico; el operador lo sube cuando tiene
    # cores ociosos.
    foo_workers: int = Field(default=4, ge=1, le=256)
```

Reglas firmes:

- Todo campo lleva tipo concreto, default explícito (o `Field(...)` para requerido) y `ge`/`le`/`min_length` cuando aplique.
- Si interactúa con otros campos, agregá un `@model_validator(mode="after")` que cruce las invariantes (ver `MappingConfig._exactly_one_mode` como template).
- Si el campo es un string que se interpola a SQL DB2, validá con `_validate_sql_identifier` (ver `NiarvilogColumnsModel`).
- Comentario breve con el spec/issue que lo introduce — la doc inline es la primera línea de defensa para revisores y operadores.

### 2. Test unit del default y los bounds

Agregar en `tests/unit/config/test_schema.py`:

```python
def test_processing_foo_workers_default() -> None:
    cfg = ProcessingConfig()
    assert cfg.foo_workers == 4


def test_processing_foo_workers_rejects_zero() -> None:
    with pytest.raises(ValidationError):
        ProcessingConfig(foo_workers=0)


def test_processing_foo_workers_rejects_above_ceiling() -> None:
    with pytest.raises(ValidationError):
        ProcessingConfig(foo_workers=257)
```

Si el campo tiene un model_validator cruzado, sumá un test que dispare la `ValueError` para cada rama del validador.

### 3. Propagá al consumidor

`schema.py` es declarativo; nadie pipeline-adentro lo importa. La traducción la hace `cmcourier.config.wiring` — buscá la función que construye el objeto runtime correspondiente y enchufá el campo nuevo.

Ejemplo (cableado de Processing al orchestrator):

```python
# src/cmcourier/config/wiring.py
def _build_pipeline_options(config: PipelineConfig) -> PipelineOptions:
    return PipelineOptions(
        # ... existentes ...
        s4_use_processes=config.processing.s4_use_processes,
        foo_workers=config.processing.foo_workers,  # 074
    )
```

Si el consumidor vive en `services/` o `orchestrators/`, recordá la regla del Principio I: esas capas NO importan `pydantic` ni `cmcourier.config.schema` — reciben un dataclass plain construido en `wiring.py`.

### 4. Actualizá `docs/reference/config-schema.md`

Agregar la fila en la tabla de `processing` con: nombre, tipo, default, rango, descripción corta de una línea. Mantené el orden por aparición en `schema.py`.

### 5. Sample YAML

Si [`docs/reference/config-reference.yaml`](../../reference/config-reference.yaml) documenta el bloque, sumá la línea anotada bajo el bloque que corresponde:

```yaml
processing:
  # ... existentes ...
  foo_workers: 4                          # (default: 4, range: 1-256) paralelismo del stage Foo (074)
```

Mantené el placeholder o el default — este YAML no se ejecuta tal cual, es referencia.

### 6. CHANGELOG bajo `Unreleased`

`CHANGELOG.md` arriba de todo, sección `[Unreleased] - Added` (o `Changed` si modificás semántica):

```markdown
### Added
- `processing.foo_workers` (default `4`, range `1-256`) — paralelismo del stage Foo. Spec 074.
```

Conventional commits: `feat(config): add processing.foo_workers for stage Foo parallelism`.

### 7. ¿Es breaking?

Cualquiera de estas lo hace breaking:

- Renombrar o eliminar un campo existente.
- Cambiar el default cuando el cambio altera el comportamiento observable.
- Agregar un campo **requerido** sin default.

En esos casos:

- Bump de versión mayor en `pyproject.toml` y `CHANGELOG.md`.
- Sumá una nota de migración en `docs/runbooks/` o `docs/explanation/` describiendo el path de upgrade.
- Considerá un alias retrocompatible vía `@field_validator(mode="before")` o un `Annotated[..., AliasChoices(...)]` para no romper YAMLs vivos. Ver el patrón `_coerce_system_metrics` en `ObservabilityConfig` como ejemplo de coerción suave.

## Verificación

```bash
pytest tests/unit/config/test_schema.py -v   # bounds y defaults
pytest tests/unit/config/test_loader.py -v   # round-trip YAML → modelo
pytest -m unit                                # toda la suite unit
mypy src/cmcourier/config                     # strict en config/
ruff check src/cmcourier/config tests/unit/config
```

Si el campo afecta a un consumer pipeline, corré también `pytest tests/integration/config/` y la integration del consumer.

## Gotchas

- **`extra="forbid"` muerde**: un typo en YAML (`foo_woRkers`) levanta `ValidationError` al cargar — útil para el operador, pero asegurate de que el nombre canónico del campo coincida con lo documentado.
- **`frozen=True`**: no podés mutar un modelo ya construido. Si necesitás "variantes" en runtime, devolvé un modelo nuevo con `model_copy(update={...})`.
- **`FilePath` vs `Path`**: `FilePath` exige que el archivo exista al validar. Usalo para inputs reales (CSVs); usá `Path` para outputs que se crean en runtime (temp dirs, SQLite paths).
- **Defaults mutables**: `list`/`dict`/`set` siempre vía `Field(default_factory=...)`, nunca `Field(default=[])` — Pydantic lo permite pero compartirías referencia entre instancias.
- **Sin perilla en CLI por capricho**: Click no debería ofrecer un flag para cada campo del YAML. Solo lo que el operador realmente ajusta por corrida (ver `app.py` para el patrón actual).

## Ver también

- [`../../reference/config-schema.md`](../../reference/config-schema.md) — catálogo completo de campos
- [`../../reference/config-reference.yaml`](../../reference/config-reference.yaml) — sample anotado
- `src/cmcourier/config/schema.py` — fuente declarativa de verdad
- `src/cmcourier/config/wiring.py` — traducción schema → runtime
- `src/cmcourier/config/loader.py` — carga YAML → modelos
- `.specify/memory/constitution.md` — Principio V (single source of truth declarativa)
