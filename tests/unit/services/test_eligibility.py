"""150 — elegibilidad: sólo los clientes con producto activo.

Dos responsabilidades, y la primera es la que justifica la spec:

* **El preflight (REQ-002).** Una lista que no abre, a la que le falta una
  columna declarada, o que tiene CERO filas, **aborta la corrida**. No se
  falla documento por documento: no se arranca. Si el pipeline siguiera
  produciría un censo impecable diciendo que 200.000 documentos se
  excluyeron por cliente inactivo — un reporte prolijo y completamente
  falso. Una lista vacía no puede significar "no migres nada".

* **El chequeo por documento (REQ-001/003).** ``match_any``: basta con que
  UNO de los criterios matchee. Las búsquedas se memoizan por corrida con
  la clave ``(fuente, columna, valor)``, así el primer documento de un
  cliente paga el salto y los demás van gratis.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from cmcourier.domain.exceptions import EligibilityListError
from cmcourier.domain.models import RVABREPDocument, RvabrepRowTrigger, Trigger
from cmcourier.domain.ports import IDataSource
from cmcourier.services.eligibility import (
    EligibilityConfig,
    EligibilityMatch,
    EligibilityService,
)
from cmcourier.services.metadata import (
    FieldSourceConfig,
    MetadataConfig,
    MetadataService,
    SourceConfig,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSource(IDataSource):
    """``IDataSource`` en memoria que cuenta cada ``get_by_fields``."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.get_by_fields_calls = 0

    def get_all(self) -> Iterator[dict[str, Any]]:
        yield from self.rows

    def get_by_fields(self, filters: Mapping[str, Any]) -> list[dict[str, Any]]:
        self.get_by_fields_calls += 1
        return [row for row in self.rows if all(row.get(k) == v for k, v in filters.items())]

    def get_by_fields_in(
        self, field: str, values: list[Any], fixed_filters: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        return [row for row in self.rows if row.get(field) in values]

    def stream_by_fields_in(
        self, field: str, values: list[Any], fixed_filters: Mapping[str, Any]
    ) -> Iterator[dict[str, Any]]:
        yield from self.get_by_fields_in(field, values, fixed_filters)

    def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError

    def query_stream(self, sql: str, params: list[Any] | None = None) -> Iterator[dict[str, Any]]:
        raise NotImplementedError

    def count(self) -> int:
        return len(self.rows)

    def close(self) -> None:
        return None


class _BrokenSource(_FakeSource):
    """La lista que no abre: cualquier lectura explota."""

    def count(self) -> int:
        raise OSError("the network share is gone")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_ACTIVOS = [
    {"Shortname": "ACMESA01", "CIF": "123456789"},
    {"Shortname": "", "CIF": "987654321"},  # una fila sin shortname: pasa por CIF
]


def _document(**overrides: object) -> RVABREPDocument:
    defaults: dict[str, object] = {
        "system_code": "1",
        "txn_num": "TXN_ID",
        "index1": "",
        "index2": "",
        "index3": "",
        "index4": "",
        "index5": "",
        "index6": "",
        "index7": "0001",
        "image_type": "O",
        "image_path": "x",
        "file_name": "DOC.pdf",
        "creation_date": datetime(2026, 1, 1),  # noqa: DTZ001
        "last_view_date": None,
        "total_pages": 1,
        "delete_code": "",
    }
    defaults.update(overrides)
    return RVABREPDocument(**defaults)  # type: ignore[arg-type]


def _trigger(**row: str) -> Trigger:
    base = {"ABABCD": "", "ABACCD": "", "ABAACD": ""}
    base.update(row)
    return RvabrepRowTrigger(row=base)


def _metadata_config() -> MetadataConfig:
    return MetadataConfig(
        field_aliases={},
        field_sources={
            "BAC_Shortname": FieldSourceConfig(
                sources=(SourceConfig(source_type="rvabrep", lookup_value_column="index1"),),
            ),
            "BAC_CIF": FieldSourceConfig(
                sources=(SourceConfig(source_type="rvabrep", lookup_value_column="index2"),),
            ),
        },
        prefetch_enabled=False,
    )


@pytest.fixture
def activos() -> _FakeSource:
    return _FakeSource([dict(row) for row in _ACTIVOS])


@pytest.fixture
def metadata_service(activos: _FakeSource) -> MetadataService:
    return MetadataService(_metadata_config(), {"clientes_activos": activos})


def _config(**overrides: object) -> EligibilityConfig:
    base: dict[str, Any] = {
        "enabled": True,
        "source": "csv:clientes_activos",
        "match_any": (
            EligibilityMatch(field="BAC_Shortname", column="Shortname"),
            EligibilityMatch(field="BAC_CIF", column="CIF"),
        ),
        "source_path": "",
    }
    base.update(overrides)
    return EligibilityConfig(**base)


def _service(metadata_service: MetadataService, **overrides: object) -> EligibilityService:
    return EligibilityService(_config(**overrides), metadata_service)


# ---------------------------------------------------------------------------
# REQ-002 — una lista rota NO es "nadie está activo": la corrida no arranca
# ---------------------------------------------------------------------------


class TestPreflight150:
    def test_una_lista_sana_no_aborta(self, metadata_service: MetadataService) -> None:
        snapshot = _service(metadata_service).verify_list()
        assert snapshot.row_count == 2
        assert snapshot.source == "csv:clientes_activos"

    def test_lista_vacia_aborta_nombrando_la_fuente(self) -> None:
        """El corazón de la spec: cero filas es "no podemos responder la
        pregunta", NUNCA "no migres nada"."""
        vacia = _FakeSource([])
        service = _service(MetadataService(_metadata_config(), {"clientes_activos": vacia}))
        with pytest.raises(EligibilityListError) as exc_info:
            service.verify_list()
        message = str(exc_info.value)
        assert "csv:clientes_activos" in message
        assert "zero rows" in message

    def test_columna_faltante_aborta_nombrandola(self) -> None:
        sin_cif = _FakeSource([{"Shortname": "ACMESA01"}])
        service = _service(MetadataService(_metadata_config(), {"clientes_activos": sin_cif}))
        with pytest.raises(EligibilityListError) as exc_info:
            service.verify_list()
        message = str(exc_info.value)
        assert "csv:clientes_activos" in message
        assert "CIF" in message

    def test_fuente_que_no_abre_aborta(self) -> None:
        rota = _BrokenSource([{"Shortname": "ACMESA01", "CIF": "1"}])
        service = _service(MetadataService(_metadata_config(), {"clientes_activos": rota}))
        with pytest.raises(EligibilityListError, match="csv:clientes_activos"):
            service.verify_list()

    def test_alias_que_no_esta_en_el_registro_aborta(self) -> None:
        service = _service(MetadataService(_metadata_config(), {}))
        with pytest.raises(EligibilityListError, match="clientes_activos"):
            service.verify_list()

    def test_el_snapshot_lleva_ruta_y_fecha_para_la_auditoria(
        self, metadata_service: MetadataService, tmp_path: Path
    ) -> None:
        """REQ-005: dentro de seis meses alguien va a preguntar "¿activo
        según qué lista?"."""
        lista = tmp_path / "clientes-activos.csv"
        lista.write_text("Shortname,CIF\n")
        service = _service(metadata_service, source_path=str(lista))
        snapshot = service.verify_list()
        assert snapshot.path == str(lista)
        assert snapshot.modified_at != ""


# ---------------------------------------------------------------------------
# REQ-001 — `match_any`: basta con que UNA matchee
# ---------------------------------------------------------------------------


class TestMatchAny150:
    def test_matchea_por_el_primer_criterio(self, metadata_service: MetadataService) -> None:
        service = _service(metadata_service)
        assert service.is_active(_trigger(), _document(), {"BAC_Shortname": "ACMESA01"}) is True

    def test_matchea_solo_por_el_segundo(self, metadata_service: MetadataService) -> None:
        """Una fila a la que le falta el shortname no debería costar la
        exclusión del cliente entero."""
        service = _service(metadata_service)
        resolved = {"BAC_Shortname": "NOESTA01", "BAC_CIF": "987654321"}
        assert service.is_active(_trigger(), _document(), resolved) is True

    def test_sin_ningun_match_el_cliente_no_esta_activo(
        self, metadata_service: MetadataService
    ) -> None:
        service = _service(metadata_service)
        resolved = {"BAC_Shortname": "NOESTA01", "BAC_CIF": "000000000"}
        assert service.is_active(_trigger(), _document(), resolved) is False

    def test_un_campo_vacio_no_cuenta_como_match(self, metadata_service: MetadataService) -> None:
        """La lista trae una fila con ``Shortname`` vacío: un documento sin
        shortname resuelto NO puede matchearla."""
        service = _service(metadata_service)
        assert service.is_active(_trigger(), _document(), {"BAC_Shortname": ""}) is False

    def test_un_campo_que_la_identidad_no_resolvio_se_resuelve_por_la_cadena(
        self, metadata_service: MetadataService
    ) -> None:
        """``match_any.field`` no tiene que ser un slot de ``identity:``: lo
        que falte se resuelve con el MISMO motor de ``field_sources``."""
        service = _service(metadata_service)
        document = _document(index2="123456789")
        assert service.is_active(_trigger(), document, {}) is True


# ---------------------------------------------------------------------------
# REQ-003 — el memo por corrida
# ---------------------------------------------------------------------------


class TestMemoPorCorrida150:
    def test_el_mismo_cliente_no_se_vuelve_a_buscar(
        self, metadata_service: MetadataService, activos: _FakeSource
    ) -> None:
        service = _service(metadata_service)
        resolved = {"BAC_Shortname": "ACMESA01"}
        for _ in range(5):
            assert service.is_active(_trigger(), _document(), resolved) is True
        assert activos.get_by_fields_calls == 1

    def test_tambien_se_memoiza_el_miss(
        self, metadata_service: MetadataService, activos: _FakeSource
    ) -> None:
        """Un cliente que NO está en la lista tampoco se vuelve a preguntar —
        si no, el caso inactivo (el caro) sería el único sin memo."""
        service = _service(metadata_service)
        resolved = {"BAC_Shortname": "NOESTA01", "BAC_CIF": "000000000"}
        for _ in range(5):
            assert service.is_active(_trigger(), _document(), resolved) is False
        assert activos.get_by_fields_calls == 2  # uno por criterio, una sola vez

    def test_dos_clientes_distintos_pagan_su_salto(
        self, metadata_service: MetadataService, activos: _FakeSource
    ) -> None:
        service = _service(metadata_service)
        assert service.is_active(_trigger(), _document(), {"BAC_Shortname": "ACMESA01"}) is True
        assert service.is_active(_trigger(), _document(), {"BAC_Shortname": "OTRASA02"}) is False
        assert activos.get_by_fields_calls == 2
