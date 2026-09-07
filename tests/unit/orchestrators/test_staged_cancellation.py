"""Tests de cancelación cooperativa en StagedPipeline (097).

Con el `cancel_token` prendido, los métodos per-doc saltean el trabajo
sin contar fallas — eso habilita el *drain* cuando el operador cancela
desde el TUI.
"""

from __future__ import annotations

import threading
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from cmcourier.domain.exceptions import CMISClientError
from cmcourier.domain.models import (
    ClientTrigger,
    CMMapping,
    ResolvedMetadata,
    RVABREPDocument,
    StagedFile,
)
from cmcourier.orchestrators.staged import StagedPipeline, _StageItem
from cmcourier.services.cancellation import CancellationToken

pytestmark = pytest.mark.unit


def _pipeline(workers: int = 1) -> StagedPipeline:
    return StagedPipeline(
        trigger_strategy=MagicMock(),
        indexing_service=MagicMock(),
        mapping_service=MagicMock(),
        metadata_service=MagicMock(),
        assembler=MagicMock(),
        uploader=MagicMock(),
        tracking_store=MagicMock(),
        workers=workers,
    )


def _doc() -> RVABREPDocument:
    return RVABREPDocument(
        system_code="1",
        txn_num="TXN_C",
        index1="1",
        index2="1",
        index3="",
        index4="",
        index5="",
        index6="",
        index7="CC03",
        image_type="O",
        image_path="x",
        file_name="DOC.pdf",
        creation_date=datetime(2025, 11, 17),  # noqa: DTZ001
        last_view_date=None,
        total_pages=1,
        delete_code="",
    )


def _mapping() -> CMMapping:
    return CMMapping(
        clase_id="CC03",
        id_rvi="CC03",
        id_corto="CC03",
        clase_name="ClaseTest",
        required_metadata_fields=(),
    )


def _full_item(tmp_path) -> _StageItem:
    """Item con mapping + metadata + staged_file — listo para _upload_one."""
    p = tmp_path / "out.pdf"
    p.write_bytes(b"%PDF-1.4\n%%EOF\n")
    return _StageItem(
        trigger=ClientTrigger(shortname="SN", cif="1", system_id="1"),
        document=_doc(),
        mapping=_mapping(),
        metadata=ResolvedMetadata.from_dict({}),
        staged_file=StagedFile(path=p, size_bytes=p.stat().st_size, page_count=1),
    )


def test_cancel_token_property_exposes_the_token() -> None:
    pipeline = _pipeline()
    assert isinstance(pipeline.cancel_token, CancellationToken)
    assert pipeline.cancel_token.is_cancelled() is False


def test_upload_one_skips_when_cancelled(tmp_path) -> None:
    pipeline = _pipeline()
    item = _full_item(tmp_path)
    pipeline.cancel_token.cancel()

    outcome = pipeline._upload_one(item, "B1")

    assert outcome == "skipped"
    pipeline._uploader.upload.assert_not_called()  # no se subió nada


def test_s2_one_skips_when_cancelled() -> None:
    pipeline = _pipeline()
    item = _StageItem(
        trigger=ClientTrigger(shortname="SN", cif="1", system_id="1"),
        document=_doc(),
    )
    pipeline.cancel_token.cancel()

    survivor, counted_failure = pipeline._s2_one(item, "B1", pipeline._metrics)

    assert survivor is None
    assert counted_failure is False  # cancelar NO cuenta como falla
    pipeline._mapping_service.get_mapping.assert_not_called()


def test_s3_one_skips_when_cancelled() -> None:
    pipeline = _pipeline()
    item = _StageItem(
        trigger=ClientTrigger(shortname="SN", cif="1", system_id="1"),
        document=_doc(),
        mapping=_mapping(),
    )
    pipeline.cancel_token.cancel()

    survivor, counted_failure = pipeline._s3_one(item, "B1", pipeline._metrics)

    assert survivor is None
    assert counted_failure is False


def test_s4_one_skips_when_cancelled() -> None:
    pipeline = _pipeline()
    item = _StageItem(
        trigger=ClientTrigger(shortname="SN", cif="1", system_id="1"),
        document=_doc(),
        mapping=_mapping(),
    )
    pipeline.cancel_token.cancel()

    survivor, counted_failure = pipeline._s4_one(item, "B1", pipeline._metrics)

    assert survivor is None
    assert counted_failure is False
    pipeline._assembler.assemble_traced.assert_not_called()


def test_not_cancelled_upload_one_is_unaffected(tmp_path) -> None:
    """Sin cancelar, _upload_one sigue su curso normal (sube el doc)."""
    pipeline = _pipeline()
    item = _full_item(tmp_path)
    pipeline._tracking_store.is_stage_done.return_value = False
    pipeline._uploader.upload.return_value = "cm-obj-1"

    outcome = pipeline._upload_one(item, "B1")

    assert outcome == "done"
    pipeline._uploader.upload.assert_called_once()


# ------------------------------------------------------ 132: pausa en caliente


def _run_upload_in_thread(
    pipeline: StagedPipeline, item: _StageItem
) -> tuple[threading.Thread, list]:
    results: list[str] = []
    t = threading.Thread(target=lambda: results.append(pipeline._upload_one(item, "B1")))
    t.start()
    return t, results


def test_upload_one_waits_while_paused_then_uploads(tmp_path) -> None:
    pipeline = _pipeline()
    item = _full_item(tmp_path)
    pipeline._tracking_store.is_stage_done.return_value = False
    pipeline._uploader.upload.return_value = "cm-obj-1"
    pipeline.cancel_token.pause()

    t, results = _run_upload_in_thread(pipeline, item)
    t.join(0.3)
    assert t.is_alive(), "pausado: el worker espera antes de tomar trabajo"
    pipeline._uploader.upload.assert_not_called()

    pipeline.cancel_token.resume()
    t.join(3.0)
    assert results == ["done"]
    pipeline._uploader.upload.assert_called_once()


def test_upload_one_paused_then_cancelled_skips(tmp_path) -> None:
    pipeline = _pipeline()
    item = _full_item(tmp_path)
    pipeline.cancel_token.pause()

    t, results = _run_upload_in_thread(pipeline, item)
    t.join(0.3)
    assert t.is_alive()

    pipeline.cancel_token.cancel()
    t.join(3.0)
    assert results == ["skipped"]
    pipeline._uploader.upload.assert_not_called()


def test_s2_one_waits_while_paused() -> None:
    pipeline = _pipeline()
    item = _StageItem(
        trigger=ClientTrigger(shortname="SN", cif="1", system_id="1"), document=_doc()
    )
    pipeline.cancel_token.pause()
    results: list = []
    t = threading.Thread(
        target=lambda: results.append(pipeline._s2_one(item, "B1", pipeline._metrics))
    )
    t.start()
    t.join(0.3)
    assert t.is_alive()
    pipeline.cancel_token.cancel()
    t.join(3.0)
    assert results == [(None, False)]


# --------------------------------------------- 132: re-autenticación al 401


def _pipeline_with_401_then_ok(tmp_path, *, failures: int = 1, workers: int = 1):
    """Uploader que devuelve 401 `failures` veces y después sube OK."""
    pipeline = _pipeline(workers)
    item = _full_item(tmp_path)
    pipeline._tracking_store.is_stage_done.return_value = False
    calls = {"n": 0}

    def upload(**_kw):
        calls["n"] += 1
        if calls["n"] <= failures:
            raise CMISClientError(status_code=401, response_body="expired")
        return "cm-obj-1"

    pipeline._uploader.upload.side_effect = upload
    return pipeline, item, calls


def test_401_without_handler_fails_as_before(tmp_path) -> None:
    pipeline, item, calls = _pipeline_with_401_then_ok(tmp_path)

    outcome = pipeline._upload_one(item, "B1")

    assert outcome == "failed"
    assert calls["n"] == 1
    assert pipeline.cancel_token.is_paused() is False
    pipeline._uploader.set_credentials.assert_not_called()


def test_401_with_handler_pauses_and_retries_with_new_credentials(tmp_path) -> None:
    pipeline, item, calls = _pipeline_with_401_then_ok(tmp_path)
    fired: list[int] = []
    pipeline.set_auth_expired_handler(lambda: fired.append(1))

    t, results = _run_upload_in_thread(pipeline, item)
    t.join(1.0)
    assert t.is_alive(), "tras el 401 el worker espera en la compuerta"
    assert pipeline.cancel_token.is_paused() is True
    assert fired == [1]

    pipeline.set_cmis_credentials("nuevo", "clave")
    pipeline.cancel_token.resume()
    t.join(3.0)

    assert results == ["done"]
    assert calls["n"] == 2
    pipeline._uploader.set_credentials.assert_called_once_with("nuevo", "clave")
    pipeline._tracking_store.mark_stage_failed.assert_not_called()


def test_401_handler_fires_once_per_episode_across_workers(tmp_path) -> None:
    # workers=2: el que espera en la compuerta retiene su slot del semáforo
    # (está DENTRO de S5), así que el segundo necesita slot propio.
    pipeline, item, calls = _pipeline_with_401_then_ok(tmp_path, failures=2, workers=2)
    item2 = _full_item(tmp_path)
    fired: list[int] = []
    pipeline.set_auth_expired_handler(lambda: fired.append(1))
    # Barrera: los DOS POST están en vuelo antes de que cualquiera reciba
    # el 401 (si uno pausara antes, el otro se frenaría en la compuerta de
    # entrada y nunca vería el 401 — otro escenario, no éste).
    both_in_flight = threading.Barrier(2, timeout=2.0)
    inner = pipeline._uploader.upload.side_effect

    def upload(**kw):
        if calls["n"] < 2:
            both_in_flight.wait()
        return inner(**kw)

    pipeline._uploader.upload.side_effect = upload

    t1, r1 = _run_upload_in_thread(pipeline, item)
    t2, r2 = _run_upload_in_thread(pipeline, item2)
    t1.join(1.0)
    t2.join(1.0)
    assert t1.is_alive() and t2.is_alive()
    assert fired == [1], "un episodio de 401 → UN aviso, aunque choquen dos workers"

    pipeline.cancel_token.resume()
    t1.join(3.0)
    t2.join(3.0)
    assert r1 == ["done"] and r2 == ["done"]


def test_401_with_handler_then_cancel_marks_failed(tmp_path) -> None:
    pipeline, item, _calls = _pipeline_with_401_then_ok(tmp_path)
    pipeline.set_auth_expired_handler(lambda: None)

    t, results = _run_upload_in_thread(pipeline, item)
    t.join(1.0)
    assert t.is_alive()

    pipeline.cancel_token.cancel()
    t.join(3.0)
    assert results == ["failed"]
    pipeline._tracking_store.mark_stage_failed.assert_called_once()


def test_401_gives_up_after_two_reauth_episodes(tmp_path) -> None:
    """Tercer 401 consecutivo del mismo doc → failed (sin loop infinito)."""
    pipeline, item, calls = _pipeline_with_401_then_ok(tmp_path, failures=10)
    pipeline.set_auth_expired_handler(lambda: pipeline.cancel_token.resume())

    outcome = pipeline._upload_one(item, "B1")

    assert outcome == "failed"
    assert calls["n"] == 3
    assert pipeline.cancel_token.is_paused() is False


def test_401_after_fresh_credentials_retries_without_pausing(tmp_path) -> None:
    """Un 401 de un request que ya estaba en vuelo cuando se refrescaron las
    credenciales NO abre un episodio nuevo: reintenta directo."""
    pipeline, item, calls = _pipeline_with_401_then_ok(tmp_path)
    fired: list[int] = []
    pipeline.set_auth_expired_handler(lambda: fired.append(1))

    def upload(**_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            pipeline.set_cmis_credentials("nuevo", "clave")  # llega mientras el POST viaja
            raise CMISClientError(status_code=401)
        return "cm-obj-1"

    pipeline._uploader.upload.side_effect = upload

    outcome = pipeline._upload_one(item, "B1")

    assert outcome == "done"
    assert calls["n"] == 2
    assert fired == []
    assert pipeline.cancel_token.is_paused() is False


def test_reauth_wait_is_not_charged_to_s5_latency(tmp_path) -> None:
    """Antagonista I3: la espera de credenciales ocurría DENTRO del
    StageTimer de S5 y envenenaba el p95 que lee el AIMD (un doc de 40 s
    "de upload" → el AIMD recortaba workers al reanudar)."""
    pipeline, item, _calls = _pipeline_with_401_then_ok(tmp_path)

    def handler() -> None:
        threading.Timer(0.4, pipeline.cancel_token.resume).start()

    pipeline.set_auth_expired_handler(handler)

    assert pipeline._upload_one(item, "B1") == "done"

    p95_ms, count = pipeline._metrics.current_stage_p95_with_count("S5")
    assert count == 1
    assert p95_ms < 300, f"S5 cobró la espera de re-auth: p95={p95_ms:.0f} ms"


def test_401_during_manual_pause_still_fires_the_handler(tmp_path) -> None:
    """Antagonista I7: con la corrida pausada a mano ANTES del 401, el
    handler se gateaba en ``is_paused()`` y nunca avisaba — el operador
    reanudaba y el doc volvía a fallar por credenciales viejas."""
    pipeline, item, _calls = _pipeline_with_401_then_ok(tmp_path)
    fired: list[int] = []
    pipeline.set_auth_expired_handler(lambda: fired.append(1))
    pipeline.cancel_token.pause()  # pausa manual, la compuerta ya está cerrada

    # Entramos por _upload_with_reauth directo: la compuerta de entrada de
    # _upload_one frenaría al worker antes del POST (otro escenario).
    t = threading.Thread(
        target=lambda: pipeline._upload_with_reauth(item, "B1", "TXN_C", "/f", "t"),
        daemon=True,
    )
    t.start()
    t.join(1.0)
    assert t.is_alive()
    assert fired == [1]

    pipeline.cancel_token.resume()
    t.join(3.0)
    assert not t.is_alive()


def test_reauth_episode_closes_on_resume_so_the_next_401_fires_again(tmp_path) -> None:
    """Complemento de I7: el episodio se cierra al reanudar; un 401 posterior
    (credenciales nuevas también vencidas) vuelve a avisar."""
    pipeline, item, _calls = _pipeline_with_401_then_ok(tmp_path, failures=2)
    fired: list[int] = []

    def handler() -> None:
        fired.append(1)
        pipeline.cancel_token.resume()

    pipeline.set_auth_expired_handler(handler)
    assert pipeline._upload_one(item, "B1") == "done"
    assert fired == [1, 1]


def test_handler_exception_does_not_kill_the_worker(tmp_path) -> None:
    """Antagonista I8: si el handler del TUI explota, el worker no muere
    con el slot tomado — la corrida queda pausada y espera igual."""
    pipeline, item, _calls = _pipeline_with_401_then_ok(tmp_path)

    def handler() -> None:
        raise RuntimeError("el TUI se cayó")

    pipeline.set_auth_expired_handler(handler)

    t, results = _run_upload_in_thread(pipeline, item)
    t.join(1.0)
    assert t.is_alive(), "el worker sigue esperando en la compuerta"
    assert pipeline.cancel_token.is_paused() is True

    pipeline.cancel_token.resume()
    t.join(3.0)
    assert results == ["done"]


def test_second_episode_fires_handler_again(tmp_path) -> None:
    pipeline, item, _calls = _pipeline_with_401_then_ok(tmp_path, failures=2)
    fired: list[int] = []

    def handler() -> None:
        fired.append(1)
        pipeline.cancel_token.resume()

    pipeline.set_auth_expired_handler(handler)

    outcome = pipeline._upload_one(item, "B1")

    assert outcome == "done"
    assert fired == [1, 1]
