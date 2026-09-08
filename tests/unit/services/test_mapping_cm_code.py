"""141 REQ-001 (E1) — índice de mapping por código CM (``IDCM``).

``MappingService`` gana ``get_by_cm_code`` y ``cm_codes`` sin cambiar el
índice por ``IDRVI`` ni el descarte de filas.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from cmcourier.adapters.sources import TabularDataSource
from cmcourier.services.mapping import MappingService

pytestmark = pytest.mark.unit

_SAMPLE = Path(__file__).parent.parent.parent.parent / "sample"
_RVI_CM = _SAMPLE / "MapeoRVI_CM.csv"
_METADATOS = _SAMPLE / "MetadatosCM.csv"


@pytest.fixture
def sample_service() -> Iterator[MappingService]:
    rvi = TabularDataSource(_RVI_CM)
    metadatos = TabularDataSource(_METADATOS)
    try:
        yield MappingService(rvi, metadata_source=metadatos)
    finally:
        rvi.close()
        metadatos.close()


class TestGetByCmCode:
    def test_known_code_returns_rows(self, sample_service: MappingService) -> None:
        rows = sample_service.get_by_cm_code("CN01")
        assert rows
        assert all(m.id_corto == "CN01" for m in rows)
        assert rows[0].cmis_folder == "/cmcourier-staging/CN01"
        assert rows[0].id_rvi == "FB01"

    def test_strips_whitespace(self, sample_service: MappingService) -> None:
        assert sample_service.get_by_cm_code("  CN01 ") == sample_service.get_by_cm_code("CN01")

    def test_unknown_code_is_empty(self, sample_service: MappingService) -> None:
        assert sample_service.get_by_cm_code("ZZ99") == ()

    def test_required_metadata_comes_from_metadatos_csv(
        self, sample_service: MappingService
    ) -> None:
        (row,) = sample_service.get_by_cm_code("CN01")
        assert "CIF" in row.required_metadata_fields
        assert row.cmis_property_ids is not None
        assert row.cmis_property_ids["CIF"] == "cmcourier:BAC_CIF"

    def test_idrvi_index_unchanged(self, sample_service: MappingService) -> None:
        # El descarte de IDRVI duplicados no cambia: FB01 gana la primera fila.
        assert sample_service.get_mapping("FB01").id_corto == "CN01"


class TestCmCodes:
    def test_sorted_and_unique(self, sample_service: MappingService) -> None:
        codes = sample_service.cm_codes()
        assert codes == tuple(sorted(set(codes)))
        assert "CN01" in codes

    def test_every_code_resolves(self, sample_service: MappingService) -> None:
        # 141 antagonista I7: la cuenta literal (22 IDCM únicos en el CSV de
        # ``sample/``) evita que este test se vuelva una tautología que
        # compara el índice contra sí mismo.
        codes = sample_service.cm_codes()
        assert len(codes) == 22
        assert all(sample_service.get_by_cm_code(code) for code in codes)

    def test_every_idcm_in_the_sample_csv_resolves(self, sample_service: MappingService) -> None:
        """141 antagonista I3: ``cm_codes()`` tiene que cubrir TODO IDCM
        presente en ``sample/MapeoRVI_CM.csv``, incluidos los que llegan
        por una fila con ``IDRVI`` duplicado."""
        rows = [
            line.split(",")
            for line in _RVI_CM.read_text(encoding="utf-8").splitlines()[1:]
            if line.strip()
        ]
        csv_codes = {row[2].strip() for row in rows if row[2].strip()}
        assert csv_codes <= set(sample_service.cm_codes())
        assert "CJ02" in sample_service.cm_codes()
        assert sample_service.get_by_cm_code("CJ02") != ()


class TestMultipleRowsPerCode:
    """Varias filas ``IDRVI`` pueden apuntar al mismo ``IDCM``."""

    @pytest.fixture
    def service(self, tmp_path: Path) -> Iterator[MappingService]:
        rvi_path = tmp_path / "MapeoRVI_CM.csv"
        rvi_path.write_text(
            "IDRVI,IDCM,IDClaseDocumental,CMISType,CMISFolder\n"
            "AA01,XX99,01.01,D:tipoA,/carpeta/A\n"
            "AA02,XX99,01.02,D:tipoB,/carpeta/B\n"
            "AA03,YY01,01.03,,\n",
            encoding="utf-8",
        )
        meta_path = tmp_path / "MetadatosCM.csv"
        meta_path.write_text(
            "IDCorto,Metadato,Requerido,CMISPropertyId\n"
            "XX99,BAC_Nombre,Yes,cm:nombre\n"
            "XX99,BAC_Opcional,No,cm:opcional\n",
            encoding="utf-8",
        )
        rvi = TabularDataSource(rvi_path)
        metadatos = TabularDataSource(meta_path)
        try:
            yield MappingService(rvi, metadata_source=metadatos)
        finally:
            rvi.close()
            metadatos.close()

    def test_returns_all_rows_in_source_order(self, service: MappingService) -> None:
        rows = service.get_by_cm_code("XX99")
        assert [m.id_rvi for m in rows] == ["AA01", "AA02"]
        assert [m.cmis_type for m in rows] == ["D:tipoA", "D:tipoB"]
        assert [m.cmis_folder for m in rows] == ["/carpeta/A", "/carpeta/B"]

    def test_cm_codes_deduplicates(self, service: MappingService) -> None:
        assert service.cm_codes() == ("XX99", "YY01")

    def test_only_required_metadata(self, service: MappingService) -> None:
        rows = service.get_by_cm_code("XX99")
        assert rows[0].required_metadata_fields == ("BAC_Nombre",)


class TestDuplicateIdRviStillIndexesByCmCode:
    """141 antagonista I3: una fila con ``IDRVI`` duplicado se descartaba
    ANTES de llamar a ``_remember`` — perdía su ``IDCM`` del índice
    secundario aunque el ``IDCM`` fuera legítimo y único."""

    @pytest.fixture
    def service(self, tmp_path: Path) -> Iterator[MappingService]:
        rvi_path = tmp_path / "MapeoRVI_CM.csv"
        rvi_path.write_text(
            "IDRVI,IDCM,IDClaseDocumental,CMISType,CMISFolder\n"
            "FB01,CN01,01.01,D:tipoA,/carpeta/A\n"
            "FB01,CN02,01.02,D:tipoB,/carpeta/B\n",
            encoding="utf-8",
        )
        meta_path = tmp_path / "MetadatosCM.csv"
        meta_path.write_text(
            "IDCorto,Metadato,Requerido,CMISPropertyId\n"
            "CN01,BAC_Nombre,Yes,cm:nombre\n"
            "CN02,BAC_CIF,Yes,cm:cif\n",
            encoding="utf-8",
        )
        rvi = TabularDataSource(rvi_path)
        metadatos = TabularDataSource(meta_path)
        try:
            yield MappingService(rvi, metadata_source=metadatos)
        finally:
            rvi.close()
            metadatos.close()

    def test_both_cm_codes_are_indexed(self, service: MappingService) -> None:
        assert set(service.cm_codes()) == {"CN01", "CN02"}

    def test_each_code_resolves_its_own_row(self, service: MappingService) -> None:
        (cn01,) = service.get_by_cm_code("CN01")
        (cn02,) = service.get_by_cm_code("CN02")
        assert cn01.cmis_folder == "/carpeta/A"
        assert cn02.cmis_folder == "/carpeta/B"

    def test_id_rvi_index_still_keeps_first_occurrence_only(self, service: MappingService) -> None:
        """El descarte por IDRVI duplicado no cambia: FB01 sigue
        resolviendo a la PRIMERA fila (CN01), no a CN02."""
        assert service.get_mapping("FB01").id_corto == "CN01"
        assert service.count() == 1


class TestConsolidatedMode:
    """El modo consolidado (``METADATOS`` en una sola planilla) también indexa."""

    def test_index_uses_id_corto(self, tmp_path: Path) -> None:
        path = tmp_path / "modelo.csv"
        path.write_text(
            "ID CLASE DOCUMENTAL,ID RVI,ID Corto,CLASE DOCUMENTAL,METADATOS\n"
            '01.01,FF17,PT57,Autorizacion,"BAC_CIF, BAC_Nombre"\n',
            encoding="utf-8",
        )
        source = TabularDataSource(path)
        try:
            service = MappingService(source)
        finally:
            source.close()
        assert service.cm_codes() == ("PT57",)
        (row,) = service.get_by_cm_code("PT57")
        assert row.id_rvi == "FF17"
