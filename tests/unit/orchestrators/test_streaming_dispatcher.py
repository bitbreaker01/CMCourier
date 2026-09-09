"""Tests del dispatcher de lanes del modo streaming (115).

Pre-115 el dispatcher (un solo thread) hacía ``put`` BLOQUEANTE a la
cola de la lane destino: con la cola heavy llena (uploads lentos), el
dispatcher quedaba clavado sin drenar el bucket ni alimentar a los
consumers light ociosos — head-of-line blocking entre lanes.

115: ruteo con ``put_nowait`` + overflow acotado por lane; el poison
espera a que los overflows se vacíen (ningún item se pierde).
"""

from __future__ import annotations

import queue
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from cmcourier.orchestrators.streaming import _POISON, StreamingOrchestrator

pytestmark = pytest.mark.unit

_THRESHOLD = 1000


def _item(size: int) -> Any:
    return SimpleNamespace(staged_file=SimpleNamespace(size_bytes=size))


def _make_orchestrator(bucket_size: int = 4) -> StreamingOrchestrator:
    orch = StreamingOrchestrator.__new__(StreamingOrchestrator)
    orch._lanes_config = SimpleNamespace(heavy_threshold_bytes=_THRESHOLD)  # noqa: SLF001
    # 070: ``_lane_controller`` es una property read-through al pipeline.
    orch._pipeline = SimpleNamespace(lane_controller=None)  # noqa: SLF001
    orch._bucket_size = bucket_size  # noqa: SLF001
    orch._publish_pending_count = lambda: None  # type: ignore[method-assign] # noqa: SLF001
    # 144: contador de píldoras que el dispatcher mantiene al fan-outear.
    orch._pills_pending = 0  # noqa: SLF001
    orch._pills_lock = threading.Lock()  # noqa: SLF001
    return orch


def _run_dispatcher(
    orch: StreamingOrchestrator,
    bucket: queue.Queue[Any],
    heavy: queue.Queue[Any],
    light: queue.Queue[Any],
) -> threading.Thread:
    t = threading.Thread(
        target=orch._dispatcher_loop,  # noqa: SLF001
        args=(bucket, heavy, light, 1, 1),
        daemon=True,
    )
    t.start()
    return t


class TestHeadOfLine:
    def test_full_heavy_queue_does_not_starve_light(self) -> None:
        """E2: heavy llena y sin consumers → los light igual fluyen."""
        bucket: queue.Queue[Any] = queue.Queue()
        heavy: queue.Queue[Any] = queue.Queue(maxsize=1)
        light: queue.Queue[Any] = queue.Queue(maxsize=10)

        # 3 heavy (la cola heavy solo admite 1) seguidos de 2 light.
        for _ in range(3):
            bucket.put(_item(_THRESHOLD + 1))
        light_a, light_b = _item(1), _item(2)
        bucket.put(light_a)
        bucket.put(light_b)

        orch = _make_orchestrator(bucket_size=4)
        t = _run_dispatcher(orch, bucket, heavy, light)

        # Pre-115 el dispatcher quedaba bloqueado en el segundo heavy y
        # los light no llegaban nunca.
        got_a = light.get(timeout=5.0)
        got_b = light.get(timeout=5.0)
        assert got_a is light_a and got_b is light_b

        # Destrabar: drenar heavy y cerrar.
        drained = [heavy.get(timeout=5.0)]
        bucket.put(_POISON)
        while True:
            entry = heavy.get(timeout=5.0)
            if entry is _POISON:
                break
            drained.append(entry)
        assert len(drained) == 3
        assert light.get(timeout=5.0) is _POISON
        t.join(timeout=5.0)
        assert not t.is_alive()

    def test_poison_flushes_overflow_before_pills(self) -> None:
        """E3: los items en overflow se entregan antes que las pills."""
        bucket: queue.Queue[Any] = queue.Queue()
        heavy: queue.Queue[Any] = queue.Queue(maxsize=1)
        light: queue.Queue[Any] = queue.Queue(maxsize=10)

        items = [_item(_THRESHOLD + i) for i in range(3)]
        for it in items:
            bucket.put(it)
        bucket.put(_POISON)

        orch = _make_orchestrator(bucket_size=4)
        t = _run_dispatcher(orch, bucket, heavy, light)

        received: list[Any] = []
        while True:
            entry = heavy.get(timeout=5.0)
            if entry is _POISON:
                break
            received.append(entry)
        assert received == items, "se perdieron items del overflow con el poison"
        assert light.get(timeout=5.0) is _POISON
        t.join(timeout=5.0)
        assert not t.is_alive()

    def test_fifo_order_preserved_per_lane(self) -> None:
        bucket: queue.Queue[Any] = queue.Queue()
        heavy: queue.Queue[Any] = queue.Queue(maxsize=2)
        light: queue.Queue[Any] = queue.Queue(maxsize=2)
        h = [_item(_THRESHOLD + i) for i in range(4)]
        lt = [_item(i) for i in range(4)]
        # Intercalados.
        for pair in zip(h, lt, strict=True):
            bucket.put(pair[0])
            bucket.put(pair[1])
        bucket.put(_POISON)

        orch = _make_orchestrator(bucket_size=8)
        t = _run_dispatcher(orch, bucket, heavy, light)

        got_h: list[Any] = []
        got_l: list[Any] = []
        while len(got_h) < 4:
            got_h.append(heavy.get(timeout=5.0))
        while len(got_l) < 4:
            got_l.append(light.get(timeout=5.0))
        assert got_h == h
        assert got_l == lt
        assert heavy.get(timeout=5.0) is _POISON
        assert light.get(timeout=5.0) is _POISON
        t.join(timeout=5.0)
