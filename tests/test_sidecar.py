from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from immich_export.models import Asset
from immich_export.sidecar import NS, build_xmp, format_gps, write_sidecar

from .fake_immich import make_asset


def _rich_asset() -> Asset:
    raw = make_asset(
        "a1",
        "IMG_0001.jpg",
        b"x",
        "2019-04-12T10:00:00.000Z",
        people=[{"id": "p1", "name": "Anna"}],
        tags=[{"id": "t1", "name": "japan", "value": "travel/japan"}],
        description="Tokyo tower",
        latitude=35.6586,
        longitude=-139.7454,
        favorite=True,
    )
    return Asset.model_validate(raw)


def _texts(root: ET.Element, xpath: str) -> list[str]:
    return [el.text or "" for el in root.findall(xpath, NS)]


class TestBuildXmp:
    def test_contains_all_metadata(self) -> None:
        xmp = build_xmp(_rich_asset(), albums=["Japan 2019"])
        root = ET.fromstring(xmp.split("?>\n", 1)[1].rsplit("\n<?xpacket", 1)[0])
        desc = root.find("rdf:RDF/rdf:Description", NS)
        assert desc is not None

        assert _texts(desc, "dc:subject/rdf:Bag/rdf:li") == ["travel/japan"]
        assert _texts(desc, "Iptc4xmpExt:PersonInImage/rdf:Bag/rdf:li") == ["Anna"]
        assert _texts(desc, "immich:Albums/rdf:Bag/rdf:li") == ["Japan 2019"]
        assert _texts(desc, "dc:description/rdf:Alt/rdf:li") == ["Tokyo tower"]
        assert _texts(desc, "xmp:Rating") == ["5"]
        assert _texts(desc, "immich:AssetId") == ["a1"]
        (lat,) = _texts(desc, "exif:GPSLatitude")
        (lon,) = _texts(desc, "exif:GPSLongitude")
        assert lat.endswith("N") and lat.startswith("35,")
        assert lon.endswith("W") and lon.startswith("139,")

    def test_minimal_asset_omits_empty_sections(self) -> None:
        raw = make_asset("a2", "IMG_0002.jpg", b"x", "2019-04-13T10:00:00.000Z")
        xmp = build_xmp(Asset.model_validate(raw), albums=[])
        assert "dc:subject" not in xmp
        assert "PersonInImage" not in xmp
        assert "GPSLatitude" not in xmp
        assert "Rating" not in xmp
        assert "photoshop:DateCreated" in xmp


class TestFormatGps:
    def test_north_east(self) -> None:
        assert format_gps(35.5, is_latitude=True) == "35,30.000000N"
        assert format_gps(139.75, is_latitude=False) == "139,45.000000E"

    def test_south_west(self) -> None:
        assert format_gps(-33.9, is_latitude=True).endswith("S")
        assert format_gps(-70.6, is_latitude=False).endswith("W")


def test_write_sidecar_places_file_next_to_media(tmp_path: Path) -> None:
    media = tmp_path / "IMG_0001.jpg"
    media.write_bytes(b"x")
    sidecar = write_sidecar(_rich_asset(), ["Japan 2019"], media)
    assert sidecar == tmp_path / "IMG_0001.jpg.xmp"
    assert "travel/japan" in sidecar.read_text(encoding="utf-8")
