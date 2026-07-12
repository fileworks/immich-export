"""Typed views of the Immich API responses this tool reads.

Field names mirror the Immich OpenAPI schemas (camelCase on the wire, snake_case
here). `tests/test_contract.py` asserts every field used below still exists in
the vendored Immich OpenAPI spec, so an Immich upgrade surfaces as a failing
test instead of a silent runtime break.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class _ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="ignore", frozen=True
    )


class AssetType(StrEnum):
    IMAGE = "IMAGE"
    VIDEO = "VIDEO"
    AUDIO = "AUDIO"
    OTHER = "OTHER"


class ExifInfo(_ApiModel):
    date_time_original: datetime | None = None
    description: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    make: str | None = None
    model: str | None = None


class PersonRef(_ApiModel):
    id: str
    name: str


class TagRef(_ApiModel):
    id: str
    name: str
    value: str


class Asset(_ApiModel):
    id: str
    original_file_name: str
    original_path: str
    checksum: str
    """Base64-encoded SHA-1 of the original file, as reported by Immich."""
    file_created_at: datetime
    local_date_time: datetime
    type: AssetType
    is_favorite: bool = False
    exif_info: ExifInfo | None = None
    people: list[PersonRef] = []
    tags: list[TagRef] = []

    @property
    def taken_at(self) -> datetime:
        """Best available capture date: EXIF original > local date > file date."""
        if self.exif_info is not None and self.exif_info.date_time_original is not None:
            return self.exif_info.date_time_original
        return self.local_date_time or self.file_created_at

    @property
    def description(self) -> str | None:
        if self.exif_info is not None and self.exif_info.description:
            return self.exif_info.description
        return None


class Album(_ApiModel):
    id: str
    album_name: str
    asset_count: int = 0
    description: str = ""


class Person(_ApiModel):
    id: str
    name: str
    is_hidden: bool = False


class Tag(_ApiModel):
    id: str
    name: str
    value: str
    """Full hierarchical tag path, e.g. ``travel/japan``."""


class SearchAssetPage(_ApiModel):
    items: list[Asset]
    next_page: str | None = None


class ServerAbout(_ApiModel):
    version: str
