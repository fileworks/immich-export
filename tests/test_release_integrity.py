"""Release gating and package identity checks."""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts.release_integrity import (
    ReleaseIntegrityError,
    artifact_versions,
    require_new_version,
    source_versions,
)


def _artifacts(dist: Path, version: str) -> None:
    dist.mkdir()
    wheel = dist / f"immich_export-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            f"immich_export-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.3\nName: immich-export\nVersion: {version}\n",
        )
    sdist = dist / f"immich_export-{version}.tar.gz"
    metadata = (f"Metadata-Version: 2.3\nName: immich-export\nVersion: {version}\n").encode()
    with tarfile.open(sdist, "w:gz") as archive:
        info = tarfile.TarInfo(f"immich_export-{version}/PKG-INFO")
        info.size = len(metadata)
        archive.addfile(info, io.BytesIO(metadata))


def test_new_release_must_be_newer_and_match_tag() -> None:
    require_new_version("0.0.4", "v0.0.4")
    with pytest.raises(ReleaseIntegrityError, match="newer"):
        require_new_version("0.0.3", "v0.0.3")
    with pytest.raises(ReleaseIntegrityError, match="does not identify"):
        require_new_version("0.0.4", "v0.0.5")


def test_artifacts_require_exactly_one_consistent_wheel_and_sdist(
    tmp_path: Path,
) -> None:
    dist = tmp_path / "dist"
    _artifacts(dist, "0.0.4")
    assert set(artifact_versions(dist).values()) == {"0.0.4"}

    (dist / "immich_export-0.0.3.tar.gz").write_bytes(b"stale")
    with pytest.raises(ReleaseIntegrityError, match="exactly one wheel and one sdist"):
        artifact_versions(dist)


def test_artifact_metadata_disagreement_is_detected(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    _artifacts(dist, "0.0.4")
    wheel = next(dist.glob("*.whl"))
    wheel.unlink()
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "immich_export-0.0.4.dist-info/METADATA",
            "Metadata-Version: 2.3\nName: immich-export\nVersion: 0.0.5\n",
        )
    assert artifact_versions(dist)[wheel.name] == "0.0.5"


def test_source_version_locations_are_both_inspected(tmp_path: Path) -> None:
    (tmp_path / "src/immich_export").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "immich-export"\nversion = "0.0.4"\n'
    )
    (tmp_path / "src/immich_export/__init__.py").write_text('__version__ = "0.0.4"\n')
    assert source_versions(tmp_path) == {
        "pyproject.toml": "0.0.4",
        "__version__": "0.0.4",
    }


def test_release_verification_checks_tagged_sources_and_clean_tree() -> None:
    script = Path("scripts/release_integrity.py").read_text()
    assert 'f"{tag}:pyproject.toml"' in script
    assert 'f"{tag}:src/immich_export/__init__.py"' in script
    assert '"status", "--porcelain", "--untracked-files=no"' in script


def test_release_workflow_is_success_and_exact_sha_gated() -> None:
    workflow = Path(".github/workflows/release.yml").read_text()
    assert "workflow_run:" in workflow
    assert "conclusion == 'success'" in workflow
    assert "workflow_run.event == 'push'" in workflow
    assert "workflow_run.head_sha" in workflow
    assert 'test "$(git rev-parse origin/main)" = "$TESTED_SHA"' in workflow
    assert 'test "$(git rev-parse "$RELEASE_COMMIT^")" = "$TESTED_SHA"' in workflow
    assert workflow.index("mkdir -p dist") < workflow.index(
        "Build and stage semantic release locally"
    )


def test_release_verifies_before_any_remote_publication() -> None:
    workflow = Path(".github/workflows/release.yml").read_text()
    preflight = workflow.index("release_integrity.py preflight")
    build = workflow.index("Build and stage semantic release locally")
    verify = workflow.index("release_integrity.py verify")
    push = workflow.index("git push --atomic")
    github_release = workflow.index("gh release create")
    pypi = workflow.index("pypa/gh-action-pypi-publish@release/v1")
    brew = workflow.index("gh workflow run bump.yml")
    assert preflight < build < verify < push < github_release < pypi < brew
    assert "root_options: --noop" in workflow
    assert "push: false" in workflow
    assert "vcs_release: false" in workflow


def test_integrity_change_preserves_reviewed_action_generations() -> None:
    release = Path(".github/workflows/release.yml").read_text()
    ci = Path(".github/workflows/ci.yml").read_text()
    assert "actions/checkout@v7" in release
    assert "python-semantic-release/python-semantic-release@v9" in release
    assert "actions/checkout@v7" in ci
    assert "astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9" in ci
    assert "@v10" not in release


def test_release_docs_are_bounded_and_preserve_v003_history() -> None:
    readme = Path("README.md").read_text()
    changelog = Path("CHANGELOG.md").read_text()
    assert "manifest-current.jsonl" in readme
    assert "--stale-assets" in readme
    assert "exit code `5`" in readme
    assert "not a replacement for independent" in readme
    assert "## v0.0.3 (2026-07-13)" in changelog
    assert "Not yet published" not in readme
