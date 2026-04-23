from __future__ import annotations

import folium
import geopandas as gpd

from .config import Config
from .io import log


def render_folium_map(
    rappel_pts: gpd.GeoDataFrame,
    candidate_routes: gpd.GeoDataFrame,
    dem_bounds,
    out_path: str,
    cfg: Config,
) -> None:
    pts = rappel_pts.to_crs("EPSG:4326") if not rappel_pts.empty else rappel_pts
    rts = candidate_routes.to_crs("EPSG:4326") if not candidate_routes.empty else candidate_routes

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

    if not rts.empty:
        folium.GeoJson(
            rts.__geo_interface__,
            name="Candidate routes",
            style_function=lambda _: {
                "color": "#00b4d8",
                "weight": 2,
                "fillColor": "#00b4d8",
                "fillOpacity": 0.25,
            },
            tooltip=folium.GeoJsonTooltip(
                fields=["total_descent_m", "grade", "cliff_count"],
                aliases=["Descent (m)", "Grade", "Cliffs"],
                localize=True,
            ),
        ).add_to(m)

    if not pts.empty:
        pts_render = pts.sort_values("drop_height_m", ascending=False).head(
            cfg.folium_max_points
        )
        max_h = max(pts_render["drop_height_m"].max(), 1.0)
        fg = folium.FeatureGroup(name="Rappel candidates")
        for _, row in pts_render.iterrows():
            h = row["drop_height_m"]
            norm = min(h / max_h, 1.0)
            color = f"#ff{int(255 * (1 - norm)):02x}00"
            folium.CircleMarker(
                location=[row.geometry.y, row.geometry.x],
                radius=max(3, min(10, h / 3)),
                color=color,
                fill=True,
                fillColor=color,
                fillOpacity=0.85,
                popup=f"Estimated drop: {h:.1f} m",
            ).add_to(fg)
        fg.add_to(m)

    folium.LayerControl().add_to(m)
    m.save(out_path)
    log(f"Folium map saved: {out_path}")
