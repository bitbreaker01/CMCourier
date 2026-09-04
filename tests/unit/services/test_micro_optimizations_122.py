"""Tests de los micro-hallazgos de la auditoría (122)."""

from __future__ import annotations

import pytest

from cmcourier.services.document_cache import _fields_hash_cached, compute_fields_hash
from cmcourier.services.worker_pool_stats import WorkerPoolStats

pytestmark = pytest.mark.unit


class TestFieldsHashMemo:
    def test_same_tuple_computes_once(self) -> None:
        # E1: la tupla de campos se repite toda la corrida (una por
        # id_rvi) — el hash se memoiza.
        fields = ("BAC_CIF", "BAC_Nombre")
        before = _fields_hash_cached.cache_info().hits
        h1 = compute_fields_hash(fields)
        h2 = compute_fields_hash(fields)
        assert h1 == h2
        assert _fields_hash_cached.cache_info().hits > before

    def test_order_insensitive(self) -> None:
        # El hash siempre se computó sobre sorted(fields) — se preserva.
        assert compute_fields_hash(("b", "a")) == compute_fields_hash(("a", "b"))

    def test_different_fields_different_hash(self) -> None:
        assert compute_fields_hash(("a",)) != compute_fields_hash(("a", "b"))


class TestDecrementQueueDepth:
    def test_decrements_by_one(self) -> None:
        stats = WorkerPoolStats()
        stats.set_queue_depth(5)
        stats.decrement_queue_depth()
        assert stats.snapshot().queue_depth == 4

    def test_floors_at_zero(self) -> None:
        stats = WorkerPoolStats()
        stats.set_queue_depth(0)
        stats.decrement_queue_depth()
        assert stats.snapshot().queue_depth == 0


class TestUploaderTimeoutAccessor:
    def test_current_timeout_reflects_live_value(self) -> None:
        from cmcourier.adapters.upload.cmis_uploader import CmisConfig, CmisUploader

        uploader = CmisUploader(
            CmisConfig(
                base_url="http://cm.test/cmis",
                repo_id="repo",
                username="u",
                password="p",
                timeout_seconds=300.0,
                verify_ssl=False,
                max_bandwidth_mbps=0.0,
                retry_max_attempts=1,
                retry_base_delay_s=0.01,
                pool_size=2,
                unmask_pii=False,
            )
        )
        assert uploader.current_timeout_s == 300.0
        uploader._timeout_s = 45.0  # noqa: SLF001 — el AIMD lo ajusta en vivo
        assert uploader.current_timeout_s == 45.0


class TestMetadataAliasesPrecomputed:
    def test_aliases_lower_built_once_in_init(self) -> None:
        from cmcourier.services.metadata import MetadataConfig, MetadataService

        service = MetadataService(
            config=MetadataConfig(
                field_aliases={"CIF": "BAC_CIF", "Nombre": "BAC_Nombre"},
                field_sources={},
            ),
            sources_registry={},
        )
        assert service._aliases_lower == {  # noqa: SLF001
            "cif": "BAC_CIF",
            "nombre": "BAC_Nombre",
        }
