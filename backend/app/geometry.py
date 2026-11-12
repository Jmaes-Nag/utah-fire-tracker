"""Coordinate and polygon validation, and the wind-corridor threat geometry."""

import math

from shapely import make_valid
from shapely.affinity import scale, translate
from shapely.errors import GEOSException
from shapely.geometry import LineString, Point, box, shape
from shapely.ops import unary_union

from . import config
from .util import finite_number

UTAH_ENVELOPE = box(*config.UTAH_BOUNDS)


def coordinates(lon, lat):
    lon, lat = finite_number(lon), finite_number(lat)
    if lon is None or lat is None or not (-180 <= lon <= 180 and -90 <= lat <= 90):
        return None
    return lon, lat


def utah_point(lon, lat):
    point = coordinates(lon, lat)
    return point if point and UTAH_ENVELOPE.covers(Point(*point)) else None


def polygon_geometry(geometry):
    """Validate/repair polygonal GeoJSON without allowing NaN or non-WGS84 coordinates."""
    if not isinstance(geometry, dict) or geometry.get("type") not in {"Polygon", "MultiPolygon"}:
        raise ValueError("Expected polygon geometry")
    polygons = geometry.get("coordinates")
    polygons = [polygons] if geometry["type"] == "Polygon" else polygons
    if not isinstance(polygons, list) or not polygons:
        raise ValueError("Empty polygon")
    for rings in polygons:
        if not isinstance(rings, list) or not rings:
            raise ValueError("Missing polygon rings")
        for ring in rings:
            if not isinstance(ring, list) or len(ring) < 4:
                raise ValueError("Invalid polygon ring")
            for point in ring:
                if (
                    not isinstance(point, (list, tuple))
                    or len(point) not in (2, 3)
                    or coordinates(*point[:2]) is None
                    or any(finite_number(value) is None for value in point)
                ):
                    raise ValueError("Invalid polygon coordinate")
            if ring[0][:2] != ring[-1][:2]:
                raise ValueError("Unclosed polygon ring")
    try:
        geom = shape(geometry)
        if not geom.is_valid:
            geom = make_valid(geom)
    except GEOSException as exc:
        raise ValueError("Invalid polygon geometry") from exc
    if geom.geom_type == "GeometryCollection":
        geom = unary_union([g for g in geom.geoms if g.geom_type in {"Polygon", "MultiPolygon"}])
    if geom.is_empty or geom.geom_type not in {"Polygon", "MultiPolygon"} or geom.area <= 0:
        raise ValueError("Degenerate polygon")
    return geom


def arcgis_polygon(rings):
    """ArcGIS rings may be separate shells, holes, or nested islands (even-odd fill)."""
    if not isinstance(rings, list) or not rings:
        raise ValueError("Missing ArcGIS rings")
    result = None
    for ring in rings:
        poly = polygon_geometry({"type": "Polygon", "coordinates": [ring]})
        result = poly if result is None else result.symmetric_difference(poly)
    if result.is_empty or not result.is_valid:
        raise ValueError("Invalid ArcGIS polygon")
    return result


def threat_geometry(lat, lon, speed, direction):
    """Local miles projection corrects longitude scale and fills the complete wind corridor."""
    miles_per_lat = 69.0
    miles_per_lon = miles_per_lat * math.cos(math.radians(lat))
    speed = finite_number(speed) or 0.0
    direction = finite_number(direction)
    dx = dy = 0.0
    if direction is not None and speed > 0:
        downwind = math.radians((direction + 180) % 360)
        dx, dy = math.sin(downwind) * speed * 0.2, math.cos(downwind) * speed * 0.2
    # Operate in local miles, then map back to WGS84 for municipality overlay.
    base = Point(0, 0).buffer(5.0)
    plume = (LineString([(0, 0), (dx, dy)]) if dx or dy else Point(0, 0)).buffer(5.0)

    def to_geo(geom):
        return translate(
            scale(geom, xfact=1 / miles_per_lon, yfact=1 / miles_per_lat, origin=(0, 0)), xoff=lon, yoff=lat
        )

    return to_geo(base), to_geo(plume), miles_per_lon, miles_per_lat
