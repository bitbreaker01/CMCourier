"""Tests unitarios para ``cmcourier.domain.ports``.

Los puertos son clases base abstractas; esta suite demuestra que no se
pueden instanciar directamente y que cada método abstracto declarado
está, en efecto, decorado con ``@abstractmethod``.
"""

from __future__ import annotations

import abc
import collections.abc

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
            "stream_by_fields_in",  # 148 REQ-001
            "get_all",
            "count",
            "close",
        }
        assert IDataSource.__abstractmethods__ == frozenset(expected)

    def test_stream_by_fields_in_returns_an_iterator_not_a_list(self) -> None:
        """148 REQ-001: el equivalente en stream de ``get_by_fields_in``.

        Con el censo siempre activo ``filters.systems: ["1"]`` es la
        configuración NORMAL; materializar un sistema entero en una lista
        de Python no es aceptable. El tipo de retorno es la primera línea
        de defensa contra que alguien lo "optimice" de vuelta a ``list``.
        """
        import typing

        hints = typing.get_type_hints(IDataSource.stream_by_fields_in)
        assert typing.get_origin(hints["return"]) is collections.abc.Iterator


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
                "increment_source_total",  # 148 REQ-005
                "set_source_total",  # 148 REQ-005
                "list_txn_nums_for_batch",
                "list_docs_for_batch",
                "flush",
                "close",
            }
        )

    def test_reason_code_is_an_optional_keyword_on_the_failure_paths(self) -> None:
        """148 REQ-004: WP2 tiene que poder dejar una razón sin romper a
        ningún caller existente — el parámetro es keyword-only y opcional."""
        import inspect

        for name in ("mark_stage_failed", "mark_stage_terminal"):
            param = inspect.signature(getattr(ITrackingStore, name)).parameters["reason_code"]
            assert param.kind is inspect.Parameter.KEYWORD_ONLY
            assert param.default is None


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
