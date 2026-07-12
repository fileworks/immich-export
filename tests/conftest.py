from __future__ import annotations

from pathlib import Path

import pytest
import respx

from immich_export.config import ExportConfig

from .fake_immich import BASE, FakeImmich, standard_library


@pytest.fixture
def fake_immich(respx_mock: respx.MockRouter) -> FakeImmich:
    fake = standard_library()
    fake.install(respx_mock)
    return fake


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    return tmp_path / "export"


@pytest.fixture
def base_config(out_dir: Path) -> ExportConfig:
    return ExportConfig(server=BASE, api_key="test-key", out=out_dir)
