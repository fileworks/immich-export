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
from pathlib import Path

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


def build_xmp(asset: Asset, albums: list[str]) -> str:
    root = ET.Element(_q("x", "xmpmeta"))
    rdf = ET.SubElement(root, _q("rdf", "RDF"))
    desc = ET.SubElement(rdf, _q("rdf", "Description"), {_q("rdf", "about"): ""})

    _bag(desc, _q("dc", "subject"), [tag.value for tag in asset.tags])
    _bag(desc, _q("Iptc4xmpExt", "PersonInImage"), [p.name for p in asset.people if p.name])
    _bag(desc, _q("immich", "Albums"), sorted(albums))

    if asset.description:
        prop = ET.SubElement(desc, _q("dc", "description"))
        alt = ET.SubElement(prop, _q("rdf", "Alt"))
        li = ET.SubElement(alt, _q("rdf", "li"))
        li.set("{http://www.w3.org/XML/1998/namespace}lang", "x-default")
        li.text = asset.description

    ET.SubElement(desc, _q("photoshop", "DateCreated")).text = asset.taken_at.isoformat()
    if asset.is_favorite:
        ET.SubElement(desc, _q("xmp", "Rating")).text = "5"

    exif = asset.exif_info
    if exif is not None and exif.latitude is not None and exif.longitude is not None:
        ET.SubElement(desc, _q("exif", "GPSLatitude")).text = format_gps(
            exif.latitude, is_latitude=True
        )
        ET.SubElement(desc, _q("exif", "GPSLongitude")).text = format_gps(
            exif.longitude, is_latitude=False
        )

    ET.SubElement(desc, _q("immich", "AssetId")).text = asset.id
    ET.SubElement(desc, _q("immich", "Checksum")).text = asset.checksum

    ET.indent(root)
    body = ET.tostring(root, encoding="unicode")
    return f'<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n{body}\n<?xpacket end="w"?>\n'


def write_sidecar(asset: Asset, albums: list[str], media_path: Path) -> Path:
    """Write `<file>.<ext>.xmp` next to the exported/located media file."""
    sidecar_path = media_path.with_name(media_path.name + ".xmp")
    sidecar_path.write_text(build_xmp(asset, albums), encoding="utf-8")
    return sidecar_path
