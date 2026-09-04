"""Servicio de cache de metadata S3 cross-`batch` (POST-MVP §9, 037 Fase 2).

Envuelve :class:`IDocumentCache` con lógica de TTL, contadores de
hits y misses, y eventos de log estructurados. La ruta de S3 del
`pipeline` consulta este servicio antes de invocar
:class:`MetadataService`; un hit cortocircuita al resolver y un miss
ejecuta el resolver y escribe en la cache.

La inyección de `clock` (``clock=lambda: datetime.now(UTC)``)
mantiene la expiración por TTL determinista en los tests.

Principio III de la Constitución: tamaño de función ≤ 50 líneas; los
helpers ``try_get`` y ``put`` entran cada uno en una pantalla.
"""

from __future__ import annotations

__all__ = [
    "CacheCounters",
    "DocumentCacheService",
    "compute_fields_hash",
]

import hashlib
import logging
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from types import MappingProxyType

from cmcourier.domain.models import ResolvedMetadata
from cmcourier.domain.ports import (
    CacheEntry,
    CacheKey,
    CacheStats,
    IDocumentCache,
)

_log = logging.getLogger(__name__)


@lru_cache(maxsize=1024)
def _fields_hash_cached(fields: tuple[str, ...]) -> str:
    joined = ",".join(sorted(fields))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def compute_fields_hash(fields: Iterable[str]) -> str:
    """Devuelve el `SHA-256` hex de la lista de campos ordenada y
    unida por comas.

    El orden hace que el hash sea independiente del orden de
    declaración en el CSV de mapping. Dos conjuntos distintos de
    campos DEBEN producir hashes distintos: esa es la garantía de
    seguridad de la cache frente a la evolución del mapping.

    122: memoizado — se invocaba dos veces por documento (`try_get` +
    `put`) sobre tuplas que se repiten toda la corrida (una por
    `id_rvi`).
    """
    return _fields_hash_cached(tuple(fields))


@dataclass(slots=True)
class CacheCounters:
    """Contadores in-memory de hits y misses, thread-safe, expuestos al CLI."""

    hits: int = 0
    misses_absent: int = 0
    misses_expired: int = 0

    @property
    def total_misses(self) -> int:
        return self.misses_absent + self.misses_expired

    @property
    def total_calls(self) -> int:
        return self.hits + self.total_misses


class DocumentCacheService:
    """Wrapper sobre :class:`IDocumentCache` con conciencia de TTL.

    Ciclo de vida:

    1. Construcción con ``cache``, ``ttl_minutes`` y un `clock`
       monotónico opcional.
    2. El `pipeline` llama a :meth:`try_get` antes de S3.
    3. Ante un miss, el `pipeline` corre el resolver y llama a
       :meth:`put`.
    """

    def __init__(
        self,
        *,
        cache: IDocumentCache,
        ttl_minutes: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._cache = cache
        self._ttl = timedelta(minutes=int(ttl_minutes))
        self._clock = clock
        self._lock = threading.Lock()
        self._counters = CacheCounters()

    # ---------- API consumida por el `pipeline` ----------

    def try_get(self, *, txn_num: str, fields: Iterable[str]) -> CacheEntry | None:
        """Devuelve una entrada fresca o ``None``. Actualiza los
        contadores in-memory."""
        key = CacheKey(txn_num=txn_num, fields_hash=compute_fields_hash(fields))
        entry = self._cache.get(key)
        if entry is None:
            with self._lock:
                self._counters.misses_absent += 1
            _log.debug(
                "document_cache miss",
                extra={
                    "event": "document_cache_miss",
                    "txn_num": txn_num,
                    "reason": "absent",
                    "fields_hash": key.fields_hash,
                },
            )
            return None
        age = self._clock() - entry.cached_at
        if age > self._ttl:
            with self._lock:
                self._counters.misses_expired += 1
            _log.debug(
                "document_cache miss",
                extra={
                    "event": "document_cache_miss",
                    "txn_num": txn_num,
                    "reason": "expired",
                    "age_s": age.total_seconds(),
                    "fields_hash": key.fields_hash,
                },
            )
            return None
        with self._lock:
            self._counters.hits += 1
        _log.debug(
            "document_cache hit",
            extra={
                "event": "document_cache_hit",
                "txn_num": txn_num,
                "age_s": age.total_seconds(),
                "fields_hash": key.fields_hash,
            },
        )
        return entry

    def put(
        self,
        *,
        txn_num: str,
        fields: Iterable[str],
        metadata: ResolvedMetadata,
        trigger_cif: str | None,
    ) -> None:
        """Hace upsert de la metadata resuelta junto con el CIF
        (posiblemente self-healed)."""
        entry = CacheEntry(
            txn_num=txn_num,
            fields_hash=compute_fields_hash(fields),
            trigger_cif=trigger_cif,
            properties=MappingProxyType(dict(metadata.properties)),
            cached_at=self._clock(),
        )
        self._cache.put(entry)

    # ---------- pass-throughs orientados al operador ----------

    def clear_txn(self, txn_num: str) -> int:
        return self._cache.clear_txn(txn_num)

    def clear_all(self) -> int:
        return self._cache.clear_all()

    def clear_older_than_minutes(self, minutes: int) -> int:
        threshold = self._clock() - timedelta(minutes=int(minutes))
        return self._cache.clear_older_than(threshold)

    def disk_stats(self) -> CacheStats:
        return self._cache.stats()

    def counters_snapshot(self) -> CacheCounters:
        with self._lock:
            return CacheCounters(
                hits=self._counters.hits,
                misses_absent=self._counters.misses_absent,
                misses_expired=self._counters.misses_expired,
            )
