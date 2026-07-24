from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_actions_and_setup_uv_use_reviewed_generations() -> None:
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "actions/checkout@v7" in ci
    assert "actions/checkout@v7" in release
    assert "astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9" in ci
    assert "v9.0.0" in ci
    assert "actions/checkout@v4" not in ci + release
    assert "astral-sh/setup-uv@v5" not in ci


def test_tap_dispatch_passes_release_values_as_quoted_data() -> None:
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    bump_block = release.split("- name: Bump Homebrew formula", maxsplit=1)[1]

    assert '-f "formula=immich-export"' in bump_block
    assert '-f "version=$RELEASE_VERSION"' in bump_block
    assert '-f "source_repository=$SOURCE_REPOSITORY"' in bump_block
    assert '-f "source_run=$SOURCE_RUN"' in bump_block
    run_block = bump_block.split("run: |", maxsplit=1)[1]
    assert "${{ steps.semrel.outputs.version }}" not in run_block
