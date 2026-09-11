"""147 REQ-001 + REQ-005: cadenas de lookups encadenados y memo por corrida.

``lookup_value_source: "field.<NAME>"`` hace que la clave de búsqueda sea el
valor YA RESUELTO de otro campo. El resolver arma el grafo, lo ordena
topológicamente y resuelve en ese orden — el orden de declaración en el YAML
deja de importar. Una dependencia que no resolvió NO aborta: saltea esa
fuente igual que un valor vacío y sigue con la próxima de la cadena.

El memo (REQ-005) es por corrida y por ``(campo, valor de clave)``: la cadena
se repite idéntica para todos los documentos de un mismo cliente.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import datetime
from typing import Any

import pytest

from cmcourier.domain.models import ClientTrigger, CMMapping, RVABREPDocument
from cmcourier.domain.ports import IDataSource
from cmcourier.services.metadata import (
    FieldSourceConfig,
    MetadataConfig,
    MetadataService,
    SourceConfig,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fake source (en memoria, cuenta y ordena sus invocaciones)
# ---------------------------------------------------------------------------


class _FakeSource(IDataSource):
    """``IDataSource`` en memoria que registra cada ``get_by_fields``."""

    def __init__(self, alias: str, rows: list[dict[str, Any]], log: list[str]) -> None:
        self.alias = alias
        self.rows = rows
        self.log = log
        self.get_by_fields_calls = 0

    def get_all(self) -> Iterator[dict[str, Any]]:
        yield from self.rows

    def get_by_fields(self, filters: Mapping[str, Any]) -> list[dict[str, Any]]:
        self.get_by_fields_calls += 1
        self.log.append(f"{self.alias}:{sorted(filters.items())}")
        return [row for row in self.rows if all(row.get(k) == v for k, v in filters.items())]

    def get_by_fields_in(
        self, field: str, values: list[Any], fixed_filters: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        return [row for row in self.rows if row.get(field) in values]

    def stream_by_fields_in(  # 148 REQ-001
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _document(**overrides: object) -> RVABREPDocument:
    defaults: dict[str, object] = {
        "system_code": "1",
        "txn_num": "999",
        "index1": "HIJO-1",
        "index2": "",
        "index3": "",
        "index4": "",
        "index5": "",
        "index6": "",
        "index7": "FF17",
        "image_type": "B",
        "image_path": "x",
        "file_name": "x.001",
        "creation_date": datetime(2025, 11, 17),
        "last_view_date": None,
        "total_pages": 1,
        "delete_code": "",
    }
    defaults.update(overrides)
    return RVABREPDocument(**defaults)  # type: ignore[arg-type]


def _mapping(*fields: str) -> CMMapping:
    return CMMapping(
        clase_id="01.02.04.01.01",
        id_rvi="FF17",
        id_corto="PT57",
        clase_name="Test",
        required_metadata_fields=fields,
    )


def _lookup(
    alias: str, key_col: str, value_col: str, from_field: str, *, kind: str = "as400"
) -> SourceConfig:
    return SourceConfig(
        source_type=f"{kind}:{alias}",
        lookup_value_column=value_col,
        lookup_key_column=key_col,
        lookup_value_source=f"field.{from_field}",
    )


@pytest.fixture
def call_log() -> list[str]:
    return []


@pytest.fixture
def registry(call_log: list[str]) -> dict[str, IDataSource]:
    """Dos tablas: afiliados (hijo → padre) y clientes (shortname ↔ cif)."""
    afiliados = _FakeSource(
        "afiliados",
        [{"AFIHIJ": "HIJO-1", "AFIPAD": "PADRE-7"}],
        call_log,
    )
    clientes = _FakeSource(
        "clientes",
        [{"CUSAFI": "PADRE-7", "CUSSHN": "ACMESA01", "CUSCIF": "123456789"}],
        call_log,
    )
    return {"afiliados": afiliados, "clientes": clientes}


def _chain_config() -> MetadataConfig:
    """La cadena de tres saltos: hijo → padre → shortname → cif.

    Los campos se declaran DELIBERADAMENTE en orden inverso al de
    resolución: el orden topológico es el que manda, no el del YAML.
    """
    return MetadataConfig(
        field_aliases={},
        field_sources={
            "BAC_CIF": FieldSourceConfig(
                sources=(_lookup("clientes", "CUSSHN", "CUSCIF", "BAC_Shortname"),),
                default_value="000000000",
            ),
            "BAC_Shortname": FieldSourceConfig(
                sources=(_lookup("clientes", "CUSAFI", "CUSSHN", "BAC_Afiliado_Padre"),),
                default_value="SIN_SHORTNAME",
            ),
            "BAC_Afiliado_Padre": FieldSourceConfig(
                sources=(_lookup("afiliados", "AFIHIJ", "AFIPAD", "BAC_Afiliado_Hijo"),),
            ),
            "BAC_Afiliado_Hijo": FieldSourceConfig(
                sources=(SourceConfig(source_type="rvabrep", lookup_value_column="index1"),),
            ),
        },
        prefetch_enabled=False,
    )


@pytest.fixture
def chain_service(registry: dict[str, IDataSource]) -> MetadataService:
    return MetadataService(_chain_config(), registry)


@pytest.fixture
def trigger() -> ClientTrigger:
    return ClientTrigger(shortname="ACMESA01", cif=None, system_id="1")


# ---------------------------------------------------------------------------
# REQ-001 — orden topológico
# ---------------------------------------------------------------------------


class TestOrdenTopologico:
    def test_la_cadena_de_tres_saltos_resuelve_de_punta_a_punta(
        self, chain_service: MetadataService, trigger: ClientTrigger
    ) -> None:
        result = chain_service.resolve(trigger, _document(), _mapping("BAC_CIF"))
        assert result.metadata["BAC_CIF"] == "123456789"

    def test_el_orden_de_declaracion_no_manda(
        self, chain_service: MetadataService, trigger: ClientTrigger, call_log: list[str]
    ) -> None:
        # BAC_CIF está declarado PRIMERO y depende de todo lo demás. Si el
        # resolver siguiera el orden de entrada, la dependencia no estaría
        # resuelta y caería al default.
        chain_service.resolve(trigger, _document(), _mapping("BAC_CIF"))
        assert call_log == [
            "afiliados:[('AFIHIJ', 'HIJO-1')]",
            "clientes:[('CUSAFI', 'PADRE-7')]",
            "clientes:[('CUSSHN', 'ACMESA01')]",
        ]

    def test_solo_se_devuelven_los_campos_pedidos(
        self, chain_service: MetadataService, trigger: ClientTrigger
    ) -> None:
        result = chain_service.resolve(trigger, _document(), _mapping("BAC_CIF"))
        assert set(result.metadata.properties) == {"BAC_CIF"}

    def test_las_dependencias_pedidas_tambien_viajan(
        self, chain_service: MetadataService, trigger: ClientTrigger
    ) -> None:
        result = chain_service.resolve(
            trigger, _document(), _mapping("BAC_CIF", "BAC_Afiliado_Padre")
        )
        assert result.metadata["BAC_Afiliado_Padre"] == "PADRE-7"
        assert result.metadata["BAC_CIF"] == "123456789"


class TestSoloLoPedido:
    def test_un_campo_no_pedido_ni_dependido_no_se_resuelve(
        self, registry: dict[str, IDataSource], trigger: ClientTrigger, call_log: list[str]
    ) -> None:
        config = _chain_config()
        extra = dict(config.field_sources)
        extra["BAC_Irrelevante"] = FieldSourceConfig(
            sources=(_lookup("clientes", "CUSSHN", "CUSCIF", "BAC_Shortname"),),
        )
        service = MetadataService(
            MetadataConfig(field_aliases={}, field_sources=extra, prefetch_enabled=False),
            registry,
        )
        service.resolve(trigger, _document(), _mapping("BAC_Afiliado_Hijo"))
        # El hijo sale de rvabrep: ni una sola consulta a las tablas.
        assert call_log == []


# ---------------------------------------------------------------------------
# REQ-001 — una dependencia sin resolver saltea SOLO esa fuente
# ---------------------------------------------------------------------------


class TestDependenciaSinResolver:
    def _config(self) -> MetadataConfig:
        return MetadataConfig(
            field_aliases={},
            field_sources={
                # No resuelve nunca: rvabrep.index2 viene vacío y no hay default.
                "BAC_Huerfano": FieldSourceConfig(
                    sources=(SourceConfig(source_type="rvabrep", lookup_value_column="index2"),),
                ),
                "BAC_Nombre": FieldSourceConfig(
                    sources=(
                        _lookup("clientes", "CUSSHN", "CUSCIF", "BAC_Huerfano"),
                        SourceConfig(source_type="rvabrep", lookup_value_column="index3"),
                    ),
                ),
            },
            prefetch_enabled=False,
        )

    def test_la_cadena_sigue_con_la_proxima_fuente(
        self, registry: dict[str, IDataSource], trigger: ClientTrigger
    ) -> None:
        service = MetadataService(self._config(), registry)
        result = service.resolve(trigger, _document(index3="FALLBACK"), _mapping("BAC_Nombre"))
        assert result.metadata["BAC_Nombre"] == "FALLBACK"

    def test_la_fuente_dependiente_ni_siquiera_consulta_la_tabla(
        self, registry: dict[str, IDataSource], trigger: ClientTrigger, call_log: list[str]
    ) -> None:
        service = MetadataService(self._config(), registry)
        service.resolve(trigger, _document(index3="FALLBACK"), _mapping("BAC_Nombre"))
        assert call_log == []

    def test_la_dependencia_rota_no_aborta_el_documento(
        self, registry: dict[str, IDataSource], trigger: ClientTrigger
    ) -> None:
        service = MetadataService(self._config(), registry)
        # No explota: BAC_Huerfano es sólo dependencia, no campo pedido.
        result = service.resolve(trigger, _document(index3="OK"), _mapping("BAC_Nombre"))
        assert "BAC_Huerfano" not in result.metadata.properties


# ---------------------------------------------------------------------------
# REQ-005 — memo por corrida
# ---------------------------------------------------------------------------


class TestMemoPorCorrida:
    def test_arranca_en_cero(self, chain_service: MetadataService) -> None:
        assert chain_service.memo_hits == 0

    def test_el_segundo_documento_del_mismo_cliente_no_re_consulta(
        self, chain_service: MetadataService, trigger: ClientTrigger, call_log: list[str]
    ) -> None:
        chain_service.resolve(trigger, _document(), _mapping("BAC_CIF"))
        calls_after_first = len(call_log)
        chain_service.resolve(trigger, _document(txn_num="1000"), _mapping("BAC_CIF"))
        assert len(call_log) == calls_after_first

    def test_cuenta_los_hits(self, chain_service: MetadataService, trigger: ClientTrigger) -> None:
        chain_service.resolve(trigger, _document(), _mapping("BAC_CIF"))
        assert chain_service.memo_hits == 0
        chain_service.resolve(trigger, _document(txn_num="1000"), _mapping("BAC_CIF"))
        # Los tres saltos de la cadena salen del memo.
        assert chain_service.memo_hits == 3

    def test_una_clave_distinta_no_es_hit(
        self, chain_service: MetadataService, trigger: ClientTrigger, call_log: list[str]
    ) -> None:
        chain_service.resolve(trigger, _document(), _mapping("BAC_CIF"))
        calls_after_first = len(call_log)
        chain_service.resolve(trigger, _document(index1="HIJO-9"), _mapping("BAC_CIF"))
        assert len(call_log) > calls_after_first


# ---------------------------------------------------------------------------
# REQ-003 — semilla: lo que S2 ya resolvió no se vuelve a resolver en S3
# ---------------------------------------------------------------------------


class TestSemillaDeResolucion:
    """147 REQ-003: ``resolve(..., seed=...)`` recibe los campos que la
    resolución de identidad (S2) ya resolvió. Un campo sembrado no se
    re-consulta, y tampoco se resuelven las dependencias que existían SÓLO
    para llegar a él — el ahorro es la cadena entera, no el último salto."""

    def test_un_campo_sembrado_no_cuesta_ni_una_consulta(
        self, chain_service: MetadataService, trigger: ClientTrigger, call_log: list[str]
    ) -> None:
        chain_service.resolve(
            trigger, _document(), _mapping("BAC_CIF"), seed={"BAC_CIF": "123456789"}
        )
        assert call_log == []

    def test_el_valor_sembrado_es_el_que_viaja(
        self, chain_service: MetadataService, trigger: ClientTrigger
    ) -> None:
        result = chain_service.resolve(
            trigger, _document(), _mapping("BAC_CIF"), seed={"BAC_CIF": "999999999"}
        )
        assert result.metadata["BAC_CIF"] == "999999999"

    def test_la_semilla_de_un_eslabon_ahorra_los_saltos_previos(
        self, chain_service: MetadataService, trigger: ClientTrigger, call_log: list[str]
    ) -> None:
        # Sembrando el shortname, los dos saltos de afiliado desaparecen y
        # queda sólo el último (shortname → cif).
        chain_service.resolve(
            trigger, _document(), _mapping("BAC_CIF"), seed={"BAC_Shortname": "ACMESA01"}
        )
        assert call_log == ["clientes:[('CUSSHN', 'ACMESA01')]"]

    def test_sin_semilla_el_comportamiento_no_cambia(
        self, chain_service: MetadataService, trigger: ClientTrigger, call_log: list[str]
    ) -> None:
        chain_service.resolve(trigger, _document(), _mapping("BAC_CIF"), seed={})
        assert len(call_log) == 3
