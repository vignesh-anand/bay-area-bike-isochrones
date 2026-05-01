"""Render a static PNG preview of the isochrones for the README."""
from pathlib import Path

import contextily as cx
import geopandas as gpd
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import pandas as pd

HERE = Path(__file__).resolve().parent
GEOJSON = HERE / "isochrones.geojson"
STATIONS = HERE / "stations.csv"
OUT_PNG = HERE / "docs" / "preview.png"

AGENCY_COLORS = {"BART": "#0066cc", "Caltrain": "#c8102e"}


def main() -> int:
    gdf = gpd.read_file(GEOJSON).set_crs(4326).to_crs(3857)
    stations = pd.read_csv(STATIONS)
    sgdf = gpd.GeoDataFrame(
        stations,
        geometry=gpd.points_from_xy(stations.lon, stations.lat),
        crs=4326,
    ).to_crs(3857)

    fig, ax = plt.subplots(figsize=(12, 13), dpi=120)

    for agency, color in AGENCY_COLORS.items():
        sub = gdf[gdf["agency"] == agency]
        if not sub.empty:
            sub.plot(ax=ax, color=color, alpha=0.28,
                     edgecolor=color, linewidth=0.6)

    for agency, color in AGENCY_COLORS.items():
        sgdf[sgdf["agency"] == agency].plot(
            ax=ax, color=color, edgecolor="black",
            markersize=18, linewidth=0.4, zorder=5,
        )

    cx.add_basemap(ax, source=cx.providers.CartoDB.Positron, attribution_size=6)

    ax.set_axis_off()
    ax.set_title("Bay Area: 15-min bike isochrones around BART + Caltrain stations",
                 fontsize=14, pad=12)
    legend_handles = [
        mpatches.Patch(color=c, alpha=0.5, label=f"{a} (15 min bike)")
        for a, c in AGENCY_COLORS.items()
    ]
    ax.legend(handles=legend_handles, loc="upper right", framealpha=0.9)

    OUT_PNG.parent.mkdir(exist_ok=True)
    fig.tight_layout()
    fig.savefig(OUT_PNG, bbox_inches="tight", facecolor="white")
    print(f"Wrote {OUT_PNG} ({OUT_PNG.stat().st_size/1e3:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
