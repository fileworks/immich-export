"""A respx-backed fake Immich v3 server for integration tests."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

import httpx
import respx

BASE = "https://immich.test"


def checksum_of(content: bytes) -> str:
    return base64.b64encode(hashlib.sha1(content).digest()).decode()


def make_asset(
    asset_id: str,
    file_name: str,
    content: bytes,
    taken_at: str,
    *,
    asset_type: str = "IMAGE",
    favorite: bool = False,
    people: list[dict[str, str]] | None = None,
    tags: list[dict[str, str]] | None = None,
    description: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    visibility: str = "timeline",
) -> dict[str, Any]:
    return {
        "id": asset_id,
        "originalFileName": file_name,
        "originalPath": f"upload/library/admin/{taken_at[:4]}/{taken_at[5:7]}/{file_name}",
        "checksum": checksum_of(content),
        "fileCreatedAt": taken_at,
        "localDateTime": taken_at,
        "type": asset_type,
        "isFavorite": favorite,
        "exifInfo": {
            "dateTimeOriginal": taken_at,
            "description": description,
            "latitude": latitude,
            "longitude": longitude,
        },
        "people": people or [],
        "tags": tags or [],
        "visibility": visibility,
    }


class FakeImmich:
    """Holds fixture data and wires it into a respx router."""

    def __init__(self) -> None:
        self.version = "3.0.1"
        self.assets: list[dict[str, Any]] = []
        self.contents: dict[str, bytes] = {}
        self.albums: dict[str, dict[str, Any]] = {}
        self.album_members: dict[str, list[str]] = {}
        self.people: list[dict[str, Any]] = []
        self.tags: list[dict[str, Any]] = []
        self.tag_members: dict[str, list[str]] = {}
        self.page_size_override: int | None = None
        self.download_calls = 0

    def add_asset(self, asset: dict[str, Any], content: bytes) -> None:
        self.assets.append(asset)
        self.contents[asset["id"]] = content

    def add_album(self, album_id: str, name: str, asset_ids: list[str]) -> None:
        self.albums[album_id] = {
            "id": album_id,
            "albumName": name,
            "assetCount": len(asset_ids),
            "description": "",
        }
        self.album_members[album_id] = asset_ids

    def add_tag(self, tag_id: str, name: str, value: str, asset_ids: list[str]) -> None:
        self.tags.append({"id": tag_id, "name": name, "value": value})
        self.tag_members[tag_id] = asset_ids

    def _search(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        matching = self.assets
        if "albumIds" in body:
            wanted = {a for album in body["albumIds"] for a in self.album_members.get(album, [])}
            matching = [a for a in matching if a["id"] in wanted]
        if "tagIds" in body:
            wanted = {a for tag in body["tagIds"] for a in self.tag_members.get(tag, [])}
            matching = [a for a in matching if a["id"] in wanted]
        if "visibility" in body:
            matching = [a for a in matching if a["visibility"] == body["visibility"]]
        if "takenAfter" in body:
            matching = [a for a in matching if a["fileCreatedAt"] >= body["takenAfter"]]

        page = int(body.get("page", 1))
        size = self.page_size_override or int(body.get("size", 1000))
        start = (page - 1) * size
        items = matching[start : start + size]
        next_page = str(page + 1) if start + size < len(matching) else None
        return httpx.Response(
            200,
            json={
                "albums": {"items": [], "total": 0, "count": 0, "facets": [], "nextPage": None},
                "assets": {
                    "items": items,
                    "total": len(matching),
                    "count": len(items),
                    "facets": [],
                    "nextPage": next_page,
                },
            },
        )

    def _download(self, request: httpx.Request, asset_id: str) -> httpx.Response:
        del request
        self.download_calls += 1
        if asset_id not in self.contents:
            return httpx.Response(404)
        return httpx.Response(200, content=self.contents[asset_id])

    def install(self, router: respx.MockRouter) -> None:
        router.get(f"{BASE}/api/server/ping").respond(json={"res": "pong"})
        router.get(f"{BASE}/api/server/about").respond(json={"version": self.version})
        router.get(f"{BASE}/api/albums").mock(
            side_effect=lambda _: httpx.Response(200, json=list(self.albums.values()))
        )
        router.get(f"{BASE}/api/tags").mock(
            side_effect=lambda _: httpx.Response(200, json=self.tags)
        )
        router.get(url__regex=rf"{BASE}/api/people.*").mock(
            side_effect=lambda _: httpx.Response(
                200,
                json={
                    "people": self.people,
                    "total": len(self.people),
                    "hasNextPage": False,
                    "hidden": 0,
                },
            )
        )
        router.post(f"{BASE}/api/search/metadata").mock(side_effect=self._search)
        router.get(url__regex=rf"{BASE}/api/assets/(?P<asset_id>[^/]+)/original").mock(
            side_effect=lambda request, asset_id: self._download(request, asset_id)
        )


def standard_library() -> FakeImmich:
    """~5 assets across 2 albums + 1 named person + tags, per the spec's fixture."""
    fake = FakeImmich()
    anna = {"id": "p-anna", "name": "Anna"}
    fake.people = [{**anna, "isHidden": False}]
    tag_japan = {"id": "t-japan", "name": "japan", "value": "travel/japan"}

    fake.add_asset(
        make_asset(
            "a1",
            "IMG_0001.jpg",
            b"jpeg-bytes-one",
            "2019-04-12T10:00:00.000Z",
            people=[anna],
            tags=[tag_japan],
            description="Tokyo tower",
            latitude=35.6586,
            longitude=139.7454,
        ),
        b"jpeg-bytes-one",
    )
    fake.add_asset(
        make_asset("a2", "IMG_0002.jpg", b"jpeg-bytes-two", "2019-04-13T10:00:00.000Z"),
        b"jpeg-bytes-two",
    )
    fake.add_asset(
        make_asset(
            "a3",
            "IMG_0003.heic",
            b"heic-bytes",
            "2023-01-05T09:30:00.000Z",
            people=[anna],
            favorite=True,
        ),
        b"heic-bytes",
    )
    fake.add_asset(
        make_asset(
            "a4", "VID_0004.mp4", b"mp4-bytes", "2024-03-15T20:00:00.000Z", asset_type="VIDEO"
        ),
        b"mp4-bytes",
    )
    fake.add_asset(
        make_asset("a5", "IMG_0005.jpg", b"jpeg-bytes-five", "2024-03-16T08:00:00.000Z"),
        b"jpeg-bytes-five",
    )

    fake.add_album("al-japan", "Japan 2019", ["a1", "a2"])
    fake.add_album("al-family", "Family", ["a3"])
    fake.add_tag("t-japan", "japan", "travel/japan", ["a1"])
    return fake
