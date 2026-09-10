"""Tests unitarios para la jerarquía polimórfica de `Trigger` (046 Fase 1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cmcourier.domain.models import (
    ClientTrigger,
    LocalScanTrigger,
    RvabrepRowTrigger,
    Trigger,
    TriggerRecord,
    trigger_system_id,
)

pytestmark = pytest.mark.unit


class TestBackwardCompat046:
    """``TriggerRecord`` debe seguir apuntando a ``ClientTrigger`` para
    que cada import pre-046 (estrategia `csv`, CLI single-doc, adaptador
    de tracking, tests de integración) compile sin cambios."""

    def test_alias_is_identity(self) -> None:
        assert TriggerRecord is ClientTrigger

    def test_legacy_construction_still_works(self) -> None:
        # La forma pre-046: posicional o nombrada, los tres campos.
        t = TriggerRecord(shortname="ACME-001", cif="123456", system_id="PROD")
        assert isinstance(t, ClientTrigger)
        assert isinstance(t, Trigger)


class TestClientTrigger:
    def test_validation_rejects_empty_shortname(self) -> None:
        with pytest.raises(ValueError, match="shortname"):
            ClientTrigger(shortname="", cif="x", system_id="1")

    def test_validation_rejects_empty_system_id(self) -> None:
        with pytest.raises(ValueError, match="system_id"):
            ClientTrigger(shortname="A", cif="x", system_id="")

    def test_cif_may_be_none(self) -> None:
        # El `self-healing` de CIF necesita `cif=None` como estado de primera clase.
        t = ClientTrigger(shortname="A", cif=None, system_id="1")
        assert t.cif is None

    def test_audit_row_returns_literal_fields(self) -> None:
        t = ClientTrigger(shortname="A", cif="42", system_id="PROD")
        assert t.audit_row() == {"shortname": "A", "cif": "42", "system_id": "PROD"}

    def test_audit_row_preserves_none_cif(self) -> None:
        t = ClientTrigger(shortname="A", cif=None, system_id="PROD")
        assert t.audit_row() == {"shortname": "A", "cif": None, "system_id": "PROD"}


class TestRvabrepRowTrigger:
    def test_audit_row_projects_from_default_rvabrep_columns(self) -> None:
        """Los nombres de columna por defecto son el esquema físico de AS400."""
        row = {
            "ABABCD": "ACME-001",  # shortname
            "ABACCD": "987654",  # cif
            "ABAACD": "PROD",  # system_id
            "ABAJCD": "FILE001.PDF",  # file_name (no va en auditoría)
            "extra_col": "irrelevant",
        }
        t = RvabrepRowTrigger(row=row)
        assert t.audit_row() == {
            "shortname": "ACME-001",
            "cif": "987654",
            "system_id": "PROD",
        }

    def test_audit_row_uses_overridden_columns(self) -> None:
        """Las estrategias que leen CSVs con nombres de columna amigables
        pasan su propio mapa de columnas en la construcción."""
        row = {"shortname": "X", "index2": "42", "system_id": "PROD"}
        t = RvabrepRowTrigger(
            row=row,
            col_shortname="shortname",
            col_cif="index2",
            col_system_id="system_id",
        )
        assert t.audit_row() == {"shortname": "X", "cif": "42", "system_id": "PROD"}

    def test_audit_row_normalizes_blank_cif_to_none(self) -> None:
        """Las filas RVABREP suelen tener CIF de solo whitespace para
        clientes cuyo CIF es `self-healed` luego por S3. La fila de
        auditoría debe reportar None, no el whitespace crudo."""
        t = RvabrepRowTrigger(row={"ABABCD": "X", "ABACCD": "   ", "ABAACD": "1"})
        assert t.audit_row()["cif"] is None

    def test_audit_row_handles_missing_columns(self) -> None:
        # Una fila que ni siquiera tiene la clave de columna devuelve None
        # para los slots faltantes.
        t = RvabrepRowTrigger(row={"ABABCD": "X"})
        assert t.audit_row() == {"shortname": "X", "cif": None, "system_id": None}


class TestLocalScanTrigger:
    def test_audit_row_projects_from_default_columns(self) -> None:
        row = {"ABABCD": "PEDRO99", "ABACCD": "555111", "ABAACD": "3"}
        t = LocalScanTrigger(file_path=Path("/tmp/scan/foo.001"), row=row)
        assert t.audit_row() == {
            "shortname": "PEDRO99",
            "cif": "555111",
            "system_id": "3",
        }

    def test_audit_row_with_overridden_columns(self) -> None:
        row = {"shortname": "PEDRO99", "index2": "555111", "system_id": "3"}
        t = LocalScanTrigger(
            file_path=Path("/tmp/scan/foo.001"),
            row=row,
            col_shortname="shortname",
            col_cif="index2",
            col_system_id="system_id",
        )
        assert t.audit_row()["shortname"] == "PEDRO99"

    def test_file_path_preserved(self) -> None:
        p = Path("/tmp/scan/AAA.PDF")
        t = LocalScanTrigger(file_path=p, row={"ABABCD": "X", "ABAACD": "1"})
        assert t.file_path == p


class TestTriggerBaseIsAbstract:
    def test_cannot_instantiate_abstract_base(self) -> None:
        with pytest.raises(TypeError):
            Trigger()  # type: ignore[abstract]


class TestTriggerSystemId:
    """145 REQ-001: el sistema que S2 le pasa a ``get_mapping``."""

    def test_client_trigger_uses_its_attribute(self) -> None:
        t = ClientTrigger(shortname="ACME", cif="1", system_id="RVI2")
        assert trigger_system_id(t) == "RVI2"

    def test_row_trigger_reads_the_audit_projection(self) -> None:
        t = RvabrepRowTrigger(row={"ABABCD": "X", "ABAACD": "RVI2"})
        assert trigger_system_id(t) == "RVI2"

    def test_local_scan_trigger_reads_the_audit_projection(self) -> None:
        t = LocalScanTrigger(
            file_path=Path("/tmp/scan/foo.001"),
            row={"ABABCD": "X", "ABAACD": " RVI2 "},
        )
        assert trigger_system_id(t) == "RVI2"

    def test_missing_system_is_none(self) -> None:
        t = RvabrepRowTrigger(row={"ABABCD": "X"})
        assert trigger_system_id(t) is None

    def test_blank_system_is_none(self) -> None:
        t = RvabrepRowTrigger(row={"ABABCD": "X", "ABAACD": "   "})
        assert trigger_system_id(t) is None
