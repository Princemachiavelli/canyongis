from __future__ import annotations

import folium
import geopandas as gpd

from .io import log


def render_folium_map(
    rappel_pts: gpd.GeoDataFrame,
    centerlines: gpd.GeoDataFrame,
    dem_bounds,
    out_path: str,
    max_points: int = 500,
) -> None:
    """Render a Folium HTML map of ML-detected centerlines and rappel points.

    `centerlines` is expected to have `mean_prob` and `length_m` columns (from the
    ML predict pipeline). `rappel_pts` is expected to have a `prob` column.
    """
    pts = rappel_pts.to_crs("EPSG:4326") if not rappel_pts.empty else rappel_pts
    lines = centerlines.to_crs("EPSG:4326") if not centerlines.empty else centerlines

    minx, miny, maxx, maxy = dem_bounds
    center = [(miny + maxy) / 2, (minx + maxx) / 2]

    m = folium.Map(location=center, zoom_start=14, tiles=None)
    folium.TileLayer(
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
        attr="Esri World Topo",
        name="Topo",
    ).add_to(m)
    folium.TileLayer(
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri World Imagery",
        name="Satellite",
    ).add_to(m)

    if not lines.empty:
        tooltip_fields = [c for c in ("mean_prob", "length_m") if c in lines.columns]
        folium.GeoJson(
            lines.__geo_interface__,
            name="ML centerlines",
            style_function=lambda _: {"color": "#00b4d8", "weight": 3},
            tooltip=folium.GeoJsonTooltip(
                fields=tooltip_fields,
                aliases=[f.replace("_", " ") for f in tooltip_fields],
                localize=True,
            )
            if tooltip_fields
            else None,
        ).add_to(m)

    if not pts.empty:
        prob_col = "prob" if "prob" in pts.columns else None
        pts_render = (
            pts if prob_col is None else pts.sort_values(prob_col, ascending=False).head(max_points)
        )
        fg = folium.FeatureGroup(name="Rappel candidates")
        for _, row in pts_render.iterrows():
            p = float(row[prob_col]) if prob_col else 1.0
            color = f"#ff{int(255 * (1 - p)):02x}00"
            folium.CircleMarker(
                location=[row.geometry.y, row.geometry.x],
                radius=max(3, min(10, 3 + 7 * p)),
                color=color,
                fill=True,
                fillColor=color,
                fillOpacity=0.85,
                popup=f"p = {p:.2f}",
            ).add_to(fg)
        fg.add_to(m)

    folium.LayerControl().add_to(m)
    m.save(out_path)
    log(f"Folium map saved: {out_path}")
