"""ConsoleRunManager — pausa / reanudar / re-auth en caliente (132).

El manager se prueba con un app doble (MagicMock) y un orchestrator con un
`CancellationToken` REAL: lo que importa es qué le pasa al token y al
pipeline, no la plomería de Textual.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from cmcourier.cli.console.runner import ConsoleRunManager, LaunchSpec
from cmcourier.config.loader import Credential
from cmcourier.services.cancellation import CancellationToken

pytestmark = pytest.mark.unit


def _manager(*, cmis_status: str = "ok") -> ConsoleRunManager:
    app = MagicMock()
    app.state.creds.get.return_value = Credential("nuevo", "clave")
    app.state.conn = {"cmis": MagicMock(status=cmis_status)}
    mgr = ConsoleRunManager(app)
    mgr.orchestrator = MagicMock()
    mgr.orchestrator.cancel_token = CancellationToken()
    mgr.pipeline = MagicMock()
    return mgr


def test_idle_manager_is_not_paused() -> None:
    mgr = ConsoleRunManager(MagicMock())
    assert mgr.paused is False
    assert mgr.reauth_pending is False
    mgr.pause()  # sin corrida: no-op
    mgr.resume()


def test_pause_closes_the_gate_and_resume_opens_it() -> None:
    mgr = _manager()
    mgr.pause()
    assert mgr.paused is True
    assert mgr.orchestrator.cancel_token.is_paused() is True
    mgr.resume()
    assert mgr.paused is False
    assert mgr.orchestrator.cancel_token.is_paused() is False
    mgr.pipeline.set_cmis_credentials.assert_not_called()


def test_auth_expired_handler_flags_and_notifies_app_on_ui_thread() -> None:
    mgr = _manager()
    mgr.orchestrator.cancel_token.pause()  # el pipeline ya pausó antes de avisar

    mgr._on_auth_expired()

    assert mgr.reauth_pending is True
    assert mgr.paused is True
    mgr.app.call_from_thread.assert_called_once_with(mgr.app.on_auth_expired, mgr)


def test_resume_after_reauth_pushes_cmis_credentials_first() -> None:
    mgr = _manager()
    mgr.orchestrator.cancel_token.pause()
    mgr._on_auth_expired()

    mgr.resume()

    mgr.app.state.creds.get.assert_called_with("cmis")
    mgr.pipeline.set_cmis_credentials.assert_called_once_with("nuevo", "clave")
    assert mgr.reauth_pending is False
    assert mgr.paused is False


def test_adjust_workers_delegates_to_the_pipeline() -> None:
    """133: el manager sólo delega; el clamp vive en el pipeline."""
    mgr = _manager()
    mgr.pipeline.adjust_worker_cap.return_value = 3
    assert mgr.adjust_workers(-1) == 3
    mgr.pipeline.adjust_worker_cap.assert_called_once_with(-1)


def test_adjust_workers_without_run_is_none() -> None:
    mgr = ConsoleRunManager(MagicMock())
    assert mgr.adjust_workers(+1) is None


def test_outcome_ignores_pause() -> None:
    mgr = _manager()
    mgr.pause()
    assert mgr.outcome() == "completed"


def test_launch_registers_the_auth_expired_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    import cmcourier.cli.console.runner as runner_mod

    app = MagicMock()
    mgr = ConsoleRunManager(app)
    pipeline = MagicMock()
    monkeypatch.setattr(runner_mod, "apply_overrides", lambda cfg, ov: cfg)
    monkeypatch.setattr(runner_mod, "configure_observability", lambda *a, **k: None)
    monkeypatch.setattr(runner_mod, "acquire_config_lock", lambda _p: MagicMock())
    monkeypatch.setattr(runner_mod, "build_data_provider", lambda *a, **k: MagicMock())

    def fake_build(_effective, _spec):
        mgr.orchestrator = MagicMock()
        mgr.orchestrator.cancel_token = CancellationToken()
        mgr.orchestrator.run.return_value = None
        return pipeline, {}

    monkeypatch.setattr(mgr, "_build", fake_build)

    mgr.launch(LaunchSpec())
    mgr.join(5.0)

    pipeline.set_auth_expired_handler.assert_called_once_with(mgr._on_auth_expired)


def test_launch_passes_planned_total_to_the_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """134: la ETA de corrida necesita el total elegido en [5]."""
    import cmcourier.cli.console.runner as runner_mod

    app = MagicMock()
    mgr = ConsoleRunManager(app)
    seen: dict[str, object] = {}

    def fake_provider(*_a: object, **kw: object) -> MagicMock:
        seen.update(kw)
        return MagicMock()

    monkeypatch.setattr(runner_mod, "apply_overrides", lambda cfg, ov: cfg)
    monkeypatch.setattr(runner_mod, "configure_observability", lambda *a, **k: None)
    monkeypatch.setattr(runner_mod, "acquire_config_lock", lambda _p: MagicMock())
    monkeypatch.setattr(runner_mod, "build_data_provider", fake_provider)

    def fake_build(_effective, _spec):
        mgr.orchestrator = MagicMock()
        mgr.orchestrator.cancel_token = CancellationToken()
        mgr.orchestrator.run.return_value = None
        return MagicMock(), {}

    monkeypatch.setattr(mgr, "_build", fake_build)

    mgr.launch(LaunchSpec(total=500))
    mgr.join(5.0)

    assert seen["planned_total"] == 500
