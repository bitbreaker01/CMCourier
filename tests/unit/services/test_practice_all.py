"""158 — la lógica pura de "probar TODOS los tipos".

La unión de campos distintos (REQ-002), el armado de propiedades por tipo
con los wire ids de ESE tipo, el único PDF marcado reusado como cuerpo
(REQ-003), el resultado tipo por tipo con la razón CRUDA de CM (REQ-004),
el alcance mapeado vs. todo-el-manifest (REQ-001) y la limpieza en lote
que sobrevive a un borrado fallido (REQ-005).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from types import MappingProxyType

import pytest

from cmcourier.adapters.sources import TabularDataSource
from cmcourier.domain.cm_types import CmPropertyDef, CmTypeEntry, CmTypeManifest
from cmcourier.domain.models import CMMapping, StagedFile
from cmcourier.services.mapping import MappingService
from cmcourier.services.practice_all import (
    PracticeType,
    build_marked_pdf,
    build_practice_types,
    delete_practice_objects,
    distinct_fields,
    run_practice_all,
)
from cmcourier.services.practice_upload import RawResponse

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 8, 31, 10, 0, 0)


def _ptype(
    code: str,
    fields: tuple[str, ...],
    wire: dict[str, str] | None,
    *,
    mapped: bool = True,
) -> PracticeType:
    mapping = CMMapping(
        clase_id=code,
        id_rvi="",
        id_corto=code,
        clase_name=f"{code} - Tipo {code}",
        required_metadata_fields=fields,
        cmis_type=f"D:cm:{code}",
        cmis_folder=f"/$type/{code}",
        cmis_property_ids=MappingProxyType(wire) if wire else None,
    )
    return PracticeType(mapping=mapping, mapped=mapped)


class _FakeUploader:
    """Doble del puerto: falla / revienta el upload de tipos elegidos y
    registra cada upload/borrado."""

    def __init__(
        self,
        *,
        fail: tuple[str, ...] = (),
        boom: tuple[str, ...] = (),
        delete_fail: tuple[str, ...] = (),
    ) -> None:
        self.uploads: list[dict[str, object]] = []
        self.deleted: list[str] = []
        self._fail, self._boom, self._delete_fail = set(fail), set(boom), set(delete_fail)

    def upload_raw(
        self,
        file: StagedFile,
        folder_path: str,
        object_type_id: str,
        document_name: str,
        mime_type: str,
        properties: Mapping[str, str],
    ) -> RawResponse:
        code = document_name.split("-")[1]
        self.uploads.append(
            {
                "name": document_name,
                "folder": folder_path,
                "object_type": object_type_id,
                "mime": mime_type,
                "props": dict(properties),
                "bytes": file.path.read_bytes(),
            }
        )
        if code in self._boom:
            raise RuntimeError("se cayó la red")
        if code in self._fail:
            return RawResponse(
                400,
                "Bad Request",
                {},
                '{"message": "propiedad BAC_X no existe en el tipo"}',
                5,
                "c",
            )
        body = f'{{"succinctProperties": {{"cmis:objectId": "obj-{code}"}}}}'
        return RawResponse(201, "Created", {"content-type": "application/json"}, body, 5, "c")

    def delete_object(self, object_id: str) -> RawResponse:
        self.deleted.append(object_id)
        if object_id in self._delete_fail:
            return RawResponse(500, "Server Error", {}, '{"message": "no se pudo borrar"}', 5, "c")
        return RawResponse(200, "OK", {}, '{"ok": true}', 3, "c")


# ---------------------------------------------------------------------------
# REQ-002 — unión de campos distintos
# ---------------------------------------------------------------------------


class TestDistinctFields:
    def test_shared_field_is_asked_once_with_its_type_count(self) -> None:
        aa = _ptype("AA", ("BAC_CIF",), {"BAC_CIF": "clb.BAC_CIF"})
        bb = _ptype("BB", ("BAC_CIF", "BAC_Nombre"), {"BAC_CIF": "clb.BAC_CIF"})
        prompts = distinct_fields((aa, bb))

        assert [p.name for p in prompts] == ["BAC_CIF", "BAC_Nombre"]
        counts = {p.name: p.type_count for p in prompts}
        assert counts == {"BAC_CIF": 2, "BAC_Nombre": 1}

    def test_field_repeated_within_one_type_counts_that_type_once(self) -> None:
        # dos wire ids que canonizan al mismo nombre en un solo tipo.
        dup = _ptype("AA", ("BAC_CIF", "BAC_CIF"), {"BAC_CIF": "clb.BAC_CIF"})
        prompts = distinct_fields((dup,))
        assert [p.type_count for p in prompts] == [1]


# ---------------------------------------------------------------------------
# REQ-003 / REQ-004 — un solo PDF reusado; resultado por tipo
# ---------------------------------------------------------------------------


class TestRunPracticeAll:
    def _content(self):  # type: ignore[no-untyped-def]
        return build_marked_pdf(operator="jdoe", station="PC-1", environment="staging", now=_NOW)

    def test_one_pdf_reused_across_uploads_with_the_mark_embedded(self, tmp_path: Path) -> None:
        types = (
            _ptype("AA", ("BAC_CIF",), {"BAC_CIF": "clb.BAC_CIF"}),
            _ptype("BB", ("BAC_Nombre",), {"BAC_Nombre": "clb.BAC_Nombre"}),
            _ptype("CC", (), None),
        )
        uploader = _FakeUploader()
        list(
            run_practice_all(
                types, {}, uploader, workdir=tmp_path, now=_NOW, content=self._content()
            )
        )
        bodies = {u["bytes"] for u in uploader.uploads}
        assert len(bodies) == 1  # los MISMOS bytes en los 3 uploads
        assert b"PRUEBA DE CARGA" in next(iter(bodies))
        assert b"jdoe" in next(iter(bodies))

    def test_per_type_properties_use_that_types_wire_ids(self, tmp_path: Path) -> None:
        types = (
            _ptype("AA", ("BAC_CIF",), {"BAC_CIF": "clbA.BAC_CIF"}),
            _ptype("BB", ("BAC_CIF",), {"BAC_CIF": "clbB.BAC_CIF"}),
        )
        values = {"BAC_CIF": " 123 ", "BAC_Nombre": "Juan"}
        uploader = _FakeUploader()
        list(
            run_practice_all(
                types, values, uploader, workdir=tmp_path, now=_NOW, content=self._content()
            )
        )
        by_code = {u["name"].split("-")[1]: u["props"] for u in uploader.uploads}
        assert by_code["AA"] == {"clbA.BAC_CIF": "123"}
        assert by_code["BB"] == {"clbB.BAC_CIF": "123"}

    def test_mixed_run_reports_ok_and_the_raw_failure_reason(self, tmp_path: Path) -> None:
        types = (
            _ptype("AA", ("BAC_CIF",), {"BAC_CIF": "clb.BAC_CIF"}),
            _ptype("BB", ("BAC_X",), {"BAC_X": "clb.BAC_X"}),
            _ptype("CC", ("BAC_CIF",), {"BAC_CIF": "clb.BAC_CIF"}),
        )
        uploader = _FakeUploader(fail=("BB",))
        outcomes = list(
            run_practice_all(
                types,
                {"BAC_CIF": "1", "BAC_X": "x"},
                uploader,
                workdir=tmp_path,
                now=_NOW,
                content=self._content(),
            )
        )
        by_code = {o.id_corto: o for o in outcomes}
        assert by_code["AA"].ok and by_code["AA"].reason == "201 Created"
        assert by_code["CC"].ok and by_code["CC"].object_id == "obj-CC"
        assert not by_code["BB"].ok
        assert "400" in by_code["BB"].reason
        assert "BAC_X no existe" in by_code["BB"].reason
        assert by_code["BB"].object_id is None

    def test_transport_error_on_one_type_does_not_abort_the_rest(self, tmp_path: Path) -> None:
        types = (
            _ptype("AA", (), None),
            _ptype("BB", (), None),
            _ptype("CC", (), None),
        )
        uploader = _FakeUploader(boom=("BB",))
        outcomes = list(
            run_practice_all(
                types, {}, uploader, workdir=tmp_path, now=_NOW, content=self._content()
            )
        )
        by_code = {o.id_corto: o for o in outcomes}
        assert by_code["AA"].ok and by_code["CC"].ok
        assert not by_code["BB"].ok
        assert "RuntimeError" in by_code["BB"].reason

    def test_cooperative_stop_halts_early(self, tmp_path: Path) -> None:
        types = tuple(_ptype(c, (), None) for c in ("AA", "BB", "CC"))
        uploader = _FakeUploader()
        seen = {"n": 0}

        def stop() -> bool:
            done = seen["n"] >= 1
            seen["n"] += 1
            return done

        outcomes = list(
            run_practice_all(
                types,
                {},
                uploader,
                workdir=tmp_path,
                now=_NOW,
                content=self._content(),
                should_stop=stop,
            )
        )
        assert len(outcomes) == 1  # cortó tras el primero

    def test_service_never_touches_tracking(self, tmp_path: Path) -> None:
        # El puerto sólo expone upload_raw / delete_object: no hay forma de
        # escribir en tracking ni idempotencia — invisible al pipeline.
        types = (_ptype("AA", (), None),)
        uploader = _FakeUploader()
        list(
            run_practice_all(
                types, {}, uploader, workdir=tmp_path, now=_NOW, content=self._content()
            )
        )
        assert not hasattr(uploader, "mark_stage_done")
        assert len(uploader.uploads) == 1


# ---------------------------------------------------------------------------
# REQ-005 — limpieza en lote
# ---------------------------------------------------------------------------


class TestDeletePracticeObjects:
    def test_deletes_all_collected_ids(self) -> None:
        uploader = _FakeUploader()
        outcomes = list(delete_practice_objects(["obj-A", "obj-B", "obj-C"], uploader))
        assert uploader.deleted == ["obj-A", "obj-B", "obj-C"]
        assert all(o.ok for o in outcomes)

    def test_individual_delete_failure_does_not_abort_the_rest(self) -> None:
        uploader = _FakeUploader(delete_fail=("obj-B",))
        outcomes = list(delete_practice_objects(["obj-A", "obj-B", "obj-C"], uploader))
        assert uploader.deleted == ["obj-A", "obj-B", "obj-C"]
        survivors = [o.object_id for o in outcomes if not o.ok]
        assert survivors == ["obj-B"]


# ---------------------------------------------------------------------------
# REQ-001 — alcance: mapeados vs. todo el manifest
# ---------------------------------------------------------------------------


def _prop(pid: str) -> CmPropertyDef:
    return CmPropertyDef(
        id=pid,
        display_name=pid,
        property_type="string",
        cardinality="single",
        updatability="readwrite",
        required=True,
        max_length=None,
        default_value=None,
        inherited=False,
        choices=(),
    )


def _entry(code: str, prop_ids: tuple[str, ...], *, use: bool = True) -> CmTypeEntry:
    props = tuple(_prop(p) for p in prop_ids)
    decisions = {p.id: ("usar" if use else "omitir") for p in props}
    return CmTypeEntry(
        id_corto=code,
        type_id=f"D:cm:{code}",
        local_name=code,
        display_name=f"{code} - Tipo {code}",
        folder=f"/$type/{code}",
        properties=props,
        decisions=decisions,
    )


def _manifest() -> CmTypeManifest:
    return CmTypeManifest(
        service_url="",
        repository_id="",
        discovered_at="",
        types={
            "AA": _entry("AA", ("clb.BAC_CIF",)),
            "BB": _entry("BB", ("clb.BAC_CIF", "clb.BAC_Nombre")),
            "CC": _entry("CC", ("clb.BAC_X",)),  # no mapeado, con usar
            "DD": _entry("DD", ("clb.BAC_Y",), use=False),  # no mapeado, sin usar
        },
    )


def _service(tmp_path: Path) -> MappingService:
    csv = tmp_path / "MapeoRVI_CM.csv"
    csv.write_text("IDSistema,IDRVI,IDCM\n,R1,AA\n,R2,AA\n,R3,BB\n")
    source = TabularDataSource(csv)
    try:
        return MappingService(source, type_manifest=_manifest())
    finally:
        source.close()


class TestBuildPracticeTypes:
    def test_default_scope_is_the_mapped_types_deduped_by_idcm(self, tmp_path: Path) -> None:
        types = build_practice_types(_service(tmp_path), _manifest())
        assert [t.id_corto for t in types] == ["AA", "BB"]
        assert all(t.mapped for t in types)

    def test_include_unmapped_adds_manifest_types_with_a_usar_property(
        self, tmp_path: Path
    ) -> None:
        types = build_practice_types(_service(tmp_path), _manifest(), include_unmapped=True)
        codes = [t.id_corto for t in types]
        assert codes == ["AA", "BB", "CC"]  # DD queda afuera: sin propiedad `usar`
        assert next(t for t in types if t.id_corto == "CC").mapped is False

    def test_unmapped_type_carries_its_wire_ids(self, tmp_path: Path) -> None:
        types = build_practice_types(_service(tmp_path), _manifest(), include_unmapped=True)
        cc = next(t for t in types if t.id_corto == "CC")
        assert cc.fields == ("BAC_X",)
        assert dict(cc.mapping.cmis_property_ids or {}) == {"BAC_X": "clb.BAC_X"}
