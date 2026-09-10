"""Tests del descubrimiento de tipos CM (145 REQ-003).

El uploader se reemplaza por un doble sin red. Lo que se verifica acá es
la ORQUESTACIÓN: las fases de progreso, la verificación de carpetas en
paralelo y la tolerancia a que una carpeta falle (no aborta la corrida).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cmcourier.domain.cm_types import FOLDER_DERIVED
from cmcourier.services.sync_progress import SyncProgress
from cmcourier.services.type_discovery import TypeDiscoveryService

pytestmark = pytest.mark.unit

_FIXTURES = Path(__file__).parent.parent.parent / "fixtures" / "cmis"
_FIXTURE = _FIXTURES / "type_descendants_sample.json"


def _sample_tree() -> list[dict[str, Any]]:
    with _FIXTURE.open(encoding="utf-8") as fh:
        data: list[dict[str, Any]] = json.load(fh)
    return data


class _FakeUploader:
    """Doble del ``CmisUploader``: devuelve el árbol del fixture y contesta
    carpetas según un dict, o revienta para las que estén en ``boom``."""

    def __init__(
        self,
        *,
        tree: list[dict[str, Any]] | None = None,
        folders: Mapping[str, bool] | None = None,
        boom: frozenset[str] = frozenset(),
    ) -> None:
        self._tree = _sample_tree() if tree is None else tree
        self._folders = folders or {}
        self._boom = boom
        self.descendant_calls: list[bool] = []
        self.folder_calls: list[str] = []

    def get_type_descendants(
        self, include_property_definitions: bool = True
    ) -> Sequence[Mapping[str, Any]]:
        self.descendant_calls.append(include_property_definitions)
        return self._tree

    def verify_folder_exists(self, folder_path: str) -> bool:
        self.folder_calls.append(folder_path)
        if folder_path in self._boom:
            raise ConnectionError(f"la red se cayó para {folder_path}")
        return self._folders.get(folder_path, True)


def _clock() -> datetime:
    return datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)


def _service(uploader: _FakeUploader, **kw: Any) -> TypeDiscoveryService:
    return TypeDiscoveryService(uploader, clock=_clock, **kw)  # type: ignore[arg-type]


def _discover(service: TypeDiscoveryService, **kw: Any) -> Any:
    return service.discover(service_url="http://cm/browser", repository_id="repo", **kw)


class TestDiscover145:
    def test_arma_el_manifest_con_la_metadata_del_repositorio(self) -> None:
        manifest = _discover(_service(_FakeUploader()), verify_folders=False)
        assert set(manifest.types) == {"PT95", "PT55", "PT55.2"}
        assert manifest.service_url == "http://cm/browser"
        assert manifest.repository_id == "repo"
        assert manifest.discovered_at == "2026-03-04T05:06:07+00:00"
        assert manifest.without_code == (("$t!-2_CmisDocumentv-1", "Default Document Type"),)

    def test_pide_las_definiciones_de_propiedades(self) -> None:
        uploader = _FakeUploader()
        _discover(_service(uploader), verify_folders=False)
        assert uploader.descendant_calls == [True]

    def test_sin_verificar_carpetas_no_toca_al_servidor(self) -> None:
        uploader = _FakeUploader()
        manifest = _discover(_service(uploader), verify_folders=False)
        assert uploader.folder_calls == []
        assert all(e.folder_ok is None for e in manifest.types.values())
        assert all(e.folder_source == FOLDER_DERIVED for e in manifest.types.values())

    def test_verifica_cada_carpeta_derivada(self) -> None:
        uploader = _FakeUploader(folders={"/$type/BAC_01_01_02_04_01_18": False})
        manifest = _discover(_service(uploader))
        assert sorted(uploader.folder_calls) == sorted(e.folder for e in manifest.types.values())
        assert manifest.types["PT95"].folder_ok is False
        assert manifest.types["PT55"].folder_ok is True

    def test_una_carpeta_que_falla_queda_sin_verificar_y_avisa(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        uploader = _FakeUploader(boom=frozenset({"/$type/BAC_01_01_02_04_01_18"}))
        with caplog.at_level(logging.WARNING, logger="cmcourier.services.type_discovery"):
            manifest = _discover(_service(uploader, workers=4))
        assert manifest.types["PT95"].folder_ok is None
        assert manifest.types["PT55"].folder_ok is True
        assert manifest.types["PT55.2"].folder_ok is True
        assert any("BAC_01_01_02_04_01_18" in r.getMessage() for r in caplog.records)

    def test_secuencia_de_fases_de_progreso(self) -> None:
        eventos: list[SyncProgress] = []
        _discover(_service(_FakeUploader()), on_progress=eventos.append)
        fases = [(e.phase, e.done, e.total) for e in eventos]
        assert fases[0] == ("descargando tipos", 0, 0)
        assert fases[1:6] == [("procesando tipos", k, 4) for k in range(5)]
        assert fases[6] == ("verificando carpetas", 0, 3)
        assert fases[7:] == [("verificando carpetas", k, 3) for k in (1, 2, 3)]

    def test_sin_carpetas_a_verificar_no_emite_la_tercera_fase(self) -> None:
        eventos: list[SyncProgress] = []
        _discover(_service(_FakeUploader()), verify_folders=False, on_progress=eventos.append)
        assert [e.phase for e in eventos if e.phase == "verificando carpetas"] == []

    def test_arbol_vacio_no_rompe(self) -> None:
        eventos: list[SyncProgress] = []
        manifest = _discover(_service(_FakeUploader(tree=[])), on_progress=eventos.append)
        assert dict(manifest.types) == {}
        assert ("procesando tipos", 0, 0) in [(e.phase, e.done, e.total) for e in eventos]

    def test_un_callback_roto_no_aborta_el_descubrimiento(self) -> None:
        def explota(_: SyncProgress) -> None:
            raise RuntimeError("la UI se murió")

        manifest = _discover(_service(_FakeUploader()), on_progress=explota)
        assert set(manifest.types) == {"PT95", "PT55", "PT55.2"}

    def test_clock_por_defecto_es_utc(self) -> None:
        manifest = TypeDiscoveryService(_FakeUploader()).discover(  # type: ignore[arg-type]
            service_url="u", repository_id="r", verify_folders=False
        )
        assert manifest.discovered_at.endswith("+00:00")


class TestFetchLive145:
    def test_no_verifica_carpetas(self) -> None:
        uploader = _FakeUploader()
        manifest = _service(uploader).fetch_live(service_url="u", repository_id="r")
        assert uploader.folder_calls == []
        assert set(manifest.types) == {"PT95", "PT55", "PT55.2"}

    def test_reporta_progreso(self) -> None:
        eventos: list[SyncProgress] = []
        _service(_FakeUploader()).fetch_live(
            service_url="u", repository_id="r", on_progress=eventos.append
        )
        assert [e.phase for e in eventos][0] == "descargando tipos"
        assert "verificando carpetas" not in [e.phase for e in eventos]
