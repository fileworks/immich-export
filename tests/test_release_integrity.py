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
    require_new_version("0.0.5", "v0.0.5")
    with pytest.raises(ReleaseIntegrityError, match="newer"):
        require_new_version("0.0.4", "v0.0.4")
    with pytest.raises(ReleaseIntegrityError, match="does not identify"):
        require_new_version("0.0.5", "v0.0.6")


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


def test_every_source_version_location_is_inspected(tmp_path: Path) -> None:
    # uv.lock is one of them. It was not, and that is exactly how 0.1.0 reached
    # PyPI with the lock still saying 0.0.4: nothing compared them, so nothing
    # noticed until the Homebrew bump refused the mismatch.
    (tmp_path / "src/immich_export").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "immich-export"\nversion = "0.0.4"\n'
    )
    (tmp_path / "src/immich_export/__init__.py").write_text('__version__ = "0.0.4"\n')
    (tmp_path / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "immich-export"\n'
        'version = "0.0.4"\nsource = { editable = "." }\n'
    )

    assert source_versions(tmp_path) == {
        "pyproject.toml": "0.0.4",
        "__version__": "0.0.4",
        "uv.lock": "0.0.4",
    }


def test_a_lagging_lock_version_is_visible_as_disagreement(tmp_path: Path) -> None:
    # The shape of the real failure: pyproject moved, the lock did not.
    (tmp_path / "src/immich_export").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "immich-export"\nversion = "0.1.0"\n'
    )
    (tmp_path / "src/immich_export/__init__.py").write_text('__version__ = "0.1.0"\n')
    (tmp_path / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "immich-export"\n'
        'version = "0.0.4"\nsource = { editable = "." }\n'
    )

    assert len(set(source_versions(tmp_path).values())) != 1


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
    assert "no_operation_mode: true" in workflow
    assert "push: false" in workflow
    assert "vcs_release: false" in workflow


def test_release_channels_use_explicit_protected_environments() -> None:
    workflow = Path(".github/workflows/release.yml").read_text()

    assert "environment: github-release" in workflow
    assert "environment: pypi" in workflow
    assert "environment: homebrew" in workflow
    assert "name: python-distributions" in workflow
    assert "actions/upload-artifact@v7" in workflow
    assert workflow.count("actions/download-artifact@v8") == 2
    assert workflow.index("environment: github-release") < workflow.index("environment: pypi")
    assert workflow.index("environment: pypi") < workflow.index("environment: homebrew")


def test_integrity_change_preserves_reviewed_action_generations() -> None:
    release = Path(".github/workflows/release.yml").read_text()
    ci = Path(".github/workflows/ci.yml").read_text()
    assert "actions/checkout@v7" in release
    assert "python-semantic-release/python-semantic-release@v10" in release
    assert "no_operation_mode: true" in release
    assert "root_options:" not in release
    assert "GH_REPO: ${{ github.repository }}" in release
    assert "actions/checkout@v7" in ci
    assert "astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9" in ci


def test_release_docs_are_bounded_and_preserve_v003_history() -> None:
    readme = Path("README.md").read_text()
    changelog = Path("CHANGELOG.md").read_text()
    assert "manifest-current.jsonl" in readme
    assert "--stale-assets" in readme
    # Was `exit code \`5\``, which pinned a code the tool cannot return: ExitCode
    # runs 0, 1, 2, 3, 4, 130 and an asset-integrity failure raises PARTIAL. The
    # assertion outlived the renumbering onto the shared vocabulary and kept the
    # wrong number in the README by requiring it.
    assert "exit code `1` (`PARTIAL`)" in readme
    assert "not a replacement for independent" in readme
    assert "## v0.0.3 (2026-07-13)" in changelog
    assert "Not yet published" not in readme


def test_lock_identities_are_sources_not_distribution_filenames() -> None:
    # Adding uv.lock to source_versions without adding it here made verify treat
    # it as a distribution filename, and the 0.1.1 release failed with
    # "Distribution filenames disagree". Both the working and tagged lock keys
    # have to be recognised as source identities.
    script = Path("scripts/release_integrity.py").read_text(encoding="utf-8")
    source_names = script.split("source_names = {", 1)[1].split("}", 1)[0]

    assert '"uv.lock"' in source_names
    assert '"tag:uv.lock"' in source_names


def test_the_tagged_lock_version_is_inspected() -> None:
    # The working tree's lock agreeing is not enough: what shipped is the tag.
    script = Path("scripts/release_integrity.py").read_text(encoding="utf-8")

    assert 'f"{tag}:uv.lock"' in script
    assert '"tag:uv.lock": lock_version(lock)' in script
