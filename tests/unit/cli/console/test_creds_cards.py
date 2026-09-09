"""131 — tarjetas de credenciales DINÁMICAS: una por alias del registro (129)
más CMIS; intentos/lockout sólo en as400; INICIO y el guard de lanzamiento
exigen TODAS las conexiones de la config efectiva."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.widgets import Input, Static

import cmcourier.cli.console.app as app_module
from cmcourier.cli.console.app import ConsoleApp
from cmcourier.cli.console.creds_pane import CredsPane, run_single_check
from cmcourier.cli.doctor import CheckResult, CheckStatus
from cmcourier.config.loader import load_config
from cmcourier.config.schema import PipelineConfig
from tests.unit.cli.console.conftest import goto, wait_for
from tests.unit.cli.console.test_console_app import _make_config

pytestmark = pytest.mark.unit


def _registry_config(tmp_path: Path) -> tuple[PipelineConfig, Path]:
    """CSV de indexing + as400 `rvi` para el sync + mssql `clientes_sql` de metadata."""
    _, path = _make_config(tmp_path)
    text = path.read_text()
    text = (
        "connections:\n"
        "  rvi: {kind: as400, host: as400.test, port: 446, database: RVILIB}\n"
        "  clientes_sql: {kind: mssql, host: sql.test, database: cmcourier}\n"
    ) + text
    text = text.replace(
        "metadata:\n  field_aliases: {}\n  field_sources: {}\n",
        "metadata:\n  field_aliases: {}\n  field_sources: {}\n  sources:\n"
        "    - kind: mssql\n      alias: clientes\n      connection: clientes_sql\n"
        "      table: dbo.clientes\n",
    )
    text = text.replace(
        "tracking:\n",
        "tracking:\n  as400_sync:\n    enabled: true\n    connection: rvi\n",
    )
    path.write_text(text)
    return load_config(path), path


def _pass(alias: str, console: ConsoleApp) -> tuple[CheckResult, float]:
    return CheckResult(name=alias, status=CheckStatus.PASS, message="conectado"), 1.0


class TestCards:
    def test_one_card_per_alias_plus_cmis(self, tmp_path: Path) -> None:
        """E2: cmis + rvi (as400) + clientes_sql (mssql); sin tarjeta `as400` fija."""

        async def _run() -> None:
            config, path = _registry_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "2")
                pane = app.query_one(CredsPane)
                ids = [c.id for c in pane.query(".card")]
                assert ids == ["card-cmis", "card-clientes_sql", "card-rvi"]
                title = str(app.query_one("#title-clientes_sql", Static).renderable)
                assert title == "clientes_sql · mssql · sql.test"
                sites = str(app.query_one("#sites-rvi", Static).renderable)
                assert "tracking" in sites
                assert list(app.state.conn) == ["cmis", "clientes_sql", "rvi"]

        asyncio.run(_run())

    def test_csv_only_config_shows_only_cmis(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _make_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "2")
                ids = [c.id for c in app.query_one(CredsPane).query(".card")]
                assert ids == ["card-cmis"]
                assert "solo CMIS" in str(app.query_one("#creds-hint", Static).renderable)

        asyncio.run(_run())

    def test_env_prefill_per_alias(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """E4: `CLIENTES_SQL_USERNAME` aparece en su tarjeta; `AS400_*` NO toca `rvi`."""
        monkeypatch.setenv("CLIENTES_SQL_USERNAME", "sa")
        monkeypatch.setenv("CLIENTES_SQL_PASSWORD", "pw")
        monkeypatch.setenv("AS400_USERNAME", "viejo")
        monkeypatch.delenv("RVI_USERNAME", raising=False)

        async def _run() -> None:
            config, path = _registry_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "2")
                assert app.query_one("#user-clientes_sql", Input).value == "sa"
                assert app.query_one("#user-rvi", Input).value == ""
                assert app.state.creds.complete("clientes_sql")

        asyncio.run(_run())

    def test_typing_routes_to_its_alias_and_test_uses_check_connection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """E3: probar la tarjeta mssql pasa por `check_connection(..., alias)`, no por
        el check de AS400."""
        seen: list[str] = []

        def _fake(alias: str, console: ConsoleApp) -> tuple[CheckResult, float]:
            seen.append(alias)
            return _pass(alias, console)

        monkeypatch.setattr(app_module, "run_single_check", _fake)

        async def _run() -> None:
            config, path = _registry_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "2")
                app.query_one("#user-clientes_sql", Input).value = "sa"
                app.query_one("#pass-clientes_sql", Input).value = "pw"
                await pilot.pause()  # Input.Changed es un mensaje: se procesa async
                assert app.state.creds.get("clientes_sql").username == "sa"
                app.query_one("#test-clientes_sql").focus()
                await pilot.press("enter")
                await pilot.pause()
                await app.workers.wait_for_complete()
                assert await wait_for(pilot, lambda: app.state.conn["clientes_sql"].status == "ok")
                assert seen == ["clientes_sql"]
                assert app.state.conn["rvi"].status == "idle"

        asyncio.run(_run())

    def test_tries_label_only_on_as400_cards(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _registry_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "2")
                pane = app.query_one(CredsPane)
                app.state.record_conn_result("rvi", ok=False, message="401")
                app.state.record_conn_result("clientes_sql", ok=False, message="18456")
                pane.render_all()
                assert "intento 1 de 3" in str(app.query_one("#tries-rvi", Static).renderable)
                assert len(app.query("#tries-clientes_sql")) == 0

        asyncio.run(_run())

    @pytest.mark.parametrize("size", [(120, 50), (100, 45)])
    def test_every_card_shows_test_button_and_status_chip(
        self, tmp_path: Path, size: tuple[int, int]
    ) -> None:
        """Queja del operador: "probar conexión" sólo se veía en CMIS. Un `Static`
        sin `width` en un `Horizontal` llena la fila (Textual: "fill available
        space") y empuja el botón, `editar`/`quitar` y el chip de estado fuera
        de la tarjeta. Cada widget de acción/estado tiene que caer DENTRO del
        área de su tarjeta — en 120 y en 100 columnas."""

        async def _run() -> None:
            config, path = _registry_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test(size=size) as pilot:
                await goto(pilot, app, "2")
                app.state.record_conn_result("rvi", ok=False, message="401")
                app.query_one(CredsPane).render_all()
                await pilot.pause()
                for alias in ("cmis", "clientes_sql", "rvi"):
                    card = app.query_one(f"#card-{alias}").region
                    for wid in app.query(f"#card-{alias} Button, #card-{alias} Static"):
                        if wid.id and wid.id.startswith(("msg-", "sites-")):
                            continue
                        r = wid.region
                        assert r.width > 0 and r.x + r.width <= card.x + card.width, (
                            f"{wid.id} se sale de la tarjeta {alias}: {r} vs {card} en {size}"
                        )

        asyncio.run(_run())

    @pytest.mark.parametrize("size", [(100, 30), (120, 24)])
    def test_pane_scrolls_to_reach_every_card(self, tmp_path: Path, size: tuple[int, int]) -> None:
        """Queja del operador: con 3 conexiones la tercera tarjeta queda cortada
        abajo y "no puedo hacer scroll down". `CredsPane` era un `Vertical`
        (overflow oculto): la grilla de 2 columnas mide más que la terminal y
        no hay forma de llegar. Tras `scroll_end` TODAS las tarjetas tienen que
        caer dentro de la pantalla (y ninguna se sale por la derecha)."""

        async def _run() -> None:
            config, path = _registry_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test(size=size) as pilot:
                await goto(pilot, app, "2")
                pane = app.query_one(CredsPane)
                assert pane.max_scroll_y > 0, f"la grilla entra entera en {size}: test sin valor"
                width, height = size
                for alias in ("cmis", "clientes_sql", "rvi"):
                    r = app.query_one(f"#card-{alias}").region
                    assert r.x + r.width <= width, f"{alias} se sale por la derecha: {r} en {size}"
                last = app.query_one("#card-rvi").region
                assert last.y + last.height > height, f"rvi ya entra sin scroll en {size}"
                pane.scroll_end(animate=False)
                await pilot.pause()
                last = app.query_one("#card-rvi").region
                assert last.y + last.height <= height, f"rvi no se alcanza: {last} en {size}"

        asyncio.run(_run())

    def test_query_phase_failure_does_not_count_as_lockout_try(self, tmp_path: Path) -> None:
        """Queja del operador: "intento 2 de 3 — al 3° el perfil se bloquea"
        tras un `AS400 query failed`. Si el driver ya aceptó el login, el
        fallo de la consulta de prueba NO es un sign-on inválido: la tarjeta
        no suma intento ni muestra la cuenta regresiva. Un fallo de conexión
        (`phase=connect`) sí suma."""

        async def _run() -> None:
            config, path = _registry_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "2")
                pane = app.query_one(CredsPane)
                fail = CheckResult(
                    name="connection:rvi",
                    status=CheckStatus.FAIL,
                    message="conectó pero la consulta de prueba falló",
                    details={"phase": "query"},
                )
                pane._apply_result("rvi", fail, 12.0)
                await pilot.pause()
                assert app.state.conn["rvi"].attempts == 0
                assert str(app.query_one("#tries-rvi", Static).renderable) == ""
                assert not app.state.as400_needs_lockout_confirm("rvi")

                connect_fail = CheckResult(
                    name="connection:rvi",
                    status=CheckStatus.FAIL,
                    message="no conectó",
                    details={"phase": "connect"},
                )
                pane._apply_result("rvi", connect_fail, 12.0)
                await pilot.pause()
                assert app.state.conn["rvi"].attempts == 1

        asyncio.run(_run())


class TestRecompose:
    """Antagonista 129-131 I1/I2: la rama de remonte de `rebuild_cards` hoy
    es defensiva (los overrides 127 no cambian conexiones), pero si corre
    NO puede destruir estado ni reventar el message-loop."""

    def test_remount_keeps_tested_connections(self, tmp_path: Path) -> None:
        """I1: `Input(value=...)` al montar dispara `Input.Changed`; eso NO debe
        invalidar una conexión que sobrevivió al rebuild."""

        async def _run() -> None:
            config, path = _make_config(tmp_path)
            (tmp_path / "b").mkdir()
            registry, _ = _registry_config(tmp_path / "b")
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "2")
                app.state.creds.set("cmis", "admin", "admin")
                app.state.record_conn_result("cmis", ok=True, message="ok")
                app.effective_config = lambda: registry  # type: ignore[method-assign]
                await app.query_one(CredsPane).rebuild_cards()
                await pilot.pause()
                ids = [c.id for c in app.query_one(CredsPane).query(".card")]
                assert ids == ["card-cmis", "card-clientes_sql", "card-rvi"]
                assert app.state.conn["cmis"].status == "ok"
                assert app.query_one("#user-cmis", Input).value == "admin"

        asyncio.run(_run())

    def test_late_worker_result_for_vanished_alias_does_not_raise(self, tmp_path: Path) -> None:
        """I2: el callback del worker llega después de que la tarjeta se fue."""

        async def _run() -> None:
            config, path = _registry_config(tmp_path)
            (tmp_path / "b").mkdir()
            csv_only, _ = _make_config(tmp_path / "b")
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await goto(pilot, app, "2")
                pane = app.query_one(CredsPane)
                app.effective_config = lambda: csv_only  # type: ignore[method-assign]
                await pane.rebuild_cards()
                await pilot.pause()
                assert list(app.state.conn) == ["cmis"]
                pane._apply_result("rvi", _pass("rvi", app)[0], 12.0)
                await pilot.pause()
                assert list(app.state.conn) == ["cmis"]

        asyncio.run(_run())


class TestGates:
    def test_inicio_and_launch_require_every_alias(self, tmp_path: Path) -> None:
        """E1: cmis OK no alcanza; INICIO lista cada conexión con su estado."""

        async def _run() -> None:
            config, path = _registry_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                app.state.record_conn_result("cmis", ok=True, message="ok")
                app.state.record_conn_result("rvi", ok=True, message="ok")
                assert app.required_aliases() == ("clientes_sql", "rvi")
                assert not app.state.creds_ready(required=app.required_aliases())
                await goto(pilot, app, "1")
                body = str(app.query_one("#inicio-body", Static).renderable)
                assert "clientes_sql" in body and "rvi" in body
                assert "AS400" not in body
                app.state.record_conn_result("clientes_sql", ok=True, message="ok")
                assert app.state.creds_ready(required=app.required_aliases())

        asyncio.run(_run())

    def test_top_status_lists_each_alias(self, tmp_path: Path) -> None:
        async def _run() -> None:
            config, path = _registry_config(tmp_path)
            app = ConsoleApp(config=config, config_path=path)
            async with app.run_test() as pilot:
                await pilot.pause()
                status = str(app.query_one("#top-status", Static).renderable)
                assert "cmis ·" in status and "rvi ·" in status and "clientes_sql ·" in status

        asyncio.run(_run())


class TestRunSingleCheck:
    def test_dispatches_cmis_vs_alias(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import cmcourier.cli.console.creds_pane as pane_module

        calls: list[tuple[str, str]] = []
        monkeypatch.setattr(
            pane_module,
            "check_cmis",
            lambda cfg, sec: calls.append(("cmis", "")) or _pass("cmis", None)[0],  # type: ignore[arg-type]
        )
        monkeypatch.setattr(
            pane_module,
            "check_connection",
            lambda cfg, sec, alias: calls.append(("conn", alias)) or _pass(alias, None)[0],  # type: ignore[arg-type]
        )
        config, path = _registry_config(tmp_path)
        app = ConsoleApp(config=config, config_path=path)
        run_single_check("cmis", app)
        run_single_check("clientes_sql", app)
        assert calls == [("cmis", ""), ("conn", "clientes_sql")]
