// Read a canyon GeoPackage (schema v2 to v4) with @ngageoint/geopackage.
// Works in the browser (global `GeoPackage`) and in Node (require) for tests.
(function (root) {
  "use strict";

  // Geometry blob -> GeoJSON geometry ({type, coordinates}) via @ngageoint/geopackage.
  // Returns null for NULL blobs.
  function decodeGeometry(lib, blob) {
    if (!blob) return null;
    const geojson = new lib.GeometryData(blob).toGeoJSON();
    if (!geojson) return null;
    return geojson.type === "Feature" ? geojson.geometry || null : geojson;
  }

  // Open a GeoPackage with @ngageoint/geopackage. `lib` is the library module (Node
  // require) or the browser global `GeoPackage`. Returns the handle the readers take.
  async function openDatabase(lib, bytes) {
    const geopackage = await lib.GeoPackageAPI.open(bytes);
    return {
      geopackage,
      // `run` queries and returns rows; `exec` runs one statement (or, without params, a
      // script) and returns {lastInsertRowid, changes}; `transaction` is synchronous.
      run: (sql, params = []) => geopackage.connection.all(sql, params),
      exec: (sql, params) => geopackage.connection.run(sql, params),
      transaction: (work) => geopackage.connection.transaction(work),
      // The whole database as file bytes (a Uint8Array). Frees the driver's cached statements.
      export: () => geopackage.connection.export(),
      decode: (blob) => decodeGeometry(lib, blob),
      close: () => geopackage.connection.close(),
    };
  }

  // Canyon list for the sidebar and map. `db` comes from openDatabase().
  function listCanyons(db) {
    return db.run(
      `SELECT id, ropewiki_title, region, technical_rating, water_rating, time_rating,
              number_of_rappels, geom
         FROM canyons ORDER BY ropewiki_title`
    ).map((row) => {
      const point = db.decode(row.geom);
      return {
        id: row.id,
        title: row.ropewiki_title,
        region: row.region,
        technicalRating: row.technical_rating,
        waterRating: row.water_rating,
        timeRating: row.time_rating,
        rappelCount: row.number_of_rappels,
        lon: point ? point.coordinates[0] : null,
        lat: point ? point.coordinates[1] : null,
      };
    });
  }

  // Columns that only newer files have: the ones of `wanted` that `table` really has, so a file
  // built before they existed still loads (the missing values read as null).
  function presentColumns(db, table, wanted) {
    const have = new Set(db.run(`SELECT name FROM pragma_table_info('${table}')`).map((r) => r.name));
    return wanted.filter((column) => have.has(column));
  }
  const withColumns = (base, extra) => (extra.length ? `${base}, ${extra.join(", ")}` : base);

  // The cached weather forecast stored for a canyon (same shape as forecast.parseForecast), or
  // null. Files built before schema version 3 have no forecast tables.
  function loadForecast(db, canyonId) {
    const hasTable = db.run(
      "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'forecasts'").length > 0;
    if (!hasTable) return null;
    const [head] = db.run("SELECT * FROM forecasts WHERE canyon_id = ?", [canyonId]);
    if (!head) return null;
    return {
      source: head.source,
      fetched_at: head.fetched_at,
      latitude: head.latitude,
      longitude: head.longitude,
      elevation_m: head.elevation_m,
      timezone: head.timezone,
      days: db.run(
        `SELECT date, weather_code, temp_max_c, temp_min_c, precip_mm, precip_prob_pct,
                wind_max_kmh, gust_max_kmh, sunrise, sunset
           FROM forecast_days WHERE forecast_id = ? ORDER BY date`, [head.id]),
      // Hourly values (none in a file saved before they existed).
      hours: db.run("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'forecast_hours'").length
        ? db.run(
          `SELECT time, weather_code, temp_c, precip_mm, precip_prob_pct, wind_kmh, gust_kmh
             FROM forecast_hours WHERE forecast_id = ? ORDER BY time`, [head.id])
        : [],
    };
  }

  // Everything shown on a canyon's detail view, in one object.
  function loadCanyon(db, id) {
    const run = db.run;
    const [canyon] = run("SELECT * FROM canyons WHERE id = ?", [id]);
    if (!canyon) return null;
    const conditions = run(
      `SELECT * FROM conditions WHERE canyon_id = ?
        ORDER BY trip_date DESC, id DESC`, [id]
    ).map((c) => {
      const notes = run(
        "SELECT rappel_label, note FROM condition_rappel_notes WHERE report_id = ? ORDER BY id",
        [c.id]
      );
      const photos = run(
        "SELECT filename FROM condition_photos WHERE report_id = ? ORDER BY sequence", [c.id]
      ).map((p) => p.filename);
      return { ...c, rappelNotes: notes, photos };
    });
    const geometry = run(
      `SELECT ${withColumns("kind, name, length_m, geom", presentColumns(db, "routes", ["color"]))}
         FROM routes WHERE canyon_id = ? ORDER BY id`, [id]
    ).map((r) => ({ kind: r.kind, name: r.name, lengthM: r.length_m, color: r.color ?? null,
                    line: db.decode(r.geom) }));
    return {
      canyon: {
        id: canyon.id,
        title: canyon.ropewiki_title,
        aka: canyon.also_known_as,
        region: canyon.region,
        technicalRating: canyon.technical_rating,
        waterRating: canyon.water_rating,
        timeRating: canyon.time_rating,
        numberOfRappels: canyon.number_of_rappels,
        longestRappelM: canyon.longest_rappel_m,
        hikeLengthKm: canyon.hike_length_km,
        approachGainM: canyon.approach_gain_m,
        exitGainM: canyon.exit_gain_m,
        fastestTimeH: canyon.fastest_time_h,
        slowestTimeH: canyon.slowest_time_h,
        permits: canyon.permits,
        bestSeason: canyon.best_season,
        vehicle: canyon.vehicle,
        shuttle: canyon.shuttle,
        explored: canyon.explored,
        sourceUrl: canyon.source_url,
        fetchedAt: canyon.fetched_at,
        revisionId: canyon.source_revision_id,
        licenseName: canyon.license_name ?? null,
        licenseUrl: canyon.license_url ?? null,
        anonymousAuthors: canyon.anonymous_authors ?? null,
        point: db.decode(canyon.geom),
      },
      sections: run(
        "SELECT position, heading, body_text, body_html FROM beta WHERE canyon_id = ? ORDER BY position",
        [id]
      ),
      // kml_name / kml_description: the node's own name and text in the KML (null in files
      // built before schema v4); waypoints carry the node's description.
      rappels: run(
        `SELECT ${withColumns("sequence, label, height_m, notes, geom",
          presentColumns(db, "rappels", ["kml_name", "kml_description"]))}
           FROM rappels WHERE canyon_id = ? ORDER BY sequence`, [id]
      ).map((r) => ({ ...r, kml_name: r.kml_name ?? null, kml_description: r.kml_description ?? null,
                      point: db.decode(r.geom) })),
      waypoints: run(
        `SELECT ${withColumns("kind, name, geom", presentColumns(db, "waypoints", ["description"]))}
           FROM waypoints WHERE canyon_id = ? ORDER BY id`, [id]
      ).map((w) => ({ ...w, description: w.description ?? null, point: db.decode(w.geom) })),
      routes: geometry,
      conditions,
      ratings: run(
        "SELECT rater, rating, source_title FROM ratings WHERE canyon_id = ? ORDER BY rating DESC",
        [id]
      ),
      links: run("SELECT label, url, source_name FROM links WHERE canyon_id = ? ORDER BY id", [id]),
      // Ropewiki page authors, for attribution (empty for files built before schema v5).
      authors: presentColumns(db, "authors", ["name"]).length
        ? run("SELECT name FROM authors WHERE canyon_id = ? ORDER BY id", [id]).map((a) => a.name)
        : [],
      forecast: loadForecast(db, id),
    };
  }

  // ---- raster tile layers (gpkg_contents data_type = 'tiles', EPSG:3857) --------------

  const WORLD_HALF_SIZE = 20037508.342789244;
  // Bottom-to-top draw order for the layers canyongis-gpkg-tiles writes; others follow by name.
  const TILE_LAYER_ORDER = [
    "tiles_osm", "tiles_imagery", "tiles_hillshade", "tiles_contours_m", "tiles_contours_ft",
    "tiles_terrain",
  ];
  // Elevation, not a picture: Terrarium-encoded PNG tiles (metres = R * 256 + G + B / 256 - 32768)
  // that the 3D map drapes the other layers over. Never shown as a layer of its own.
  const TERRAIN_TABLE = "tiles_terrain";

  function mercatorToLonLat(x, y) {
    const lon = (x / WORLD_HALF_SIZE) * 180;
    const lat = (Math.atan(Math.sinh((y / WORLD_HALF_SIZE) * Math.PI)) * 180) / Math.PI;
    return [lon, lat];
  }

  // The tile writer appends the attribution to the description: "Aerial imagery (USDA NAIP)".
  function splitDescription(description) {
    const match = /^(.*?)\s*\(([^()]*)\)\s*$/.exec(description || "");
    return match ? { text: match[1], attribution: match[2] } : { text: description || "", attribution: "" };
  }

  // Tile layers in draw order (bottom first). Only Web Mercator tables are supported. The
  // elevation table (terrain: true) comes last; filter it out to list the pictures.
  function listTileLayers(db) {
    const rows = db.run(
      `SELECT c.table_name, c.identifier, c.description, c.min_x, c.min_y, c.max_x, c.max_y,
              (SELECT MIN(zoom_level) FROM gpkg_tile_matrix m WHERE m.table_name = c.table_name) AS min_zoom,
              (SELECT MAX(zoom_level) FROM gpkg_tile_matrix m WHERE m.table_name = c.table_name) AS max_zoom
         FROM gpkg_contents c
        WHERE c.data_type = 'tiles' AND c.srs_id = 3857 AND c.table_name GLOB '[A-Za-z0-9_]*'`
    ).filter((r) => r.min_zoom !== null && /^\w+$/.test(r.table_name));
    const rank = (name) => {
      const i = TILE_LAYER_ORDER.indexOf(name);
      return i < 0 ? TILE_LAYER_ORDER.length : i;
    };
    rows.sort((a, b) => rank(a.table_name) - rank(b.table_name) || a.table_name.localeCompare(b.table_name));
    return rows.map((r) => {
      const [west, south] = mercatorToLonLat(r.min_x, r.min_y);
      const [east, north] = mercatorToLonLat(r.max_x, r.max_y);
      const { text, attribution } = splitDescription(r.description);
      return {
        table: r.table_name,
        terrain: r.table_name === TERRAIN_TABLE,
        name: r.identifier || r.table_name,
        description: text,
        attribution,
        minZoom: r.min_zoom,
        maxZoom: r.max_zoom,
        bounds: [west, south, east, north],
      };
    });
  }

  function sniffImageType(bytes) {
    if (bytes[0] === 0x89 && bytes[1] === 0x50) return "image/png";
    if (bytes[0] === 0xff && bytes[1] === 0xd8) return "image/jpeg";
    if (bytes[0] === 0x52 && bytes[8] === 0x57) return "image/webp";
    return "application/octet-stream";
  }

  // One tile by XYZ address (y = 0 at the north edge); `table` must come from listTileLayers.
  // Returns {data, type} or null when the layer has no tile there.
  function getTile(db, table, z, x, y) {
    if (!/^\w+$/.test(table) || y < 0 || y >= 2 ** z || x < 0 || x >= 2 ** z) return null;
    const [row] = db.run(
      `SELECT tile_data FROM "${table}" WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?`,
      [z, x, 2 ** z - 1 - y]
    );
    if (!row || !row.tile_data) return null;
    return { data: row.tile_data, type: sniffImageType(row.tile_data) };
  }

  const api = {
    decodeGeometry, openDatabase, listCanyons, loadCanyon, listTileLayers, getTile, TERRAIN_TABLE,
  };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    root.gpkg = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
