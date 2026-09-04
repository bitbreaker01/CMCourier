"""Tests de la higiene del uploader (116).

* P7: el TokenBucket acumulaba tokens sin techo — tras una pausa, la
  primera ráfaga salía sin throttling.
* P18: ``BandwidthLimiter.read`` cobraba por el chunk pedido, no por
  los bytes realmente leídos (EOF y lecturas cortas pagaban de más).
* P9: ``httpx.Timeout(300)`` uniforme — un host caído tardaba 5
  minutos en fallar el connect.
"""

from __future__ import annotations

import io

import httpx
import pytest

from cmcourier.adapters.upload.cmis_uploader import (
    _CONNECT_TIMEOUT_S,
    BandwidthLimiter,
    CmisConfig,
    CmisUploader,
    TokenBucket,
)

pytestmark = pytest.mark.unit


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class TestBurstCap:
    def test_long_idle_does_not_accumulate_unbounded_tokens(self) -> None:
        # E1: 1 MB/s (8 Mbps) idle 100 s → a lo sumo ~1 s de presupuesto
        # (1 MB) disponible sin dormir; el segundo MB paga la espera.
        clock = _FakeClock()
        bucket = TokenBucket(mbps=8.0, clock=clock, sleep=clock.sleep)
        clock.now += 100.0  # pausa larga

        bucket.consume(1_000_000)  # el burst permitido (1 s de rate)
        assert clock.slept == [], "el burst de 1 s no debe dormir"

        bucket.consume(1_000_000)  # el segundo MB debe pagar ~1 s
        assert sum(clock.slept) == pytest.approx(1.0, rel=0.05), (
            f"la ráfaga post-pausa no está acotada: slept={clock.slept}"
        )

    def test_sustained_rate_unchanged(self) -> None:
        # E4: consumo sostenido a la tasa configurada — 10 × 100 KB a
        # 1 MB/s ≈ 1 s de sleeps acumulados.
        clock = _FakeClock()
        bucket = TokenBucket(mbps=8.0, clock=clock, sleep=clock.sleep)
        for _ in range(10):
            bucket.consume(100_000)
        assert sum(clock.slept) == pytest.approx(1.0, rel=0.05)

    def test_disabled_bucket_never_sleeps(self) -> None:
        clock = _FakeClock()
        bucket = TokenBucket(mbps=0.0, clock=clock, sleep=clock.sleep)
        clock.now += 500.0
        bucket.consume(10**9)
        assert clock.slept == []


class _RecordingBucket(TokenBucket):
    def __init__(self) -> None:
        super().__init__(mbps=8.0)
        self.consumed: list[int] = []

    def consume(self, n_bytes: int) -> None:
        self.consumed.append(n_bytes)


class TestChargeForActualBytes:
    def test_short_read_charges_actual_length(self) -> None:
        # E2: el stream tiene 10 KB; pedir 1 MiB cobra 10 KB.
        bucket = _RecordingBucket()
        limiter = BandwidthLimiter(io.BytesIO(b"x" * 10_240), bucket)
        data = limiter.read(1 << 20)
        assert len(data) == 10_240
        assert bucket.consumed == [10_240]

    def test_eof_charges_nothing(self) -> None:
        bucket = _RecordingBucket()
        limiter = BandwidthLimiter(io.BytesIO(b""), bucket)
        assert limiter.read(1 << 20) == b""
        assert bucket.consumed in ([], [0])

    def test_full_read_charges_full_length(self) -> None:
        bucket = _RecordingBucket()
        limiter = BandwidthLimiter(io.BytesIO(b"x" * 4096), bucket)
        assert len(limiter.read(4096)) == 4096
        assert bucket.consumed == [4096]


class TestGranularTimeouts:
    def _make_uploader(self, timeout_seconds: float) -> CmisUploader:
        return CmisUploader(
            CmisConfig(
                base_url="http://cm.test/cmis",
                repo_id="repo",
                username="u",
                password="p",
                timeout_seconds=timeout_seconds,
                verify_ssl=False,
                max_bandwidth_mbps=0.0,
                retry_max_attempts=1,
                retry_base_delay_s=0.01,
                pool_size=2,
                unmask_pii=False,
            )
        )

    def test_client_connect_timeout_is_capped(self) -> None:
        # E3: read/write conservan los 300 s; connect se capea a 10.
        uploader = self._make_uploader(300.0)
        timeout = uploader._client.timeout  # noqa: SLF001
        assert timeout.connect == _CONNECT_TIMEOUT_S
        assert timeout.read == 300.0
        assert timeout.write == 300.0

    def test_request_timeout_follows_live_value_with_capped_connect(self) -> None:
        uploader = self._make_uploader(300.0)
        uploader._timeout_s = 45.0  # noqa: SLF001 — el AIMD lo ajusta en vivo
        timeout = uploader._request_timeout()  # noqa: SLF001
        assert isinstance(timeout, httpx.Timeout)
        assert timeout.connect == _CONNECT_TIMEOUT_S
        assert timeout.read == 45.0

    def test_small_configured_timeout_caps_connect_below_ten(self) -> None:
        uploader = self._make_uploader(5.0)
        timeout = uploader._client.timeout  # noqa: SLF001
        assert timeout.connect == 5.0
