"""XMP sidecar writer.

Standard namespaces are used wherever one exists (dc:subject for tags,
Iptc4xmpExt:PersonInImage for people, dc:description, exif GPS,
photoshop:DateCreated, xmp:Rating for favorites) so mainstream tools
(digiKam, Lightroom, exiftool) can read the sidecars. Album membership and
Immich identifiers have no standard slot, so they live in a small custom
`immich:` namespace — still plain XML, still greppable.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from .errors import OutputError
from .manifest import AssetState, atomic_write_text
from .models import Asset

NS = {
    "x": "adobe:ns:meta/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dc": "http://purl.org/dc/elements/1.1/",
    "xmp": "http://ns.adobe.com/xap/1.0/",
    "exif": "http://ns.adobe.com/exif/1.0/",
    "photoshop": "http://ns.adobe.com/photoshop/1.0/",
    "Iptc4xmpExt": "http://iptc.org/std/Iptc4xmpExt/2008-02-29/",
    "immich": "https://immich.app/ns/1.0/",
}

for prefix, uri in NS.items():
    ET.register_namespace(prefix, uri)


def _q(prefix: str, tag: str) -> str:
    return f"{{{NS[prefix]}}}{tag}"


def format_gps(value: float, *, is_latitude: bool) -> str:
    """XMP exif GPS format: degrees,decimal-minutes + hemisphere (e.g. `47,26.4614N`)."""
    positive, negative = ("N", "S") if is_latitude else ("E", "W")
    ref = positive if value >= 0 else negative
    magnitude = abs(value)
    degrees = int(magnitude)
    minutes = (magnitude - degrees) * 60
    return f"{degrees},{minutes:.6f}{ref}"


def _bag(parent: ET.Element, qname: str, values: list[str], *, container: str = "Bag") -> None:
    if not values:
        return
    prop = ET.SubElement(parent, qname)
    bag = ET.SubElement(prop, _q("rdf", container))
    for value in values:
        ET.SubElement(bag, _q("rdf", "li")).text = value


def _state_from_asset(
    asset: Asset, albums: Iterable[str], extra_tags: Iterable[str] = ()
) -> AssetState:
    exif = asset.exif_info
    return AssetState(
        asset_id=asset.id,
        checksum=asset.checksum,
        path="",
        file_name=asset.original_file_name,
        original_path=asset.original_path,
        taken_at=asset.taken_at,
        type=str(asset.type),
        favorite=asset.is_favorite,
        description=asset.description,
        albums=sorted(set(albums)),
        people=sorted({person.name for person in asset.people if person.name}),
        tags=sorted({tag.value for tag in asset.tags} | set(extra_tags)),
        latitude=exif.latitude if exif else None,
        longitude=exif.longitude if exif else None,
        verified_at=datetime.now(UTC),
    )


def build_xmp(
    state_or_asset: AssetState | Asset,
    albums: Iterable[str] = (),
    *,
    extra_tags: Iterable[str] = (),
) -> str:
    """Build deterministic XMP from the canonical state.

    Accepting an API ``Asset`` is retained for the small public helper surface;
    the exporter itself always passes ``AssetState`` so indexed tag membership
    and the manifest can never diverge.
    """
    state = (
        state_or_asset
        if isinstance(state_or_asset, AssetState)
        else _state_from_asset(state_or_asset, albums, extra_tags)
    )
    root = ET.Element(_q("x", "xmpmeta"))
    rdf = ET.SubElement(root, _q("rdf", "RDF"))
    desc = ET.SubElement(rdf, _q("rdf", "Description"), {_q("rdf", "about"): ""})

    _bag(desc, _q("dc", "subject"), state.tags)
    _bag(desc, _q("Iptc4xmpExt", "PersonInImage"), state.people)
    _bag(desc, _q("immich", "Albums"), state.albums)

    if state.description:
        prop = ET.SubElement(desc, _q("dc", "description"))
        alt = ET.SubElement(prop, _q("rdf", "Alt"))
        li = ET.SubElement(alt, _q("rdf", "li"))
        li.set("{http://www.w3.org/XML/1998/namespace}lang", "x-default")
        li.text = state.description

    ET.SubElement(desc, _q("photoshop", "DateCreated")).text = state.taken_at.isoformat()
    if state.favorite:
        ET.SubElement(desc, _q("xmp", "Rating")).text = "5"

    if state.latitude is not None and state.longitude is not None:
        ET.SubElement(desc, _q("exif", "GPSLatitude")).text = format_gps(
            state.latitude, is_latitude=True
        )
        ET.SubElement(desc, _q("exif", "GPSLongitude")).text = format_gps(
            state.longitude, is_latitude=False
        )

    ET.SubElement(desc, _q("immich", "AssetId")).text = state.asset_id
    ET.SubElement(desc, _q("immich", "Checksum")).text = state.checksum

    ET.indent(root)
    body = ET.tostring(root, encoding="unicode")
    return f'<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n{body}\n<?xpacket end="w"?>\n'


def sidecar_matches(state: AssetState, media_path: Path) -> bool:
    """Return whether the existing XMP is exactly the canonical serialization."""
    sidecar_path = media_path.with_name(media_path.name + ".xmp")
    try:
        return sidecar_path.read_text(encoding="utf-8") == build_xmp(state)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise OutputError(f"Cannot validate sidecar {sidecar_path}: {exc}") from exc


def write_sidecar(
    state_or_asset: AssetState | Asset,
    albums_or_media: Iterable[str] | Path,
    media_path: Path | None = None,
) -> Path:
    """Atomically write and validate ``<media>.xmp``.

    The three-argument form remains compatible with the original helper.
    """
    if isinstance(state_or_asset, AssetState):
        if not isinstance(albums_or_media, Path) or media_path is not None:
            raise TypeError("write_sidecar(state, media_path) expected")
        state = state_or_asset
        target_media = albums_or_media
    else:
        if isinstance(albums_or_media, Path) or media_path is None:
            raise TypeError("write_sidecar(asset, albums, media_path) expected")
        state = _state_from_asset(state_or_asset, albums_or_media)
        target_media = media_path
    sidecar_path = target_media.with_name(target_media.name + ".xmp")
    body = build_xmp(state)
    atomic_write_text(sidecar_path, body, operation="write XMP sidecar")
    if not sidecar_matches(state, target_media):
        raise OutputError(f"XMP sidecar validation failed after writing {sidecar_path}.")
    return sidecar_path
