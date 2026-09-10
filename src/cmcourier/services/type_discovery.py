"""Descubrimiento de tipos CM contra el servidor (145 REQ-003).

Baja el árbol de ``typeDescendants`` entero, lo convierte en manifest con
:mod:`cmcourier.services.type_manifest` y —opcionalmente— verifica en
paralelo que cada carpeta derivada exista de verdad.

Progreso en tres fases con el :class:`SyncProgress` de 144:
``"descargando tipos"`` (0/0, no sabemos cuánto viene) →
``"procesando tipos"`` k/N → ``"verificando carpetas"`` k/N.

Una carpeta que no se puede verificar NO aborta la corrida: queda
``folder_ok=None`` con un WARNING. Descubrir 368 tipos y perderlo todo
porque una carpeta dio timeout sería una pésima idea.
"""

from __future__ import annotations

__all__ = ["TypeDiscoveryService"]

import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import UTC, datetime

from cmcourier.domain.cm_types import CmTypeEntry, CmTypeManifest
from cmcourier.domain.ports import IUploader
from cmcourier.services.sync_progress import ProgressEmitter, SyncProgress
from cmcourier.services.type_manifest import build_manifest, flatten_types

_log = logging.getLogger(__name__)

_PHASE_DOWNLOAD = "descargando tipos"
_PHASE_PARSE = "procesando tipos"
_PHASE_FOLDERS = "verificando carpetas"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class TypeDiscoveryService:
    """Arma el manifest de tipos CM preguntándole al servidor (145 REQ-003)."""

    def __init__(
        self,
        uploader: IUploader,
        *,
        workers: int = 8,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._uploader = uploader
        self._workers = max(1, workers)
        self._clock = clock or _utc_now

    def discover(
        self,
        *,
        service_url: str,
        repository_id: str,
        verify_folders: bool = True,
        on_progress: Callable[[SyncProgress], None] | None = None,
    ) -> CmTypeManifest:
        """Descubre todos los tipos y devuelve el manifest recién armado.

        Con ``verify_folders=True`` (default) además chequea contra el
        server que cada carpeta derivada exista; las que no se pudieron
        verificar quedan en ``folder_ok=None``."""
        emit = ProgressEmitter(on_progress)
        emit(_PHASE_DOWNLOAD, 0, 0)
        nodes = self._uploader.get_type_descendants(True)
        # Aplanar acá (y no dentro de build_manifest) es lo que nos deja
        # emitir k/N desde el primer evento: antes del flatten no sabemos N.
        flat = flatten_types(nodes)
        emit(_PHASE_PARSE, 0, len(flat))
        manifest = build_manifest(
            flat,
            service_url=service_url,
            repository_id=repository_id,
            discovered_at=self._clock().isoformat(),
            on_type=lambda done, total: emit(_PHASE_PARSE, done, total),
        )
        if not verify_folders or not manifest.types:
            return manifest
        return self._verify_folders(manifest, emit)

    def fetch_live(
        self,
        *,
        service_url: str,
        repository_id: str,
        on_progress: Callable[[SyncProgress], None] | None = None,
    ) -> CmTypeManifest:
        """La foto fresca del servidor SIN verificar carpetas — lo que usan
        ``types diff`` y ``types update``: comparar no necesita red extra."""
        return self.discover(
            service_url=service_url,
            repository_id=repository_id,
            verify_folders=False,
            on_progress=on_progress,
        )

    def _verify_folders(self, manifest: CmTypeManifest, emit: ProgressEmitter) -> CmTypeManifest:
        """Verifica en paralelo las carpetas de todos los tipos del manifest.

        El pool es sólo para la espera de red; el progreso se emite desde
        ESTE hilo (el que llamó al servicio), como manda 144."""
        entries = list(manifest.types.values())
        total = len(entries)
        emit(_PHASE_FOLDERS, 0, total)
        checked: dict[str, bool | None] = {}
        done = 0
        with ThreadPoolExecutor(
            max_workers=min(self._workers, max(1, total)), thread_name_prefix="cm-folder"
        ) as pool:
            futures: dict[Future[bool | None], CmTypeEntry] = {
                pool.submit(self._check_folder, entry): entry for entry in entries
            }
            for future in as_completed(futures):
                checked[futures[future].id_corto] = future.result()
                done += 1
                emit(_PHASE_FOLDERS, done, total)
        types = {
            code: replace(entry, folder_ok=checked.get(code))
            for code, entry in manifest.types.items()
        }
        return replace(manifest, types=types)

    def _check_folder(self, entry: CmTypeEntry) -> bool | None:
        """Un chequeo de carpeta. Nunca levanta: un fallo de red vuelve
        como ``None`` (sin verificar) para no tirar abajo el descubrimiento."""
        try:
            return bool(self._uploader.verify_folder_exists(entry.folder))
        except Exception as exc:  # noqa: BLE001 — una carpeta rota no aborta 368 tipos
            _log.warning(
                "types discover: no se pudo verificar la carpeta %s de %s: %s",
                entry.folder,
                entry.id_corto,
                exc,
            )
            return None
