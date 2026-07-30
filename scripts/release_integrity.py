#!/usr/bin/env python3
"""Fail-closed checks around semantic-release artifact publication."""

from __future__ import annotations

import argparse
import email
import json
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.error
import urllib.request
import venv
import zipfile
from dataclasses import dataclass
from pathlib import Path

PROJECT = "immich-export"
DISTRIBUTION = "immich_export"
RELEASED_FLOOR = "0.0.4"
SEMVER = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?P<suffix>-(?:0|[1-9]\d*|[0-9A-Za-z-][0-9A-Za-z.-]*))?$"
)


class ReleaseIntegrityError(RuntimeError):
    """A release identity or uniqueness invariant failed."""


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int
    stable: bool
    suffix: str

    @classmethod
    def parse(cls, raw: str) -> Version:
        match = SEMVER.fullmatch(raw)
        if match is None:
            raise ReleaseIntegrityError(f"Invalid semantic version: {raw!r}")
        suffix = match.group("suffix") or ""
        return cls(
            int(match.group("major")),
            int(match.group("minor")),
            int(match.group("patch")),
            not suffix,
            suffix,
        )


def require_new_version(version: str, tag: str) -> None:
    selected = Version.parse(version)
    if selected <= Version.parse(RELEASED_FLOOR):
        raise ReleaseIntegrityError(
            f"Selected version {version} must be newer than released {RELEASED_FLOOR}."
        )
    if tag != f"v{version}":
        raise ReleaseIntegrityError(
            f"Semantic-release tag {tag!r} does not identify version {version}."
        )


def tag_exists(tag: str) -> bool:
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/tags/{tag}"],
        check=False,
    )
    if result.returncode not in (0, 1):
        raise ReleaseIntegrityError(f"Could not inspect existing Git tag {tag}.")
    return result.returncode == 0


def pypi_version_exists(
    version: str, *, base_url: str = f"https://pypi.org/pypi/{PROJECT}"
) -> bool:
    request = urllib.request.Request(
        f"{base_url}/{version}/json",
        headers={"Accept": "application/json", "User-Agent": "immich-export-release-check"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.status == 200:
                return True
            raise ReleaseIntegrityError(
                f"PyPI uniqueness check returned unexpected HTTP {response.status}."
            )
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise ReleaseIntegrityError(
            f"PyPI uniqueness check returned HTTP {exc.code}; refusing publication."
        ) from exc
    except urllib.error.URLError as exc:
        raise ReleaseIntegrityError(
            f"PyPI uniqueness check failed ({exc}); refusing publication."
        ) from exc


def preflight(version: str, tag: str) -> None:
    require_new_version(version, tag)
    if tag_exists(tag):
        raise ReleaseIntegrityError(f"Git tag {tag} already exists.")
    if pypi_version_exists(version):
        raise ReleaseIntegrityError(f"{PROJECT} {version} already exists on PyPI.")


def _metadata_version(payload: bytes, *, source: str) -> str:
    message = email.message_from_bytes(payload)
    value = message.get("Version")
    if not value:
        raise ReleaseIntegrityError(f"{source} metadata has no Version field.")
    return value


def artifact_versions(dist: Path) -> dict[str, str]:
    files = sorted(
        path for path in dist.iterdir() if path.is_file() and not path.name.startswith(".")
    )
    if len(files) != 2:
        names = ", ".join(path.name for path in files) or "<empty>"
        raise ReleaseIntegrityError(
            f"{dist} must contain exactly one wheel and one sdist; found: {names}."
        )
    wheel = [path for path in files if path.suffix == ".whl"]
    sdist = [path for path in files if path.name.endswith(".tar.gz")]
    if len(wheel) != 1 or len(sdist) != 1:
        raise ReleaseIntegrityError("Release staging must contain one wheel and one .tar.gz.")

    with zipfile.ZipFile(wheel[0]) as archive:
        wheel_metadata = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(wheel_metadata) != 1:
            raise ReleaseIntegrityError(f"{wheel[0].name} has ambiguous wheel metadata.")
        wheel_version = _metadata_version(archive.read(wheel_metadata[0]), source=wheel[0].name)

    with tarfile.open(sdist[0], "r:gz") as archive:
        sdist_metadata = [
            member
            for member in archive.getmembers()
            if member.name.count("/") == 1 and member.name.endswith("/PKG-INFO")
        ]
        if len(sdist_metadata) != 1:
            raise ReleaseIntegrityError(f"{sdist[0].name} has ambiguous sdist metadata.")
        extracted = archive.extractfile(sdist_metadata[0])
        if extracted is None:
            raise ReleaseIntegrityError(f"Cannot read metadata from {sdist[0].name}.")
        sdist_version = _metadata_version(extracted.read(), source=sdist[0].name)
    return {wheel[0].name: wheel_version, sdist[0].name: sdist_version}


def source_versions(root: Path) -> dict[str, str]:
    with (root / "pyproject.toml").open("rb") as file:
        pyproject_version = str(tomllib.load(file)["project"]["version"])
    init_text = (root / "src/immich_export/__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', init_text, re.MULTILINE)
    if match is None:
        raise ReleaseIntegrityError("Package __version__ could not be read.")
    return {"pyproject.toml": pyproject_version, "__version__": match.group(1)}


def tagged_source_versions(tag: str) -> dict[str, str]:
    pyproject = subprocess.run(
        ["git", "show", f"{tag}:pyproject.toml"],
        check=True,
        capture_output=True,
    ).stdout
    init_text = subprocess.run(
        ["git", "show", f"{tag}:src/immich_export/__init__.py"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    pyproject_version = str(tomllib.loads(pyproject.decode())["project"]["version"])
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', init_text, re.MULTILINE)
    if match is None:
        raise ReleaseIntegrityError(f"Package __version__ is missing from tag {tag}.")
    return {
        "tag:pyproject.toml": pyproject_version,
        "tag:__version__": match.group(1),
    }


def verify_git_release(tag: str, commit: str) -> None:
    result = subprocess.run(
        ["git", "rev-list", "-n", "1", tag],
        check=True,
        capture_output=True,
        text=True,
    )
    tagged_commit = result.stdout.strip()
    if tagged_commit != commit:
        raise ReleaseIntegrityError(
            f"Tag {tag} points to {tagged_commit}, not semantic-release commit {commit}."
        )
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status:
        raise ReleaseIntegrityError(
            "Semantic-release left tracked source changes outside its release commit."
        )


def verify_installed_cli(wheel: Path, version: str) -> None:
    with tempfile.TemporaryDirectory(prefix="immich-export-release-") as directory:
        environment = Path(directory)
        venv.EnvBuilder(with_pip=True).create(environment)
        scripts = environment / ("Scripts" if sys.platform == "win32" else "bin")
        python = scripts / ("python.exe" if sys.platform == "win32" else "python")
        executable = scripts / ("immich-export.exe" if sys.platform == "win32" else "immich-export")
        subprocess.run(
            [str(python), "-m", "pip", "install", "--disable-pip-version-check", str(wheel)],
            check=True,
        )
        result = subprocess.run(
            [str(executable), "--version"],
            check=True,
            capture_output=True,
            text=True,
        )
    expected = f"immich-export {version}"
    if result.stdout.strip() != expected:
        raise ReleaseIntegrityError(
            f"Installed CLI reported {result.stdout.strip()!r}, expected {expected!r}."
        )


def verify(
    *,
    root: Path,
    dist: Path,
    version: str,
    expected_version: str,
    tag: str,
    commit: str,
    check_install: bool,
) -> None:
    require_new_version(version, tag)
    if version != expected_version:
        raise ReleaseIntegrityError(
            f"Release changed from preflight {expected_version} to {version}."
        )
    identities = source_versions(root) | tagged_source_versions(tag) | artifact_versions(dist)
    disagreements = {source: value for source, value in identities.items() if value != version}
    if disagreements:
        raise ReleaseIntegrityError(
            f"Release version disagreement: {json.dumps(disagreements, sort_keys=True)}"
        )
    source_names = {
        "pyproject.toml",
        "__version__",
        "tag:pyproject.toml",
        "tag:__version__",
    }
    actual_names = {name for name in identities if name not in source_names}
    expected_names = {
        f"{DISTRIBUTION}-{version}.tar.gz",
        f"{DISTRIBUTION}-{version}-py3-none-any.whl",
    }
    if actual_names != expected_names:
        raise ReleaseIntegrityError(
            f"Distribution filenames disagree with {version}: {sorted(actual_names)}"
        )
    verify_git_release(tag, commit)
    if check_install:
        wheel = next(dist.glob("*.whl"))
        verify_installed_cli(wheel, version)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight_parser = subparsers.add_parser("preflight")
    preflight_parser.add_argument("--version", required=True)
    preflight_parser.add_argument("--tag", required=True)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--root", type=Path, default=Path.cwd())
    verify_parser.add_argument("--dist", type=Path, default=Path("dist"))
    verify_parser.add_argument("--version", required=True)
    verify_parser.add_argument("--expected-version", required=True)
    verify_parser.add_argument("--tag", required=True)
    verify_parser.add_argument("--commit", required=True)
    verify_parser.add_argument("--skip-install-check", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "preflight":
            preflight(args.version, args.tag)
        else:
            verify(
                root=args.root,
                dist=args.dist,
                version=args.version,
                expected_version=args.expected_version,
                tag=args.tag,
                commit=args.commit,
                check_install=not args.skip_install_check,
            )
    except (OSError, subprocess.SubprocessError, ReleaseIntegrityError) as exc:
        print(f"release integrity failure: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
