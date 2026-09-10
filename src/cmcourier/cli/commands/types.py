"""Grupo ``cmcourier types ...`` — el manifest de tipos CM (145 REQ-004).

El manifest es la foto de lo que Content Manager ya publica en
``typeDescendants``: nadie mantiene a mano lo que el servidor sabe. Este
grupo es la cara operativa de esa idea:

* ``types discover``: baja el árbol entero y escribe el manifest.
* ``types show <IDCM>``: la ficha de un tipo — propiedades y decisiones.
* ``types diff``: qué cambió el servidor desde el último manifest.
* ``types update``: mergea esos cambios conservando lo que decidiste.
* ``types review <IDCM>``: marcá qué propiedades van al wire y firmá.

El path del manifest sale de ``mapping.type_manifest_path`` del YAML;
``--manifest PATH`` lo pisa. Los comandos que hablan con el servidor
(``discover``, ``diff``, ``update``, ``show --live``) necesitan las
credenciales `cmis`; ``show`` y ``review`` sobre el manifest local corren
offline, sin ``--config``.

El progreso va a **stderr** (144): ``types diff > cambios.txt`` no se
ensucia.
"""

from __future__ import annotations

__all__ = ["types_group"]

import sys
from pathlib import Path

import click

from cmcourier.adapters.manifest.json_store import JsonTypeManifestStore
from cmcourier.cli.commands._formatting import render_table, truncate
from cmcourier.cli.doctor import build_uploader
from cmcourier.config.loader import Secrets, load_config, load_secrets
from cmcourier.config.schema import PipelineConfig
from cmcourier.domain.cm_types import (
    DECISION_OMIT,
    DECISION_USE,
    CmPropertyDef,
    CmTypeEntry,
    CmTypeManifest,
    canonical_name,
)
from cmcourier.domain.exceptions import ConfigurationError
from cmcourier.services.sync_progress import SyncProgress
from cmcourier.services.type_discovery import TypeDiscoveryService
from cmcourier.services.type_manifest import (
    apply_diff,
    diff_manifest,
    mark_reviewed,
    set_decision,
    set_folder,
)

_SHOW_HEADERS = [
    "PROPIEDAD",
    "TIPO",
    "CARD",
    "ESCRITURA",
    "LIMITES",
    "DEFAULT",
    "DECISION",
    "NOMBRE",
]
#: Cómo se ve ``folder_ok`` para el operador: verificada, ausente, sin verificar.
_FOLDER_MARK = {True: "ok", False: "✗", None: "?"}


@click.group(name="types")
def types_group() -> None:
    """Manifest de tipos CM: descubrir, comparar, revisar (145)."""


# ---------------------------------------------------------------------------
# Setup compartido
# ---------------------------------------------------------------------------

_CONFIG_OPTION = click.option(
    "--config",
    "-c",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="YAML del pipeline (sección `cmis` + `mapping.type_manifest_path`).",
)
_OPTIONAL_CONFIG_OPTION = click.option(
    "--config",
    "-c",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="YAML del pipeline. Opcional si pasás --manifest: se trabaja offline.",
)
_MANIFEST_OPTION = click.option(
    "--manifest",
    "manifest_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Manifest JSON a usar. Default: `mapping.type_manifest_path` del YAML.",
)


def _load(config_path: Path) -> tuple[PipelineConfig, Secrets]:
    """Carga YAML + credenciales `cmis`. Sale con 2 si falta algo."""
    try:
        config = load_config(config_path)
        secrets = load_secrets(config)
    except ConfigurationError as exc:
        click.echo(f"ConfigurationError: {exc}", err=True)
        click.echo(
            "types necesita el YAML y las credenciales CMIS_USERNAME / CMIS_PASSWORD.",
            err=True,
        )
        sys.exit(2)
    return config, secrets


def _config_or_none(config_path: Path | None) -> PipelineConfig | None:
    """Carga el YAML sólo si lo pasaron — ``show``/``review`` viven offline."""
    if config_path is None:
        return None
    try:
        return load_config(config_path)
    except ConfigurationError as exc:
        click.echo(f"ConfigurationError: {exc}", err=True)
        sys.exit(2)


def _store(config: PipelineConfig | None, override: Path | None) -> JsonTypeManifestStore:
    """Resuelve el path del manifest: ``--manifest`` gana sobre el YAML.

    El ``getattr`` es a propósito: el campo lo agrega REQ-001 y este
    grupo tiene que funcionar aunque la config todavía no lo declare."""
    if override is not None:
        return JsonTypeManifestStore(override)
    configured = getattr(config.mapping, "type_manifest_path", None) if config else None
    if not configured:
        raise click.ClickException(
            "no hay manifest configurado (mapping.type_manifest_path) ni --manifest"
        )
    return JsonTypeManifestStore(Path(str(configured)))


def _read(store: JsonTypeManifestStore) -> CmTypeManifest:
    """Lee el manifest del disco; un manifest ilegible sale con 2."""
    try:
        return store.load()
    except ConfigurationError as exc:
        click.echo(f"ConfigurationError: {exc}", err=True)
        sys.exit(2)


def _echo_progress(event: SyncProgress) -> None:
    """Una línea por evento en stderr — igual que ``sync recover`` (144)."""
    click.echo(f"{event.phase} {event.done}/{event.total}", err=True)


def _discovery(config: PipelineConfig, secrets: Secrets) -> TypeDiscoveryService:
    return TypeDiscoveryService(build_uploader(config, secrets))


def _fetch_live(config: PipelineConfig, secrets: Secrets) -> CmTypeManifest:
    """La foto fresca del servidor, sin verificar carpetas."""
    return _discovery(config, secrets).fetch_live(
        service_url=config.cmis.base_url,
        repository_id=config.cmis.repo_id,
        on_progress=_echo_progress,
    )


def _entry_or_fail(manifest: CmTypeManifest, id_corto: str) -> CmTypeEntry:
    entry = manifest.types.get(id_corto)
    if entry is None:
        click.echo(f"el manifest no tiene el ID corto {id_corto}", err=True)
        sys.exit(1)
    return entry


# ---------------------------------------------------------------------------
# types discover
# ---------------------------------------------------------------------------


def _refuse_if_reviewed(store: JsonTypeManifestStore, force: bool) -> None:
    """Un discover pisa TODO. Si hay tipos firmados, pedimos permiso explícito."""
    if force or not store.exists():
        return
    reviewed = [code for code, e in _read(store).types.items() if e.reviewed]
    if not reviewed:
        return
    click.echo(
        f"el manifest ya tiene {len(reviewed)} tipo(s) revisado(s) "
        f"({', '.join(sorted(reviewed)[:5])}...): usá 'cmcourier types update' "
        "para traer los cambios del servidor sin perderlos, o --force para descubrir de cero.",
        err=True,
    )
    sys.exit(1)


def _echo_discover_summary(manifest: CmTypeManifest, store: JsonTypeManifestStore) -> None:
    folders = [e.folder_ok for e in manifest.types.values()]
    click.echo(
        f"types discover: {len(manifest.types)} tipo(s), "
        f"{len(manifest.without_code)} sin código corto"
    )
    click.echo(
        f"carpetas: ok={folders.count(True)} faltan={folders.count(False)} "
        f"sin verificar={folders.count(None)}"
    )
    click.echo(f"manifest guardado en {store.path}")


@types_group.command(name="discover")
@_CONFIG_OPTION
@_MANIFEST_OPTION
@click.option(
    "--no-verify-folders",
    "no_verify_folders",
    is_flag=True,
    default=False,
    help="No chequea contra el server que cada carpeta derivada exista (más rápido).",
)
@click.option(
    "--force",
    "force",
    is_flag=True,
    default=False,
    help="Descubre de cero aunque el manifest tenga tipos ya revisados.",
)
def discover_command(
    config_path: Path, manifest_path: Path | None, no_verify_folders: bool, force: bool
) -> None:
    """Descubre los tipos del servidor y escribe el manifest desde cero.

    Ojo: pisa las decisiones que ya hayas tomado. Si el manifest tiene
    tipos revisados, el comando se planta salvo que le pases ``--force``;
    lo que querés casi siempre es ``types update``.
    """
    config, secrets = _load(config_path)
    store = _store(config, manifest_path)
    _refuse_if_reviewed(store, force)
    manifest = _discovery(config, secrets).discover(
        service_url=config.cmis.base_url,
        repository_id=config.cmis.repo_id,
        verify_folders=not no_verify_folders,
        on_progress=_echo_progress,
    )
    store.save(manifest)
    _echo_discover_summary(manifest, store)


# ---------------------------------------------------------------------------
# types show
# ---------------------------------------------------------------------------


def _limits(prop: CmPropertyDef) -> str:
    parts = []
    if prop.required:
        parts.append("req")
    if prop.max_length is not None:
        parts.append(f"len={prop.max_length}")
    if prop.choices:
        parts.append(f"{len(prop.choices)} opciones")
    return " ".join(parts) or "-"


def _property_rows(entry: CmTypeEntry) -> list[list[str]]:
    return [
        [
            prop.id,
            prop.property_type or "-",
            prop.cardinality or "-",
            prop.updatability or "-",
            _limits(prop),
            truncate(prop.default_value or "-", 20),
            entry.decisions.get(prop.id, "-"),
            truncate(prop.display_name or "-", 30),
        ]
        for prop in entry.properties
    ]


def _echo_entry(entry: CmTypeEntry) -> None:
    """Ficha completa de un tipo: encabezado + tabla de propiedades."""
    click.echo(f"ID corto: {entry.id_corto}")
    click.echo(f"type_id: {entry.type_id}")
    click.echo(f"local_name: {entry.local_name}")
    click.echo(f"display_name: {entry.display_name}")
    click.echo(f"carpeta: {entry.folder} ({_FOLDER_MARK[entry.folder_ok]}, {entry.folder_source})")
    click.echo(f"revisado: {'sí' if entry.reviewed else 'no'}")
    click.echo(f"missing_on_server: {'sí' if entry.missing_on_server else 'no'}")
    click.echo(f"changes: {len(entry.changes)}")
    for line in entry.changes:
        click.echo(f"  {line}")
    click.echo("")
    click.echo(render_table(_SHOW_HEADERS, _property_rows(entry)))


@types_group.command(name="show")
@click.argument("id_corto", metavar="IDCM", type=str)
@_OPTIONAL_CONFIG_OPTION
@_MANIFEST_OPTION
@click.option(
    "--live",
    "live",
    is_flag=True,
    default=False,
    help="Lee el tipo del servidor en vez del manifest (requiere --config).",
)
def show_command(
    id_corto: str, config_path: Path | None, manifest_path: Path | None, live: bool
) -> None:
    """Muestra la ficha de un tipo: propiedades, límites y decisiones."""
    if live:
        if config_path is None:
            raise click.ClickException("--live necesita --config para hablar con el servidor")
        config, secrets = _load(config_path)
        manifest = _fetch_live(config, secrets)
    else:
        manifest = _read(_store(_config_or_none(config_path), manifest_path))
    _echo_entry(_entry_or_fail(manifest, id_corto))


# ---------------------------------------------------------------------------
# types diff
# ---------------------------------------------------------------------------


@types_group.command(name="diff")
@_CONFIG_OPTION
@_MANIFEST_OPTION
def diff_command(config_path: Path, manifest_path: Path | None) -> None:
    """Compara el manifest local contra lo que hoy publica el servidor.

    Exit 1 si hay diferencias (sirve en un cron o un pre-flight), 0 si el
    manifest está al día.
    """
    config, secrets = _load(config_path)
    local = _read(_store(config, manifest_path))
    diff = diff_manifest(local, _fetch_live(config, secrets))
    if diff.is_empty:
        click.echo("types diff: sin diferencias")
        return
    click.echo(diff.render())
    sys.exit(1)


# ---------------------------------------------------------------------------
# types update
# ---------------------------------------------------------------------------


def _touched(local: CmTypeManifest, merged: CmTypeManifest) -> list[tuple[str, str]]:
    """Los tipos que el merge movió, con el motivo para mostrarle al operador."""
    out: list[tuple[str, str]] = []
    for code in sorted(merged.types):
        after = merged.types[code]
        before = local.types.get(code)
        if before is None:
            out.append((code, "nuevo en el servidor"))
        elif after.missing_on_server and not before.missing_on_server:
            out.append((code, "el servidor ya no lo publica"))
        elif after.changes != before.changes or after.reviewed != before.reviewed:
            out.append((code, f"{len(after.changes)} cambio(s)"))
    return out


@types_group.command(name="update")
@_CONFIG_OPTION
@_MANIFEST_OPTION
@click.option(
    "--only",
    "only",
    multiple=True,
    metavar="IDCM",
    help="Acota el merge a estos ID cortos. Repetible. Default: todos.",
)
def update_command(config_path: Path, manifest_path: Path | None, only: tuple[str, ...]) -> None:
    """Trae los cambios del servidor conservando tus decisiones.

    Las propiedades que sobreviven mantienen su ``usar``/``omitir``, las
    nuevas entran con la decisión sugerida y el tipo vuelve a
    ``reviewed=False`` con la lista de cambios para que la mires.
    """
    config, secrets = _load(config_path)
    store = _store(config, manifest_path)
    local = _read(store)
    merged = apply_diff(local, _fetch_live(config, secrets), set(only) or None)
    touched = _touched(local, merged)
    store.save(merged)
    if not touched:
        click.echo("types update: sin cambios")
        return
    click.echo(f"types update: {len(touched)} tipo(s) actualizado(s)")
    for code, reason in touched:
        click.echo(f"  {code}: {reason}")
        for line in merged.types[code].changes:
            click.echo(f"    {line}")


# ---------------------------------------------------------------------------
# types review
# ---------------------------------------------------------------------------


def _resolve_prop(entry: CmTypeEntry, token: str) -> str:
    """``BAC_CIF`` o ``clbNonGroup.BAC_CIF`` → el id de wire de la propiedad."""
    for prop in entry.properties:
        if token in (prop.id, canonical_name(prop.id)):
            return prop.id
    click.echo(f"el tipo {entry.id_corto} no tiene la propiedad escribible {token}", err=True)
    valid = ", ".join(sorted(canonical_name(p.id) for p in entry.properties)) or "(ninguna)"
    click.echo(f"válidas: {valid}", err=True)
    sys.exit(1)


def _apply_decisions(
    manifest: CmTypeManifest, entry: CmTypeEntry, use: tuple[str, ...], omit: tuple[str, ...]
) -> CmTypeManifest:
    """Resuelve TODOS los tokens antes de tocar nada: o entra todo, o nada."""
    resolved = [(_resolve_prop(entry, t), DECISION_USE) for t in use]
    resolved += [(_resolve_prop(entry, t), DECISION_OMIT) for t in omit]
    for prop_id, decision in resolved:
        manifest = set_decision(manifest, entry.id_corto, prop_id, decision)
    return manifest


@types_group.command(name="review")
@click.argument("id_corto", metavar="IDCM", type=str)
@_OPTIONAL_CONFIG_OPTION
@_MANIFEST_OPTION
@click.option(
    "--use",
    "use",
    multiple=True,
    metavar="PROP",
    help="Manda esta propiedad al wire. Acepta id completo o nombre canónico. Repetible.",
)
@click.option(
    "--omit",
    "omit",
    multiple=True,
    metavar="PROP",
    help="No manda esta propiedad (gana el default del servidor). Repetible.",
)
@click.option(
    "--folder",
    "folder",
    default=None,
    metavar="PATH",
    help="Fija la carpeta destino a mano; deja de derivarse del localName.",
)
@click.option(
    "--done",
    "done",
    is_flag=True,
    default=False,
    help="Marca el tipo como revisado y limpia sus cambios pendientes.",
)
def review_command(
    id_corto: str,
    config_path: Path | None,
    manifest_path: Path | None,
    use: tuple[str, ...],
    omit: tuple[str, ...],
    folder: str | None,
    done: bool,
) -> None:
    """Edita las decisiones de un tipo y lo firma. Corre offline."""
    store = _store(_config_or_none(config_path), manifest_path)
    manifest = _read(store)
    entry = _entry_or_fail(manifest, id_corto)
    manifest = _apply_decisions(manifest, entry, use, omit)
    if folder is not None:
        manifest = set_folder(manifest, id_corto, folder)
    if done:
        manifest = mark_reviewed(manifest, id_corto)
    store.save(manifest)
    final = manifest.types[id_corto]
    click.echo(
        f"{id_corto}: carpeta {final.folder} ({final.folder_source}), "
        f"revisado={'sí' if final.reviewed else 'no'}"
    )
    rows = [[p.id, final.decisions.get(p.id, "-")] for p in final.properties]
    click.echo(render_table(["PROPIEDAD", "DECISION"], rows))


# 145 REQ-005: `check` lo agrega el paquete 4
