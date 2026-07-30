from __future__ import annotations

from pathlib import Path

import httpx
import respx
from typer.testing import CliRunner

from immich_export.cli import app

from .fake_immich import BASE, FakeImmich

runner = CliRunner()


def _declared_options() -> set[str]:
    """Every `--option` the CLI actually declares.

    Read from the command tree rather than from `--help`, because the rendered
    help is Rich's output: it wraps, colours and boxes according to the terminal
    it thinks it has. Asserting against it made this test a function of the
    runner's width — it passed at every width locally and failed on CI, where the
    option names were not present in the rendered text at all.

    The contract being checked is "the README documents the flags that exist",
    and the declarations are what "exist" means.
    """
    import typer.main

    names: set[str] = set()

    def walk(command: object) -> None:
        for parameter in getattr(command, "params", []):
            for opt in getattr(parameter, "opts", []) or []:
                if opt.startswith("--"):
                    names.add(opt)
        for sub in getattr(command, "commands", {}).values():
            walk(sub)

    walk(typer.main.get_command(app))
    return names


def test_documented_commands_flags_and_environment_aliases_match_cli() -> None:
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    declared = _declared_options()

    for option in (
        "--out",
        "--mode",
        "--layout",
        "--album-view",
        "--people-view",
        "--manifest-batch-size",
        "--manifest-flush-interval",
        "--history-max-records",
        "--history-max-bytes",
        "--log-file",
    ):
        assert option in declared
        assert option in readme
    assert "immich-export --out ~/immich-export" in readme
    assert "--no-symlinks" not in readme


def test_manifest_batch_environment_alias_is_applied(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["--server", BASE, "--api-key", "key", "--out", str(tmp_path)],
        env={"IMMICH_EXPORT_MANIFEST_BATCH_SIZE": "0"},
    )

    assert result.exit_code == 2
    assert "--manifest-batch-size must be at least 1" in result.output


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


def test_verbose_diagnostics_coexist_with_phase_progress(
    respx_mock: respx.MockRouter, tmp_path: Path
) -> None:
    FakeImmich().install(respx_mock)
    result = runner.invoke(
        app,
        ["--server", BASE, "--api-key", "k", "--out", str(tmp_path), "--verbose"],
    )

    assert result.exit_code == 0
    assert "membership:" in result.output
    assert "completion:" in result.output
    assert "DEBUG" in result.output


def test_successful_run_prints_summary(respx_mock: respx.MockRouter, tmp_path: Path) -> None:
    from .fake_immich import standard_library

    standard_library().install(respx_mock)
    result = runner.invoke(app, ["--server", BASE, "--api-key", "k", "--out", str(tmp_path)])
    assert result.exit_code == 0
    assert "5 exported" in result.output
