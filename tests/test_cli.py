from __future__ import annotations

from pathlib import Path

import httpx
import respx
from typer.testing import CliRunner

from immich_export.cli import app

from .fake_immich import BASE, FakeImmich

runner = CliRunner()


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.startswith("immich-export ")


def test_bad_server_url_exits_2(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--server", "not-a-url", "--api-key", "k", "--out", str(tmp_path)])
    assert result.exit_code == 2
    assert "http" in result.output


def test_auth_failure_exits_2(respx_mock: respx.MockRouter, tmp_path: Path) -> None:
    respx_mock.get(f"{BASE}/api/server/ping").respond(json={"res": "pong"})
    respx_mock.get(f"{BASE}/api/server/about").respond(401)
    result = runner.invoke(app, ["--server", BASE, "--api-key", "bad", "--out", str(tmp_path)])
    assert result.exit_code == 2
    assert "Authentication failed" in result.output
    assert "Traceback" not in result.output


def test_unreachable_exits_3(respx_mock: respx.MockRouter, tmp_path: Path) -> None:
    respx_mock.get(f"{BASE}/api/server/ping").mock(side_effect=httpx.ConnectError("refused"))
    result = runner.invoke(app, ["--server", BASE, "--api-key", "k", "--out", str(tmp_path)])
    assert result.exit_code == 3
    assert "Cannot reach Immich" in result.output
    assert "Traceback" not in result.output


def test_empty_library_exits_0(respx_mock: respx.MockRouter, tmp_path: Path) -> None:
    FakeImmich().install(respx_mock)
    result = runner.invoke(app, ["--server", BASE, "--api-key", "k", "--out", str(tmp_path)])
    assert result.exit_code == 0
    assert "empty" in result.output


def test_successful_run_prints_summary(respx_mock: respx.MockRouter, tmp_path: Path) -> None:
    from .fake_immich import standard_library

    standard_library().install(respx_mock)
    result = runner.invoke(app, ["--server", BASE, "--api-key", "k", "--out", str(tmp_path)])
    assert result.exit_code == 0
    assert "5 exported" in result.output
