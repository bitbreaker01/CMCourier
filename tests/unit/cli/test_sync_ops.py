"""128: operaciones puras del sync SQLite ↔ NIARVILOG (sin click)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import cmcourier.cli.sync_ops as ops
from cmcourier.cli.sync_ops import (
    SyncOpError,
    sync_recover,
    sync_resolve,
    sync_status,
    sync_unavailable_reason,
)
from cmcourier.config.loader import Credential, Secrets
from cmcourier.config.schema import As400ConnectionConfig, ConnectionRef

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
    def test_status_uses_only_as400_and_closes_it(self) -> None:
        as400 = MagicMock()
        as400.cleanup_stale_in_progress.return_value = 3
        with (
            patch.object(ops, "build_as400_store", return_value=as400),
            patch.object(ops, "SQLiteTrackingStore") as sqlite_cls,
        ):
            result = sync_status(_config(), _secrets())
        assert result.stale_cleaned == 3
        sqlite_cls.assert_not_called()
        as400.close.assert_called_once()

    def test_status_unavailable_raises(self) -> None:
        with pytest.raises(SyncOpError, match="as400_sync"):
            sync_status(_config(enabled=False), _secrets())


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
        recovery.recover.assert_called_once_with(batch_id="b1", apply=False)
        recovery.close.assert_called_once()
        sqlite.close.assert_called_once()

    def test_recover_closes_sqlite_when_build_fails(self) -> None:
        sqlite = MagicMock()
        with (
            patch.object(ops, "SQLiteTrackingStore", return_value=sqlite),
            patch.object(ops, "build_as400_recovery", side_effect=RuntimeError("boom")),
            pytest.raises(RuntimeError),
        ):
            sync_recover(_config(), _secrets(), batch_id=None, apply=False)
        sqlite.close.assert_called_once()
