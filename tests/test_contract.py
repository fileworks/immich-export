"""Assert the API slice we use still exists in the vendored Immich OpenAPI spec.

Refresh the spec with `uv run python scripts/refresh_api_spec.py` when a new
Immich version ships; failures here mean the tool needs adapting *before* it
breaks at runtime against a real server.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from immich_export.api_contract import ENDPOINTS, RESPONSE_FIELDS, SEARCH_REQUEST_FIELDS

SPEC_PATH = Path(__file__).parent / "data" / "immich-openapi.pruned.json"


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def test_endpoints_exist(spec: dict[str, Any]) -> None:
    for path, methods in ENDPOINTS.items():
        assert path in spec["paths"], f"endpoint {path} gone from Immich API"
        missing = methods - set(spec["paths"][path])
        assert not missing, f"{path} lost method(s) {sorted(missing)}"


def test_response_fields_exist(spec: dict[str, Any]) -> None:
    for schema, fields in RESPONSE_FIELDS.items():
        assert schema in spec["schemas"], f"schema {schema} gone from Immich API"
        missing = fields - set(spec["schemas"][schema])
        assert not missing, f"{schema} lost field(s) {sorted(missing)}"


def test_search_request_fields_exist(spec: dict[str, Any]) -> None:
    missing = SEARCH_REQUEST_FIELDS - set(spec["schemas"]["MetadataSearchDto"])
    assert not missing, f"MetadataSearchDto lost field(s) {sorted(missing)}"
