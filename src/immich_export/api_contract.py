"""The exact slice of the Immich API this tool depends on.

`tests/test_contract.py` checks every entry here against the vendored OpenAPI
spec (`tests/data/immich-openapi.pruned.json`). To check compatibility with a
new Immich release: `uv run python scripts/refresh_api_spec.py` and re-run the
tests — a removed endpoint or field fails loudly instead of breaking at runtime.
"""

from __future__ import annotations

ENDPOINTS: dict[str, frozenset[str]] = {
    "/albums": frozenset({"get"}),
    "/assets/{id}/original": frozenset({"get"}),
    "/people": frozenset({"get"}),
    "/search/metadata": frozenset({"post"}),
    "/server/about": frozenset({"get"}),
    "/server/ping": frozenset({"get"}),
    "/tags": frozenset({"get"}),
}

# Response fields read, per OpenAPI schema (camelCase, as on the wire).
RESPONSE_FIELDS: dict[str, frozenset[str]] = {
    "AlbumResponseDto": frozenset({"id", "albumName", "assetCount", "description"}),
    "AssetResponseDto": frozenset(
        {
            "id",
            "originalFileName",
            "originalPath",
            "checksum",
            "fileCreatedAt",
            "localDateTime",
            "type",
            "isFavorite",
            "exifInfo",
            "people",
            "tags",
        }
    ),
    "ExifResponseDto": frozenset(
        {
            "dateTimeOriginal",
            "description",
            "latitude",
            "longitude",
            "city",
            "state",
            "country",
            "make",
            "model",
        }
    ),
    "PersonResponseDto": frozenset({"id", "name", "isHidden"}),
    "PeopleResponseDto": frozenset({"people", "hasNextPage", "total"}),
    "SearchAssetResponseDto": frozenset({"items", "nextPage"}),
    "ServerAboutResponseDto": frozenset({"version"}),
    "TagResponseDto": frozenset({"id", "name", "value"}),
}

# Request fields sent in the POST /search/metadata body.
SEARCH_REQUEST_FIELDS: frozenset[str] = frozenset(
    {
        "page",
        "size",
        "order",
        "withExif",
        "withPeople",
        "visibility",
        "takenAfter",
        "albumIds",
        "tagIds",
    }
)
