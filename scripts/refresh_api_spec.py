"""Refresh the vendored, pruned Immich OpenAPI spec used by the contract tests.

Usage:
    uv run python scripts/refresh_api_spec.py            # fetch from GitHub main
    uv run python scripts/refresh_api_spec.py --ref v3.0.1
    uv run python scripts/refresh_api_spec.py --from-file /path/to/spec.json

The pruned file keeps only what the contract tests need: every path with its
methods, and every schema with its property names. Re-running this against a
new Immich release and then running `pytest tests/test_contract.py` tells you
whether this tool still matches the API.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import httpx

SPEC_URL = (
    "https://raw.githubusercontent.com/immich-app/immich/{ref}/open-api/immich-openapi-specs.json"
)
OUTPUT = Path(__file__).resolve().parent.parent / "tests" / "data" / "immich-openapi.pruned.json"
HTTP_METHODS = {"get", "put", "post", "delete", "patch", "head", "options"}


def prune(spec: dict[str, Any]) -> dict[str, Any]:
    paths = {
        path: sorted(method for method in operations if method in HTTP_METHODS)
        for path, operations in spec["paths"].items()
    }
    schemas = {
        name: sorted(schema.get("properties", {}))
        for name, schema in spec["components"]["schemas"].items()
    }
    return {
        "immich_version": spec["info"]["version"],
        "paths": paths,
        "schemas": schemas,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="main", help="Immich git ref to fetch the spec from.")
    parser.add_argument("--from-file", type=Path, help="Use a local spec file instead of fetching.")
    args = parser.parse_args()

    if args.from_file:
        spec = json.loads(args.from_file.read_text(encoding="utf-8"))
    else:
        response = httpx.get(SPEC_URL.format(ref=args.ref), follow_redirects=True, timeout=60)
        response.raise_for_status()
        spec = response.json()

    pruned = prune(spec)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(pruned, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT} (Immich {pruned['immich_version']})")


if __name__ == "__main__":
    main()
