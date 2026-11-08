// Read a canyon GeoPackage (schema v2) with @ngageoint/geopackage.
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
      run: (sql, params = []) => geopackage.connection.all(sql, params),
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
      `SELECT kind, name, length_m, geom FROM routes WHERE canyon_id = ? ORDER BY id`, [id]
    ).map((r) => ({ kind: r.kind, name: r.name, lengthM: r.length_m,
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
        point: db.decode(canyon.geom),
      },
      sections: run(
        "SELECT position, heading, body_text, body_html FROM beta WHERE canyon_id = ? ORDER BY position",
        [id]
      ),
      rappels: run(
        `SELECT sequence, label, height_m, notes, geom FROM rappels
          WHERE canyon_id = ? ORDER BY sequence`, [id]
      ).map((r) => ({ ...r, point: db.decode(r.geom) })),
      waypoints: run(
        "SELECT kind, name, geom FROM waypoints WHERE canyon_id = ? ORDER BY id", [id]
      ).map((w) => ({ ...w, point: db.decode(w.geom) })),
      routes: geometry,
      conditions,
      ratings: run(
        "SELECT rater, rating, source_title FROM ratings WHERE canyon_id = ? ORDER BY rating DESC",
        [id]
      ),
      links: run("SELECT label, url, source_name FROM links WHERE canyon_id = ? ORDER BY id", [id]),
    };
  }

  const api = { decodeGeometry, openDatabase, listCanyons, loadCanyon };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    root.gpkg = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
