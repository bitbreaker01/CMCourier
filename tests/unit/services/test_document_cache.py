"""Tests unitarios para :class:`DocumentCacheService` (037 Fase 2).

El servicio envuelve un `fake` `IDocumentCache` en memoria para que
los tests se enfoquen en la lógica de TTL, los contadores de
`hit` / `miss` y el contrato del `fields-hash`. El comportamiento
específico de SQLite se cubre en
``tests/integration/adapters/test_sqlite_document_cache.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cmcourier.domain.models import ResolvedMetadata
from cmcourier.domain.ports import (
    CacheEntry,
    CacheKey,
    CacheStats,
    IDocumentCache,
)
from cmcourier.services.document_cache import (
    DocumentCacheService,
    compute_fields_hash,
)

pytestmark = pytest.mark.unit


class _InMemoryCache(IDocumentCache):
    """`Fake` mínimo; no es `thread-safe`, OK para tests `single-threaded`."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], CacheEntry] = {}

    def get(self, key: CacheKey) -> CacheEntry | None:
        return self.rows.get((key.txn_num, key.fields_hash))

    def put(self, entry: CacheEntry) -> None:
        self.rows[(entry.txn_num, entry.fields_hash)] = entry

    def clear_txn(self, txn_num: str) -> int:
        keys = [k for k in self.rows if k[0] == txn_num]
        for k in keys:
            del self.rows[k]
        return len(keys)

    def clear_all(self) -> int:
        n = len(self.rows)
        self.rows.clear()
        return n

    def clear_older_than(self, threshold: datetime) -> int:
        keys = [k for k, v in self.rows.items() if v.cached_at < threshold]
        for k in keys:
            del self.rows[k]
        return len(keys)

    def stats(self) -> CacheStats:
        if not self.rows:
            return CacheStats(total_rows=0, oldest_cached_at=None, newest_cached_at=None)
        ts = [e.cached_at for e in self.rows.values()]
        return CacheStats(
            total_rows=len(self.rows),
            oldest_cached_at=min(ts),
            newest_cached_at=max(ts),
        )


def _service(
    *,
    ttl_minutes: int = 60,
    now: datetime | None = None,
) -> tuple[DocumentCacheService, _InMemoryCache, list[datetime]]:
    cache = _InMemoryCache()
    clock_value = [now or datetime(2026, 5, 11, 10, 0, 0, tzinfo=UTC)]

    def clock() -> datetime:
        return clock_value[0]

    svc = DocumentCacheService(cache=cache, ttl_minutes=ttl_minutes, clock=clock)
    return svc, cache, clock_value


# ---------------------------------------------------------------------------
# Determinismo de `fields_hash`
# ---------------------------------------------------------------------------


class TestFieldsHash:
    def test_order_independent(self) -> None:
        assert compute_fields_hash(["a", "b", "c"]) == compute_fields_hash(["c", "a", "b"])

    def test_different_sets_different_hashes(self) -> None:
        assert compute_fields_hash(["a", "b"]) != compute_fields_hash(["a", "b", "c"])

    def test_empty_set_is_stable(self) -> None:
        h1 = compute_fields_hash([])
        h2 = compute_fields_hash([])
        assert h1 == h2 != ""


# ---------------------------------------------------------------------------
# try_get / put
# ---------------------------------------------------------------------------


class TestTryGet:
    def test_miss_when_absent(self) -> None:
        svc, _, _ = _service()
        got = svc.try_get(txn_num="TXN1", fields=["BAC_CIF"])
        assert got is None
        snap = svc.counters_snapshot()
        assert snap.misses_absent == 1
        assert snap.hits == 0

    def test_hit_within_ttl(self) -> None:
        svc, _, clock = _service(ttl_minutes=60)
        svc.put(
            txn_num="TXN1",
            fields=["BAC_CIF"],
            metadata=ResolvedMetadata.from_dict({"BAC_CIF": "123"}),
            trigger_cif="123",
        )
        clock[0] += timedelta(minutes=30)
        got = svc.try_get(txn_num="TXN1", fields=["BAC_CIF"])
        assert got is not None
        assert dict(got.properties) == {"BAC_CIF": "123"}
        assert got.trigger_cif == "123"
        assert svc.counters_snapshot().hits == 1

    def test_miss_when_expired(self) -> None:
        svc, _, clock = _service(ttl_minutes=60)
        svc.put(
            txn_num="TXN1",
            fields=["BAC_CIF"],
            metadata=ResolvedMetadata.from_dict({"BAC_CIF": "x"}),
            trigger_cif=None,
        )
        clock[0] += timedelta(minutes=61)
        got = svc.try_get(txn_num="TXN1", fields=["BAC_CIF"])
        assert got is None
        snap = svc.counters_snapshot()
        assert snap.misses_expired == 1
        assert snap.misses_absent == 0
        assert snap.hits == 0

    def test_fields_set_change_misses(self) -> None:
        svc, _, _ = _service()
        svc.put(
            txn_num="TXN1",
            fields=["BAC_CIF"],
            metadata=ResolvedMetadata.from_dict({"BAC_CIF": "x"}),
            trigger_cif=None,
        )
        # Fields distintos → `fields_hash` distinto → `miss`.
        got = svc.try_get(txn_num="TXN1", fields=["BAC_CIF", "Nombre_Cliente"])
        assert got is None
        assert svc.counters_snapshot().misses_absent == 1


class TestPut:
    def test_put_then_get_preserves_properties(self) -> None:
        svc, cache, _ = _service()
        svc.put(
            txn_num="TXN1",
            fields=["BAC_CIF", "Nombre_Cliente"],
            metadata=ResolvedMetadata.from_dict({"BAC_CIF": "123", "Nombre_Cliente": "Test"}),
            trigger_cif="123",
        )
        # Mete la mano en el `fake` para confirmar el almacenamiento.
        key = CacheKey(
            txn_num="TXN1",
            fields_hash=compute_fields_hash(["BAC_CIF", "Nombre_Cliente"]),
        )
        stored = cache.get(key)
        assert stored is not None
        assert dict(stored.properties) == {"BAC_CIF": "123", "Nombre_Cliente": "Test"}
        assert stored.trigger_cif == "123"


# ---------------------------------------------------------------------------
# Pass-throughs de cara al operador
# ---------------------------------------------------------------------------


class TestOperatorOps:
    def test_clear_txn(self) -> None:
        svc, cache, _ = _service()
        svc.put(
            txn_num="TXN1",
            fields=["BAC_CIF"],
            metadata=ResolvedMetadata.from_dict({"X": "1"}),
            trigger_cif=None,
        )
        svc.put(
            txn_num="TXN2",
            fields=["BAC_CIF"],
            metadata=ResolvedMetadata.from_dict({"X": "1"}),
            trigger_cif=None,
        )
        assert svc.clear_txn("TXN1") == 1
        assert svc.disk_stats().total_rows == 1

    def test_clear_all(self) -> None:
        svc, _, _ = _service()
        for txn in ("a", "b", "c"):
            svc.put(
                txn_num=txn,
                fields=["X"],
                metadata=ResolvedMetadata.from_dict({"X": "1"}),
                trigger_cif=None,
            )
        assert svc.clear_all() == 3
        assert svc.disk_stats().total_rows == 0

    def test_clear_older_than_minutes(self) -> None:
        svc, _, clock = _service(ttl_minutes=60)
        svc.put(
            txn_num="old",
            fields=["X"],
            metadata=ResolvedMetadata.from_dict({"X": "1"}),
            trigger_cif=None,
        )
        clock[0] += timedelta(minutes=120)
        svc.put(
            txn_num="fresh",
            fields=["X"],
            metadata=ResolvedMetadata.from_dict({"X": "1"}),
            trigger_cif=None,
        )
        deleted = svc.clear_older_than_minutes(60)
        assert deleted == 1
        assert svc.disk_stats().total_rows == 1


# ---------------------------------------------------------------------------
# Eventos de log estructurados
# ---------------------------------------------------------------------------


class TestLogEvents:
    def test_miss_event_fields(self, caplog: pytest.LogCaptureFixture) -> None:
        svc, _, _ = _service()
        with caplog.at_level("DEBUG", logger="cmcourier.services.document_cache"):
            svc.try_get(txn_num="TXN1", fields=["X"])
        rec = next(r for r in caplog.records if getattr(r, "event", None) == "document_cache_miss")
        assert rec.txn_num == "TXN1"
        assert rec.reason == "absent"
        assert isinstance(rec.fields_hash, str)

    def test_hit_event_fields(self, caplog: pytest.LogCaptureFixture) -> None:
        svc, _, clock = _service(ttl_minutes=60)
        svc.put(
            txn_num="TXN1",
            fields=["X"],
            metadata=ResolvedMetadata.from_dict({"X": "1"}),
            trigger_cif=None,
        )
        clock[0] += timedelta(seconds=5)
        with caplog.at_level("DEBUG", logger="cmcourier.services.document_cache"):
            svc.try_get(txn_num="TXN1", fields=["X"])
        rec = next(r for r in caplog.records if getattr(r, "event", None) == "document_cache_hit")
        assert rec.txn_num == "TXN1"
        assert rec.age_s == 5.0
