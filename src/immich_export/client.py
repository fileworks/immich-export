"""Thin, typed, read-only client for the Immich v3 REST API.

Endpoint paths and request/response fields are declared centrally (see
`api_contract.py`) and checked against the vendored OpenAPI spec in CI.
All network failures are translated into the user-facing error types in
`errors.py`; no httpx exception escapes this module.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import httpx

from .errors import AuthError, OutputError, ServerUnreachableError
from .models import Album, Asset, Person, SearchAssetPage, ServerAbout, Tag

PAGE_SIZE = 1000
"""Maximum page size accepted by /search/metadata (per the OpenAPI spec)."""

DEFAULT_VISIBILITIES: tuple[str, ...] = ("timeline", "archive")
"""Asset visibilities exported by default; 'hidden' and 'locked' are opt-in."""


def _format_taken_after(value: datetime) -> str:
    """Immich validates takenAfter against a strict ISO-8601-with-Z pattern."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class ImmichClient:
    """Async wrapper around the handful of Immich endpoints this tool needs."""

    def __init__(
        self,
        server: str,
        api_key: str,
        *,
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._server = server.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=f"{self._server}/api",
            headers={"x-api-key": api_key, "Accept": "application/json"},
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            raise ServerUnreachableError(
                f"Cannot reach Immich at {self._server}: {exc}. "
                "Is the server up and the URL correct?"
            ) from exc
        if response.status_code in (401, 403):
            raise AuthError(
                "Authentication failed — check your Immich API key (--api-key / $IMMICH_API_KEY)."
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ServerUnreachableError(
                f"Immich at {self._server} answered {response.status_code} for {path}."
            ) from exc
        return response

    async def check_connection(self) -> ServerAbout:
        """Verify the server is reachable and the API key is valid."""
        await self._request("GET", "/server/ping")
        about = await self._request("GET", "/server/about")
        return ServerAbout.model_validate(about.json())

    async def iter_assets(
        self,
        *,
        taken_after: datetime | None = None,
        visibilities: Sequence[str] = DEFAULT_VISIBILITIES,
    ) -> AsyncIterator[list[Asset]]:
        """Yield pages of assets with EXIF and people populated."""
        for visibility in visibilities:
            body: dict[str, Any] = {
                "size": PAGE_SIZE,
                "withExif": True,
                "withPeople": True,
                "order": "asc",
                "visibility": visibility,
            }
            if taken_after is not None:
                body["takenAfter"] = _format_taken_after(taken_after)
            async for page in self._paged_search(body):
                yield page.items

    async def search_asset_ids(
        self, *, album_id: str | None = None, tag_id: str | None = None
    ) -> list[str]:
        """Asset ids matching an album or tag filter (v3 has no GET membership endpoint)."""
        body: dict[str, Any] = {"size": PAGE_SIZE, "order": "asc"}
        if album_id is not None:
            body["albumIds"] = [album_id]
        if tag_id is not None:
            body["tagIds"] = [tag_id]
        ids: list[str] = []
        async for page in self._paged_search(body):
            ids.extend(asset.id for asset in page.items)
        return ids

    async def _paged_search(self, body: dict[str, Any]) -> AsyncIterator[SearchAssetPage]:
        page_token: str | None = "1"
        while page_token is not None:
            response = await self._request(
                "POST", "/search/metadata", json={**body, "page": int(page_token)}
            )
            page = SearchAssetPage.model_validate(response.json()["assets"])
            yield page
            page_token = page.next_page

    async def list_albums(self) -> list[Album]:
        response = await self._request("GET", "/albums")
        return [Album.model_validate(item) for item in response.json()]

    async def list_people(self) -> list[Person]:
        people: list[Person] = []
        page = 1
        while True:
            response = await self._request(
                "GET", "/people", params={"page": page, "withHidden": False}
            )
            payload = response.json()
            people.extend(Person.model_validate(item) for item in payload["people"])
            if not payload.get("hasNextPage"):
                return people
            page += 1

    async def list_tags(self) -> list[Tag]:
        response = await self._request("GET", "/tags")
        return [Tag.model_validate(item) for item in response.json()]

    async def download_original(self, asset_id: str, dest: Path) -> str:
        """Stream an original file to `dest`; returns the hex SHA-1 of the bytes written.

        Writes to a `.part` file first and renames on success, so an interrupted
        run never leaves a truncated file at the final path.
        """
        tmp = dest.with_name(dest.name + ".part")
        sha1 = hashlib.sha1()
        try:
            async with self._http.stream("GET", f"/assets/{asset_id}/original") as response:
                if response.status_code in (401, 403):
                    raise AuthError(
                        "Authentication failed — check your Immich API key "
                        "(--api-key / $IMMICH_API_KEY)."
                    )
                response.raise_for_status()
                with tmp.open("wb") as fh:
                    async for chunk in response.aiter_bytes():
                        sha1.update(chunk)
                        fh.write(chunk)
        except httpx.TransportError as exc:
            tmp.unlink(missing_ok=True)
            raise ServerUnreachableError(
                f"Cannot reach Immich at {self._server}: {exc}. "
                "Is the server up and the URL correct?"
            ) from exc
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise OutputError(f"Cannot write to {dest}: {exc}") from exc
        tmp.replace(dest)
        return sha1.hexdigest()
