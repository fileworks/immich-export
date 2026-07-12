from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from immich_export.errors import ConfigError
from immich_export.layout import (
    compute_relative_path,
    disambiguate,
    sanitize_component,
    validate_layout,
)
from immich_export.models import Asset

from .fake_immich import make_asset


def _asset(**overrides: object) -> Asset:
    raw = make_asset("a1", "IMG_0001.jpg", b"x", "2024-03-15T10:00:00.000Z")
    raw.update(overrides)
    return Asset.model_validate(raw)


class TestValidateLayout:
    def test_accepts_known_tokens(self) -> None:
        validate_layout("{year}/{month}/{day}/{album}/{type}")

    def test_rejects_unknown_token(self) -> None:
        with pytest.raises(ConfigError, match="Unknown layout token"):
            validate_layout("{year}/{albumm}")


class TestSanitizeComponent:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Japan 2019", "Japan 2019"),
            ("a/b\\c:d", "a_b_c_d"),
            ("trailing. ", "trailing"),
            ("  spaced   out  ", "spaced out"),
            ("", "_"),
            ("...", "_"),
        ],
    )
    def test_sanitize(self, raw: str, expected: str) -> None:
        assert sanitize_component(raw) == expected


class TestComputeRelativePath:
    def test_default_layout(self) -> None:
        path = compute_relative_path(_asset(), "{year}/{month}", None)
        assert path == PurePosixPath("2024/03/IMG_0001.jpg")

    def test_album_layout_with_album(self) -> None:
        path = compute_relative_path(_asset(), "{year}/{album}", "Japan: 2019")
        assert path == PurePosixPath("2024/Japan_ 2019/IMG_0001.jpg")

    def test_album_layout_falls_back_to_unsorted(self) -> None:
        path = compute_relative_path(_asset(), "{year}/{album}", None)
        assert path == PurePosixPath("2024/Unsorted/IMG_0001.jpg")

    def test_type_token_for_video(self) -> None:
        asset = _asset(type="VIDEO", originalFileName="VID.mp4")
        assert compute_relative_path(asset, "{type}/{year}", None) == PurePosixPath(
            "videos/2024/VID.mp4"
        )

    def test_exif_date_wins_over_file_date(self) -> None:
        asset = _asset(
            exifInfo={"dateTimeOriginal": "2020-01-02T00:00:00.000Z"},
        )
        assert compute_relative_path(asset, "{year}/{month}", None) == PurePosixPath(
            "2020/01/IMG_0001.jpg"
        )


def test_disambiguate_appends_id_fragment() -> None:
    result = disambiguate(PurePosixPath("2024/03/IMG.jpg"), "deadbeef-1234")
    assert result == PurePosixPath("2024/03/IMG-deadbeef.jpg")
