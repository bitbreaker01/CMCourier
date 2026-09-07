"""128: operaciones puras del sync SQLite ↔ NIARVILOG (sin click)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import cmcourier.cli.sync_ops as ops
from cmcourier.cli.sync_ops import SyncOpError, sync_resolve, sync_unavailable_reason
from cmcourier.config.loader import Secrets

pytestmark = pytest.mark.unit


def _config(*, enabled: bool = True, connection: str | None = "DSN=x") -> MagicMock:
    cfg = MagicMock()
    cfg.tracking.as400_sync.enabled = enabled
    cfg.tracking.as400_sync.connection = connection
    return cfg


def _secrets(user: str = "u", pwd: str = "p") -> Secrets:
    return Secrets(cmis_username="c", cmis_password="c", as400_username=user, as400_password=pwd)


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
