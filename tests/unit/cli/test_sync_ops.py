"""128: operaciones puras del sync SQLite ↔ NIARVILOG (sin click)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import cmcourier.cli.sync_ops as ops
from cmcourier.cli.sync_ops import (
    SyncOpError,
    sync_pull,
    sync_recover,
    sync_resolve,
    sync_status,
    sync_unavailable_reason,
)
from cmcourier.config.loader import Credential, Secrets
from cmcourier.config.schema import As400ConnectionConfig, ConnectionRef
from cmcourier.services.pull import PullItem, PullResult

pytestmark = pytest.mark.unit


def _config(*, enabled: bool = True, connection: str | None = "DSN=x") -> MagicMock:
    cfg = MagicMock()
    cfg.tracking.as400_sync.enabled = enabled
    cfg.tracking.as400_sync.connection = connection
    # 129: sync_ops resuelve la conexión vía connection_ref("tracking.as400_sync").
    ref = None
    if connection is not None:
        ref = ConnectionRef(
            alias="as400",
            kind="as400",
            spec=As400ConnectionConfig(host="as400.test"),
            site="tracking.as400_sync",
        )
    cfg.connection_ref.return_value = ref
    return cfg


def _secrets(user: str = "u", pwd: str = "p") -> Secrets:
    return Secrets({"cmis": Credential("c", "c"), "as400": Credential(user, pwd)})


class TestAvailability:
    def test_disabled_has_reason(self) -> None:
        reason = sync_unavailable_reason(_config(enabled=False), _secrets())
        assert reason is not None and "as400_sync" in reason

    def test_missing_connection_has_reason(self) -> None:
        reason = sync_unavailable_reason(_config(connection=None), _secrets())
        assert reason is not None and "connection" in reason

    def test_missing_creds_has_reason(self) -> None:
        reason = sync_unavailable_reason(_config(), _secrets(user="", pwd=""))
        assert reason is not None and "AS400" in reason

    def test_all_good_is_none(self) -> None:
        assert sync_unavailable_reason(_config(), _secrets()) is None


class TestResolve:
    def _stores(self, row: object) -> tuple[MagicMock, MagicMock]:
        sqlite, as400 = MagicMock(), MagicMock()
        as400.read_state_by_txn.return_value = row
        as400.mark_uploaded_by_txn.return_value = 1
        return sqlite, as400

    def test_prefer_local_requires_cm_object_id(self) -> None:
        with pytest.raises(SyncOpError, match="cm_object_id"):
            sync_resolve(_config(), _secrets(), txn="1", prefer="local", cm_object_id=None)

    def test_txn_missing_in_as400_raises(self) -> None:
        sqlite, as400 = self._stores(None)
        with (
            patch.object(ops, "build_sync_stores", return_value=(sqlite, as400)),
            pytest.raises(SyncOpError, match="not found"),
        ):
            sync_resolve(_config(), _secrets(), txn="1", prefer="as400", cm_object_id=None)
        sqlite.close.assert_called_once()
        as400.close.assert_called_once()

    def test_prefer_local_pushes_object_id(self) -> None:
        row = MagicMock(stscod="I", objidn="")
        sqlite, as400 = self._stores(row)
        with patch.object(ops, "build_sync_stores", return_value=(sqlite, as400)):
            msg = sync_resolve(
                _config(), _secrets(), txn="7", prefer="local", cm_object_id="cmis-1"
            )
        as400.mark_uploaded_by_txn.assert_called_once_with(trnnum="7", cm_object_id="cmis-1")
        assert "cmis-1" in msg
        sqlite.close.assert_called_once()
        as400.close.assert_called_once()

    def test_prefer_local_with_txn_missing_raises_and_closes(self) -> None:
        sqlite, as400 = self._stores(None)
        with (
            patch.object(ops, "build_sync_stores", return_value=(sqlite, as400)),
            pytest.raises(SyncOpError, match="not present"),
        ):
            sync_resolve(_config(), _secrets(), txn="7", prefer="local", cm_object_id="cmis-1")
        as400.mark_uploaded_by_txn.assert_not_called()
        sqlite.close.assert_called_once()
        as400.close.assert_called_once()

    def test_prefer_as400_rejects_non_terminal_row(self) -> None:
        sqlite, as400 = self._stores(MagicMock(stscod="I", objidn=""))
        with (
            patch.object(ops, "build_sync_stores", return_value=(sqlite, as400)),
            pytest.raises(SyncOpError, match="STSCOD"),
        ):
            sync_resolve(_config(), _secrets(), txn="7", prefer="as400", cm_object_id=None)
        as400.close.assert_called_once()


class TestStatus:
    def test_status_cleans_stale_and_closes_both_stores(self) -> None:
        sqlite, as400 = MagicMock(), MagicMock()
        as400.cleanup_stale_in_progress.return_value = 3
        as400.stream_rows_by_status.return_value = iter([])
        with patch.object(ops, "build_sync_stores", return_value=(sqlite, as400)):
            result = sync_status(_config(), _secrets())
        assert result.stale_cleaned == 3
        sqlite.close.assert_called_once()
        as400.close.assert_called_once()

    def test_status_reports_divergences_without_writing(self) -> None:
        """151 REQ-005: ``sync status`` decía reportar conflictos y sólo
        limpiaba los ``'I'`` vencidos. Ahora los reporta de verdad — es la
        misma consulta del pull, sin escribir."""
        sqlite, as400 = MagicMock(), MagicMock()
        as400.cleanup_stale_in_progress.return_value = 0
        divergente = PullItem("0000001", "AS400 STSCOD='F' vs local S5_DONE")
        report = PullResult(
            scanned=9, imported_uploaded=2, imported_failed=1, consistent=5, divergent=[divergente]
        )
        with (
            patch.object(ops, "build_sync_stores", return_value=(sqlite, as400)),
            patch.object(ops, "As400Pull") as pull_cls,
        ):
            pull_cls.return_value.pull.return_value = report
            result = sync_status(_config(), _secrets())

        assert result.divergences == [divergente]
        assert result.scanned == 9
        assert result.importable == 3  # 2 'O' + 1 'F' que el tracking no tiene
        assert pull_cls.return_value.pull.call_args.kwargs["apply"] is False
        # read-only: ninguna escritura al tracking local
        sqlite.record_external_upload.assert_not_called()
        sqlite.record_external_failure.assert_not_called()

    def test_status_unavailable_raises(self) -> None:
        with pytest.raises(SyncOpError, match="as400_sync"):
            sync_status(_config(enabled=False), _secrets())


class TestPull151:
    def test_pull_forwards_apply_and_progress_then_closes(self) -> None:
        sqlite, as400 = MagicMock(), MagicMock()

        def on_progress(_: object) -> None:
            pass

        with (
            patch.object(ops, "build_sync_stores", return_value=(sqlite, as400)),
            patch.object(ops, "As400Pull") as pull_cls,
        ):
            pull_cls.return_value.pull.return_value = PullResult(scanned=4)
            result = sync_pull(_config(), _secrets(), apply=True, on_progress=on_progress)

        assert result.scanned == 4
        kwargs = pull_cls.return_value.pull.call_args.kwargs
        assert kwargs["apply"] is True
        assert kwargs["on_progress"] is on_progress
        pull_cls.return_value.close.assert_called_once()  # cierra el store AS400
        sqlite.close.assert_called_once()

    def test_pull_closes_stores_when_the_scan_explodes(self) -> None:
        sqlite, as400 = MagicMock(), MagicMock()
        with (
            patch.object(ops, "build_sync_stores", return_value=(sqlite, as400)),
            patch.object(ops, "As400Pull") as pull_cls,
            pytest.raises(RuntimeError),
        ):
            pull_cls.return_value.pull.side_effect = RuntimeError("AS400 caído")
            sync_pull(_config(), _secrets(), apply=True)
        pull_cls.return_value.close.assert_called_once()
        sqlite.close.assert_called_once()

    def test_pull_unavailable_raises(self) -> None:
        with pytest.raises(SyncOpError, match="as400_sync"):
            sync_pull(_config(enabled=False), _secrets(), apply=False)


class TestRecover:
    def test_recover_closes_recovery_and_sqlite(self) -> None:
        recovery, sqlite = MagicMock(), MagicMock()
        recovery.recover.return_value = "result"
        with (
            patch.object(ops, "SQLiteTrackingStore", return_value=sqlite),
            patch.object(ops, "build_as400_recovery", return_value=recovery) as build,
        ):
            out = sync_recover(_config(), _secrets(), batch_id="b1", apply=False)
        assert out == "result"
        assert build.call_args.kwargs["sqlite_store"] is sqlite
        recovery.recover.assert_called_once_with(batch_id="b1", apply=False, on_progress=None)
        recovery.close.assert_called_once()
        sqlite.close.assert_called_once()

    def test_recover_forwards_on_progress_as_is(self) -> None:
        """144: el callback de progreso llega al servicio tal cual."""
        recovery, sqlite = MagicMock(), MagicMock()

        def on_progress(_: object) -> None:
            pass

        with (
            patch.object(ops, "SQLiteTrackingStore", return_value=sqlite),
            patch.object(ops, "build_as400_recovery", return_value=recovery),
        ):
            sync_recover(_config(), _secrets(), batch_id=None, apply=True, on_progress=on_progress)
        assert recovery.recover.call_args.kwargs["on_progress"] is on_progress

    def test_recover_closes_sqlite_when_build_fails(self) -> None:
        sqlite = MagicMock()
        with (
            patch.object(ops, "SQLiteTrackingStore", return_value=sqlite),
            patch.object(ops, "build_as400_recovery", side_effect=RuntimeError("boom")),
            pytest.raises(RuntimeError),
        ):
            sync_recover(_config(), _secrets(), batch_id=None, apply=False)
        sqlite.close.assert_called_once()
