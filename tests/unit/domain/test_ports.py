"""Tests unitarios para ``cmcourier.domain.ports``.

Los puertos son clases base abstractas; esta suite demuestra que no se
pueden instanciar directamente y que cada método abstracto declarado
está, en efecto, decorado con ``@abstractmethod``.
"""

from __future__ import annotations

import abc

import pytest

from cmcourier.domain.ports import (
    IAssembler,
    IDataSource,
    ITrackingStore,
    IUploader,
    S0Strategy,
)

ALL_PORTS: tuple[type, ...] = (IDataSource, ITrackingStore, IAssembler, IUploader, S0Strategy)


@pytest.mark.parametrize("port_cls", ALL_PORTS)
def test_port_inherits_from_abc(port_cls: type) -> None:
    assert issubclass(port_cls, abc.ABC)


@pytest.mark.parametrize("port_cls", ALL_PORTS)
def test_port_cannot_be_instantiated(port_cls: type) -> None:
    """El constructor debe levantar ``TypeError`` porque los métodos siguen abstractos."""
    with pytest.raises(TypeError):
        port_cls()  # type: ignore[abstract]


class TestIDataSourceContract:
    def test_abstract_methods(self) -> None:
        expected = {
            "query",
            "query_stream",
            "get_by_fields",
            "get_by_fields_in",
            "get_all",
            "count",
            "close",
        }
        assert IDataSource.__abstractmethods__ == frozenset(expected)


class TestITrackingStoreContract:
    def test_abstract_methods(self) -> None:
        assert ITrackingStore.__abstractmethods__ == frozenset(
            {
                "is_uploaded",
                "is_stage_done",
                "mark_stage_pending",
                "mark_stage_done",
                "mark_stage_failed",
                "record_staged_file_metadata",
                "mark_stage_terminal",
                "list_batches",
                "get_batch_details",
                "retry_failed",
                "start_batch",
                "complete_batch",
                "list_txn_nums_for_batch",
                "list_docs_for_batch",
                "flush",
                "close",
            }
        )


class TestIAssemblerContract:
    def test_abstract_methods(self) -> None:
        assert IAssembler.__abstractmethods__ == frozenset({"assemble"})


class TestIUploaderContract:
    def test_abstract_methods(self) -> None:
        assert IUploader.__abstractmethods__ == frozenset(
            {
                "verify_folder_exists",
                "upload",
                "test_connection",
                "get_type_definition",
                "get_type_descendants",  # 145
                "set_credentials",  # 132
            }
        )


class TestS0StrategyContract:
    def test_abstract_methods(self) -> None:
        assert S0Strategy.__abstractmethods__ == frozenset({"acquire"})
