"""Compute 15-minute bike-ride isochrones for every BART + Caltrain station
and render an interactive Folium map.

For each station, downloads a small (~5 km) bike-network bbox from
OpenStreetMap via the Kumi Systems Overpass mirror (cached in ./cache/) and
computes the isochrone in parallel threads. Final isochrones are written to
./isochrones.geojson. Subsequent runs reuse both caches; pass --rebuild to
recompute.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import folium
import networkx as nx
import numpy as np
import osmnx as ox
import pandas as pd
import shapely
from scipy.spatial import cKDTree
from shapely.geometry import MultiPoint, mapping
from shapely.geometry.base import BaseGeometry


HERE = Path(__file__).resolve().parent
STATIONS_CSV = HERE / "stations.csv"
CACHE_DIR = HERE / "cache"
ISOCHRONES_GEOJSON = HERE / "isochrones.geojson"
OUTPUT_HTML = HERE / "bay_area_bike_isochrones.html"
OUTPUT_KML = HERE / "bay_area_bike_isochrones.kml"

BIKE_SPEED_KMH = 15.0
RIDE_MINUTES = 15
BBOX_RADIUS_M = 5000   # ~5 km around each station (15 min @ 15 km/h ~= 3.75 km)
DEFAULT_WORKERS = 8

AGENCY_COLORS = {
    "BART": "#0066cc",
    "Caltrain": "#c8102e",
}


def configure_osmnx() -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    ox.settings.use_cache = True
    ox.settings.cache_folder = str(CACHE_DIR)
    ox.settings.log_console = False
    ox.settings.requests_timeout = 180
    # Use the main Overpass server, which serves small-bbox queries quickly
    # and supports up to ~4 concurrent slots per IP. Kumi gets congested under
    # load.
    ox.settings.overpass_url = "https://overpass-api.de/api/interpreter"
    ox.settings.overpass_rate_limit = False


def add_travel_time(G: nx.MultiDiGraph, speed_kmh: float) -> None:
    speed_mps = speed_kmh * 1000.0 / 3600.0
    for _, _, data in G.edges(data=True):
        length_m = data.get("length", 0.0) or 0.0
        data["travel_time"] = length_m / speed_mps if speed_mps > 0 else 0.0


class NodeIndex:
    """KDTree-backed nearest-node lookup over an OSMnx graph.
    O(log N) per query vs O(N) linear scan."""

    def __init__(self, G: nx.MultiDiGraph):
        nodes = list(G.nodes(data=True))
        self.node_ids = np.array([n for n, _ in nodes])
        self.coords = np.array([(d["x"], d["y"]) for _, d in nodes],
                                dtype=np.float64)
        self.tree = cKDTree(self.coords)

    def nearest(self, lon: float, lat: float) -> int:
        _, idx = self.tree.query([lon, lat], k=1)
        return int(self.node_ids[idx])


def isochrone_polygon(
    lat: float,
    lon: float,
    minutes: float,
    speed_kmh: float,
    bbox_radius_m: int,
) -> BaseGeometry | None:
    """Download a small bike-network bbox around (lat, lon), compute the
    set of nodes reachable within `minutes`, and return the concave hull."""
    try:
        G = ox.graph_from_point(
            (lat, lon),
            dist=bbox_radius_m,
            network_type="bike",
            simplify=True,
        )
    except Exception as e:
        print(f"  ! graph download failed: {e}", file=sys.stderr)
        return None
    if G.number_of_nodes() == 0:
        return None

    add_travel_time(G, speed_kmh)
    index = NodeIndex(G)
    center_node = index.nearest(lon, lat)

    cutoff_seconds = minutes * 60.0
    sub = nx.ego_graph(G, center_node, radius=cutoff_seconds,
                       distance="travel_time", undirected=True)
    if sub.number_of_nodes() < 3:
        return None

    pts = MultiPoint([(d["x"], d["y"]) for _, d in sub.nodes(data=True)])
    try:
        poly = shapely.concave_hull(pts, ratio=0.3)
    except Exception:
        poly = pts.convex_hull
    if poly is None or poly.is_empty or poly.geom_type in ("Point", "LineString"):
        poly = pts.convex_hull
    if poly.is_empty:
        return None
    return poly


_print_lock = threading.Lock()


def _process_station(idx: int, total: int, row) -> dict | None:
    t0 = time.time()
    poly = isochrone_polygon(
        lat=row.lat,
        lon=row.lon,
        minutes=RIDE_MINUTES,
        speed_kmh=BIKE_SPEED_KMH,
        bbox_radius_m=BBOX_RADIUS_M,
    )
    dt = time.time() - t0
    with _print_lock:
        status = "ok" if poly is not None else "FAIL"
        print(f"[{idx:>2}/{total}] {status:<4} ({dt:5.1f}s) "
              f"{row.agency:<8} {row.name}", flush=True)
    if poly is None:
        return None
    return {
        "type": "Feature",
        "geometry": mapping(poly),
        "properties": {
            "agency": row.agency,
            "name": row.name,
            "lat": row.lat,
            "lon": row.lon,
            "ride_minutes": RIDE_MINUTES,
            "speed_kmh": BIKE_SPEED_KMH,
        },
    }


def build_isochrones(stations: pd.DataFrame, workers: int = DEFAULT_WORKERS) -> dict:
    n = len(stations)
    rows = list(stations.itertuples(index=False))
    features: list[dict] = []
    print(f"Computing {n} isochrones with {workers} threads via Kumi mirror...",
          flush=True)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_process_station, i + 1, n, row): row
            for i, row in enumerate(rows)
        }
        for fut in as_completed(futures):
            feat = fut.result()
            if feat is not None:
                features.append(feat)

    features.sort(key=lambda f: (f["properties"]["agency"], f["properties"]["name"]))
    elapsed = time.time() - t0
    print(f"\nBuilt {len(features)}/{n} isochrones in {elapsed:.1f}s "
          f"({elapsed/n:.2f}s/station avg)")
    return {"type": "FeatureCollection", "features": features}


def render_map(stations: pd.DataFrame, geojson: dict, out_path: Path) -> None:
    center_lat = stations["lat"].mean()
    center_lon = stations["lon"].mean()
    fmap = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=10,
        tiles="CartoDB positron",
        control_scale=True,
    )

    # Additional basemaps. Skipping the raw OpenStreetMap.org tile layer
    # because their tile-usage policy returns 403 without a Referer header
    # when embedded - we use OSM-derived tiles via Carto/Esri instead.
    folium.TileLayer(
        "CartoDB Voyager",
        name="OSM (Carto Voyager)",
        attr="\u00a9 OpenStreetMap contributors \u00a9 CARTO",
    ).add_to(fmap)
    folium.TileLayer(
        tiles=("https://server.arcgisonline.com/ArcGIS/rest/services/"
               "World_Imagery/MapServer/tile/{z}/{y}/{x}"),
        name="Satellite (Esri)",
        attr=("Tiles \u00a9 Esri \u2014 Source: Esri, Maxar, Earthstar "
              "Geographics, and the GIS User Community"),
        max_zoom=19,
    ).add_to(fmap)

    feats_by_agency: dict[str, list] = {a: [] for a in AGENCY_COLORS}
    for feat in geojson["features"]:
        feats_by_agency.setdefault(feat["properties"]["agency"], []).append(feat)

    for agency, color in AGENCY_COLORS.items():
        feats = feats_by_agency.get(agency, [])
        if not feats:
            continue
        layer = folium.FeatureGroup(
            name=f"{agency} - 15 min bike ({len(feats)} stations)",
            show=True,
        )
        folium.GeoJson(
            {"type": "FeatureCollection", "features": feats},
            name=f"{agency} isochrones",
            style_function=lambda _f, c=color: {
                "fillColor": c,
                "color": c,
                "weight": 1,
                "fillOpacity": 0.18,
                "opacity": 0.6,
            },
            highlight_function=lambda _f: {"weight": 2, "fillOpacity": 0.32},
            tooltip=folium.GeoJsonTooltip(
                fields=["agency", "name", "ride_minutes", "speed_kmh"],
                aliases=["Agency", "Station", "Minutes", "km/h"],
                sticky=False,
            ),
        ).add_to(layer)
        layer.add_to(fmap)

    for agency, color in AGENCY_COLORS.items():
        marker_layer = folium.FeatureGroup(name=f"{agency} stations", show=True)
        sub = stations[stations["agency"] == agency]
        for row in sub.itertuples(index=False):
            folium.CircleMarker(
                location=[row.lat, row.lon],
                radius=4,
                color="#222",
                weight=1,
                fill=True,
                fill_color=color,
                fill_opacity=0.95,
                popup=folium.Popup(
                    f"<b>{row.name}</b><br>{row.agency}<br>"
                    f"{RIDE_MINUTES} min @ {BIKE_SPEED_KMH:g} km/h",
                    max_width=260,
                ),
            ).add_to(marker_layer)
        marker_layer.add_to(fmap)

    legend_html = f"""
    <div style="position: fixed; bottom: 24px; left: 24px; z-index: 9999;
                background: white; padding: 10px 12px; border-radius: 6px;
                box-shadow: 0 1px 4px rgba(0,0,0,0.25);
                font-family: -apple-system, system-ui, sans-serif; font-size: 12px;">
      <div style="font-weight:600; margin-bottom:6px;">
        {RIDE_MINUTES}-min bike ride ({BIKE_SPEED_KMH:g} km/h)
      </div>
      <div><span style="display:inline-block;width:12px;height:12px;
            background:{AGENCY_COLORS['BART']};opacity:.6;margin-right:6px;"></span>BART</div>
      <div><span style="display:inline-block;width:12px;height:12px;
            background:{AGENCY_COLORS['Caltrain']};opacity:.6;margin-right:6px;"></span>Caltrain</div>
    </div>
    """
    fmap.get_root().html.add_child(folium.Element(legend_html))

    folium.LayerControl(collapsed=False).add_to(fmap)

    bounds = [
        [stations["lat"].min(), stations["lon"].min()],
        [stations["lat"].max(), stations["lon"].max()],
    ]
    fmap.fit_bounds(bounds, padding=(20, 20))

    fmap.save(str(out_path))
    print(f"Wrote {out_path}")


def _web_rgb_to_kml(rgb_hex: str, alpha: int = 0x99) -> str:
    """Convert a web color like '#0066cc' to KML 'AABBGGRR' format.
    KML uses a non-standard byte order: alpha, then blue, green, red."""
    rgb_hex = rgb_hex.lstrip("#")
    r, g, b = rgb_hex[0:2], rgb_hex[2:4], rgb_hex[4:6]
    return f"{alpha:02x}{b}{g}{r}"


def write_kml(geojson: dict, stations: pd.DataFrame, out_path: Path) -> None:
    """Write a Google Earth-compatible KML with one polygon per station and a
    point marker for each. No external KML library needed."""
    from xml.sax.saxutils import escape

    fill_alpha = 0x4d   # ~30% opaque polygon fills
    line_alpha = 0xff   # solid outlines
    agency_kml_fill = {a: _web_rgb_to_kml(c, fill_alpha)
                       for a, c in AGENCY_COLORS.items()}
    agency_kml_line = {a: _web_rgb_to_kml(c, line_alpha)
                       for a, c in AGENCY_COLORS.items()}

    parts: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2">',
        '<Document>',
        f'<name>Bay Area BART + Caltrain {RIDE_MINUTES}-min bike isochrones</name>',
    ]

    for agency in AGENCY_COLORS:
        line_color = agency_kml_line[agency]
        fill_color = agency_kml_fill[agency]
        parts.append(f'<Style id="poly_{agency}">')
        parts.append(f'<LineStyle><color>{line_color}</color><width>2</width>'
                     '</LineStyle>')
        parts.append(f'<PolyStyle><color>{fill_color}</color><fill>1</fill>'
                     '<outline>1</outline></PolyStyle>')
        parts.append('</Style>')
        parts.append(f'<Style id="pin_{agency}">')
        parts.append('<IconStyle>'
                     f'<color>{line_color}</color><scale>0.8</scale>'
                     '<Icon><href>http://maps.google.com/mapfiles/kml/shapes/cycling.png</href></Icon>'
                     '</IconStyle>')
        parts.append('</Style>')

    def _coords_string(ring) -> str:
        return " ".join(f"{x:.6f},{y:.6f},0" for x, y in ring)

    parts.append('<Folder><name>Isochrones</name>')
    for feat in geojson["features"]:
        agency = feat["properties"]["agency"]
        name = escape(feat["properties"]["name"])
        geom = feat["geometry"]
        polys = (geom["coordinates"] if geom["type"] == "MultiPolygon"
                 else [geom["coordinates"]])
        parts.append('<Placemark>')
        parts.append(f'<name>{name}</name>')
        parts.append(f'<description>{agency} - {RIDE_MINUTES} min bike '
                     f'@ {BIKE_SPEED_KMH:g} km/h</description>')
        parts.append(f'<styleUrl>#poly_{agency}</styleUrl>')
        if len(polys) > 1:
            parts.append('<MultiGeometry>')
        for poly in polys:
            parts.append('<Polygon><outerBoundaryIs><LinearRing>'
                         f'<coordinates>{_coords_string(poly[0])}</coordinates>'
                         '</LinearRing></outerBoundaryIs>')
            for hole in poly[1:]:
                parts.append('<innerBoundaryIs><LinearRing>'
                             f'<coordinates>{_coords_string(hole)}</coordinates>'
                             '</LinearRing></innerBoundaryIs>')
            parts.append('</Polygon>')
        if len(polys) > 1:
            parts.append('</MultiGeometry>')
        parts.append('</Placemark>')
    parts.append('</Folder>')

    parts.append('<Folder><name>Stations</name>')
    for row in stations.itertuples(index=False):
        parts.append('<Placemark>')
        parts.append(f'<name>{escape(row.name)}</name>')
        parts.append(f'<description>{row.agency}</description>')
        parts.append(f'<styleUrl>#pin_{row.agency}</styleUrl>')
        parts.append(f'<Point><coordinates>{row.lon:.6f},{row.lat:.6f},0'
                     '</coordinates></Point>')
        parts.append('</Placemark>')
    parts.append('</Folder>')

    parts.append('</Document></kml>')
    out_path.write_text("\n".join(parts), encoding="utf-8")
    print(f"Wrote {out_path}")


def load_or_build_geojson(stations: pd.DataFrame, rebuild: bool,
                           workers: int = DEFAULT_WORKERS) -> dict:
    if ISOCHRONES_GEOJSON.exists() and not rebuild:
        print(f"Loading cached isochrones from {ISOCHRONES_GEOJSON}")
        with open(ISOCHRONES_GEOJSON) as f:
            return json.load(f)

    configure_osmnx()
    geojson = build_isochrones(stations, workers=workers)
    with open(ISOCHRONES_GEOJSON, "w") as f:
        json.dump(geojson, f)
    print(f"Wrote {ISOCHRONES_GEOJSON}")
    return geojson


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rebuild", action="store_true",
                        help="Recompute isochrones from scratch")
    parser.add_argument("--map-only", action="store_true",
                        help="Only render the map from existing isochrones.geojson")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"Parallel worker threads (default {DEFAULT_WORKERS})")
    parser.add_argument("--kml", action="store_true",
                        help="Also write a Google Earth-compatible KML file")
    args = parser.parse_args()

    if not STATIONS_CSV.exists():
        print(f"Missing {STATIONS_CSV}", file=sys.stderr)
        return 1

    stations = pd.read_csv(STATIONS_CSV)
    print(f"Loaded {len(stations)} stations "
          f"({(stations['agency']=='BART').sum()} BART, "
          f"{(stations['agency']=='Caltrain').sum()} Caltrain)")

    if args.map_only:
        if not ISOCHRONES_GEOJSON.exists():
            print(f"{ISOCHRONES_GEOJSON} not found; run without --map-only first",
                  file=sys.stderr)
            return 1
        with open(ISOCHRONES_GEOJSON) as f:
            geojson = json.load(f)
    else:
        geojson = load_or_build_geojson(stations, rebuild=args.rebuild,
                                         workers=args.workers)

    render_map(stations, geojson, OUTPUT_HTML)
    if args.kml:
        write_kml(geojson, stations, OUTPUT_KML)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
