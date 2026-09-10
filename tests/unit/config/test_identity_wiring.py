"""147 REQ-002: ``IdentityConfigModel`` → ``IdentityConfig`` del dominio.

El wiring es la única traducción entre el schema Pydantic y las dataclasses
del servicio (Principio I): ni el resolver ni el orchestrator conocen
Pydantic.
"""

from __future__ import annotations

import pytest

from cmcourier.config.schema import IdentityConfigModel, IdentitySlotModel
from cmcourier.config.wiring import build_identity_config
from cmcourier.services.identity import IdentityConfig, IdentitySlotConfig

pytestmark = pytest.mark.unit


class TestBuildIdentityConfig:
    def test_bloque_vacio_da_los_tres_slots_en_none(self) -> None:
        assert build_identity_config(IdentityConfigModel()) == IdentityConfig()

    def test_traduce_los_tres_slots(self) -> None:
        model = IdentityConfigModel(
            shortname=IdentitySlotModel(field="BAC_Shortname"),
            cif=IdentitySlotModel(field="BAC_CIF", max_digits=9),
            system_id=IdentitySlotModel(field="BAC_Sistema", on_missing="warn"),
        )
        assert build_identity_config(model) == IdentityConfig(
            shortname=IdentitySlotConfig(field="BAC_Shortname"),
            cif=IdentitySlotConfig(field="BAC_CIF", max_digits=9),
            system_id=IdentitySlotConfig(field="BAC_Sistema", on_missing="warn"),
        )

    def test_conserva_el_default_value(self) -> None:
        model = IdentityConfigModel(
            cif=IdentitySlotModel(field="BAC_CIF", on_missing="default", default_value="000000000")
        )
        config = build_identity_config(model)
        assert config.cif is not None
        assert config.cif.on_missing == "default"
        assert config.cif.default_value == "000000000"

    def test_un_slot_declarado_deja_los_otros_en_none(self) -> None:
        model = IdentityConfigModel(cif=IdentitySlotModel(field="BAC_CIF"))
        config = build_identity_config(model)
        assert config.shortname is None
        assert config.system_id is None
        assert config.declared_fields() == ("BAC_CIF",)
