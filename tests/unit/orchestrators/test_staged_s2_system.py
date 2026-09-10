"""S2 resuelve el mapping con el sistema del trigger (145 REQ-001).

Escenario 2 del spec 145: ``MapeoRVI_CM.csv`` con ``("", "0001") → DC01``
y ``("RVI2", "0001") → DC02``. Un trigger con sistema ``rvi2`` tiene que
terminar en DC02; uno con sistema ``X`` cae al comodín, DC01.

El SUT es ``StagedPipeline._s2_one``, con un ``MappingService`` REAL en
modo manifest (Principio VI: no `mock`ees el colaborador que el SUT
consume si podés darle uno real y determinístico).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from cmcourier.domain.cm_types import CmTypeEntry, CmTypeManifest
from cmcourier.domain.models import ClientTrigger, RVABREPDocument, RvabrepRowTrigger
from cmcourier.orchestrators.staged import StagedPipeline, _StageItem
from cmcourier.services.mapping import MappingService

pytestmark = pytest.mark.unit


class _FakeSource:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def get_all(self) -> Iterator[dict[str, object]]:
        return iter(self._rows)

    def close(self) -> None:  # pragma: no cover — completitud de protocolo
        pass


def _entry(id_corto: str) -> CmTypeEntry:
    return CmTypeEntry(
        id_corto=id_corto,
        type_id=f"$t!-2_BAC_{id_corto}v-1",
        local_name=f"BAC_{id_corto}",
        display_name=f"{id_corto} - Clase",
        folder=f"/$type/BAC_{id_corto}",
    )


def _mapping_service() -> MappingService:
    manifest = CmTypeManifest(
        service_url="http://cm.test/browser",
        repository_id="repo",
        discovered_at="2026-01-01T00:00:00Z",
        types={e.id_corto: e for e in (_entry("DC01"), _entry("DC02"))},
    )
    rows: list[dict[str, object]] = [
        {"IDSistema": "", "IDRVI": "0001", "IDCM": "DC01"},
        {"IDSistema": "RVI2", "IDRVI": "0001", "IDCM": "DC02"},
    ]
    return MappingService(_FakeSource(rows), type_manifest=manifest)  # type: ignore[arg-type]


def _pipeline() -> StagedPipeline:
    tracking = MagicMock()
    tracking.is_stage_done.return_value = True
    return StagedPipeline(
        trigger_strategy=MagicMock(),
        indexing_service=MagicMock(),
        mapping_service=_mapping_service(),
        metadata_service=MagicMock(),
        assembler=MagicMock(),
        uploader=MagicMock(),
        tracking_store=tracking,
        workers=1,
    )


def _doc() -> RVABREPDocument:
    return RVABREPDocument(
        system_code="1",
        txn_num="TXN_S2",
        index1="1",
        index2="1",
        index3="",
        index4="",
        index5="",
        index6="",
        index7="0001",
        image_type="O",
        image_path="x",
        file_name="DOC.pdf",
        creation_date=datetime(2026, 1, 1),  # noqa: DTZ001
        last_view_date=None,
        total_pages=1,
        delete_code="",
    )


def _resolve(trigger: object) -> str:
    pipeline = _pipeline()
    item = _StageItem(trigger=trigger, document=_doc())  # type: ignore[arg-type]
    survivor, counted_failure = pipeline._s2_one(item, "B1", pipeline._metrics)
    assert survivor is not None, "S2 no debería descartar el item"
    assert counted_failure is False
    assert survivor.mapping is not None
    return survivor.mapping.id_corto


def test_s2_uses_the_trigger_system() -> None:
    assert _resolve(ClientTrigger(shortname="SN", cif="1", system_id="rvi2")) == "DC02"


def test_s2_falls_back_to_the_wildcard_for_an_unknown_system() -> None:
    assert _resolve(ClientTrigger(shortname="SN", cif="1", system_id="X")) == "DC01"


def test_s2_reads_the_system_from_a_row_trigger() -> None:
    trigger = RvabrepRowTrigger(row={"ABABCD": "SN", "ABAACD": "RVI2"})
    assert _resolve(trigger) == "DC02"
