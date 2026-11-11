// Keeping a downloaded canyon up to date without downloading it again.
//
// The server splits a canyon's data into sections (canyon details, tracks and rappels,
// description, links, condition reports, ratings), each with a fingerprint it made when it
// ingested the data. A file stores the fingerprints it was built from (`section_stamps`), so
// comparing them with the server's manifest says which sections are out of date. The server then
// sends just those sections as a delta, which is applied here by replacing that canyon's rows.
// The viewer never makes fingerprints itself, it only stores and compares the server's.
// Works in the browser (global `gpkgUpdate`) and in Node for tests.
(function (root) {
  "use strict";

  const write = typeof module !== "undefined" && module.exports
    ? require("./gpkg-write.js") : root.gpkgWrite;
  const IDENTIFIER = /^[a-z_][a-z0-9_]*$/;
  const GEOMETRY_TABLES = ["canyons", "routes", "waypoints", "rappels"];

  const iso = (ms = Date.now()) => new Date(ms).toISOString();

  // The fingerprints the file holds for a canyon: {section: {digest, updated_at}}. A file from
  // before they existed has none.
  function readStamps(db, canyonId) {
    const hasTable = db.run(
      "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'section_stamps'").length > 0;
    if (!hasTable) return {};
    return Object.fromEntries(db.run(
      "SELECT section, digest, updated_at FROM section_stamps WHERE canyon_id = ?", [canyonId]
    ).map((r) => [r.section, { digest: r.digest, updated_at: r.updated_at }]));
  }

  // The map layers (tile tables) the file holds: {table: {version, built_at}}. A layer from before
  // layers were stamped counts as version 1, built when its row last changed (as on the server).
  function readTileStamps(db) {
    const layers = Object.fromEntries(db.run(
      "SELECT table_name, last_change FROM gpkg_contents WHERE data_type = 'tiles'"
    ).map((r) => [r.table_name, { version: 1, built_at: r.last_change }]));
    const hasTable = db.run(
      "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tile_stamps'").length > 0;
    if (hasTable) {
      for (const r of db.run("SELECT layer, version, built_at FROM tile_stamps")) {
        if (layers[r.layer]) layers[r.layer] = { version: r.version, built_at: r.built_at };
      }
    }
    return layers;
  }

  // The server's map layers that this file lacks or holds an older build of:
  // [{layer, reason}] with reason "missing", "version" or "newer". `server` is manifest.map.layers.
  function tileUpdates(have, server) {
    const out = [];
    for (const [layer, built] of Object.entries(server || {})) {
      const mine = have[layer];
      if (!mine) { out.push({ layer, reason: "missing" }); continue; }
      if ((built.version || 1) > (mine.version || 0)) { out.push({ layer, reason: "version" }); continue; }
      const theirs = Date.parse(built.built_at);
      const held = Date.parse(mine.built_at);
      if (!Number.isNaN(theirs) && !(held >= theirs)) out.push({ layer, reason: "newer" });
    }
    return out;
  }

  // {section: digest}: what to tell the server we hold.
  const haveDigests = (stamps) => Object.fromEntries(
    Object.entries(stamps).map(([section, s]) => [section, s.digest]));

  // One row per section of the server's manifest: is the file's copy current?
  //   state "current"  - same fingerprint
  //   state "outdated" - the file holds an older version
  //   state "unknown"  - the file has no fingerprint (it predates them): treated as outdated
  function compare(stamps, manifest) {
    return Object.entries(manifest.sections).map(([section, server]) => {
      const local = stamps[section];
      return {
        section,
        label: server.label,
        state: !local ? "unknown" : local.digest === server.digest ? "current" : "outdated",
        localUpdatedAt: local ? local.updated_at : null,
        serverUpdatedAt: server.updated_at,
      };
    });
  }

  const needsUpdate = (rows) => rows.filter((r) => r.state !== "current");

  // ---- applying a delta -------------------------------------------------------------------

  const canyonIdOf = (db, title) => {
    const [row] = db.run("SELECT id FROM canyons WHERE ropewiki_title = ?", [title]);
    return row ? row.id : null;
  };

  const decodeBlob = (text) => (text === null || text === undefined
    ? null : Uint8Array.from(atob(text), (c) => c.charCodeAt(0)));

  // Names come from the server: only plain identifiers are ever put into SQL.
  function checkNames(table) {
    for (const name of [table.name, ...table.columns, ...Object.keys(table.fks),
      ...Object.values(table.fks)]) {
      if (!IDENTIFIER.test(name)) throw new Error(`Unexpected name in the update: ${name}`);
    }
  }

  // SQL that selects the rows of `table` that belong to the canyon (parameter: its local id).
  function scope(table, byName) {
    const [column, parent] = Object.entries(byName[table].fks)[0];
    if (parent === "canyons") return `${column} = ?`;
    return `${column} IN (SELECT id FROM ${parent} WHERE ${scope(parent, byName)})`;
  }

  function applySection(db, canyonId, name, section) {
    if (!IDENTIFIER.test(name)) throw new Error(`Unexpected section name: ${name}`);
    const byName = Object.fromEntries(section.tables.map((t) => [t.name, t]));
    section.tables.forEach(checkNames);
    // Children first, so nothing refers to a row that is already gone.
    for (const table of [...section.tables].reverse()) {
      if (table.mode === "replace") {
        db.exec(`DELETE FROM ${table.name} WHERE ${scope(table.name, byName)}`, [canyonId]);
      }
    }
    const newIds = {};
    let rows = 0;
    for (const table of section.tables) {
      const local = new Set(db.run(`SELECT name FROM pragma_table_info('${table.name}')`)
        .map((r) => r.name));
      const columns = table.columns.filter((c) => c !== "id" && local.has(c));
      const idAt = table.columns.indexOf("id");
      newIds[table.name] = new Map();
      for (const row of table.rows) {
        const value = (column) => {
          const raw = row[table.columns.indexOf(column)];
          if (column === table.geometry) return decodeBlob(raw);
          const parent = table.fks[column];
          if (!parent) return raw;
          if (parent === "canyons") return canyonId;
          return raw === null ? null : (newIds[parent] && newIds[parent].get(raw)) ?? null;
        };
        if (table.mode === "update") {
          const sets = columns.filter((c) => c !== "ropewiki_title");
          db.exec(`UPDATE ${table.name} SET ${sets.map((c) => `${c} = ?`).join(", ")} WHERE id = ?`,
            [...sets.map(value), canyonId]);
          newIds[table.name].set(row[idAt], canyonId);
        } else {
          const { lastInsertRowid } = db.exec(
            `INSERT INTO ${table.name} (${columns.join(", ")})
             VALUES (${columns.map(() => "?").join(", ")})`, columns.map(value));
          newIds[table.name].set(row[idAt], lastInsertRowid);
        }
        rows += 1;
      }
    }
    db.exec(
      `INSERT INTO section_stamps (canyon_id, section, digest, updated_at) VALUES (?, ?, ?, ?)
       ON CONFLICT(canyon_id, section) DO UPDATE SET
         digest = excluded.digest, updated_at = excluded.updated_at`,
      [canyonId, name, section.digest, section.updated_at]);
    return rows;
  }

  // Replace the canyon's rows in the sections of `delta` ({title, sections}) in one transaction,
  // creating whatever tables and columns this file lacks. Returns {applied, rows}; nothing is
  // changed (applied is empty) when the file has no such canyon.
  function applyDelta(db, delta, now = Date.now()) {
    const canyonId = canyonIdOf(db, delta.title);
    if (canyonId === null) return { applied: [], rows: 0 };
    const applied = [];
    let rows = 0;
    db.transaction(() => {
      write.ensureTables(db);
      for (const [name, section] of Object.entries(delta.sections)) {
        rows += applySection(db, canyonId, name, section);
        applied.push(name);
      }
      if (applied.includes("canyon") || applied.includes("geometry")) {
        write.refreshExtents(db, iso(now), GEOMETRY_TABLES);
      }
    });
    return { applied, rows };
  }

  // ---- updates kept on the device ---------------------------------------------------------
  // A downloaded file is not rewritten (it can be 100 MB). Each applied section is also kept as
  // a small patch, and patches are applied again whenever the file is opened.

  const patchId = (title, section) => `${title}\u0000${section}`;

  function patchesFromDelta(delta) {
    return Object.entries(delta.sections).map(([section, data]) => ({
      id: patchId(delta.title, section),
      title: delta.title,
      section,
      digest: data.digest,
      updated_at: data.updated_at,
      tables: data.tables,
    }));
  }

  // Apply the patches that are newer than what `db` holds. Returns {applied, obsolete}: ids of
  // patches applied, and of patches the file already contains or is newer than (safe to delete).
  function applyStoredPatches(db, patches, now = Date.now()) {
    const applied = [];
    const obsolete = [];
    const byTitle = new Map();
    for (const patch of patches) {
      if (!byTitle.has(patch.title)) byTitle.set(patch.title, []);
      byTitle.get(patch.title).push(patch);
    }
    for (const [title, list] of byTitle) {
      const canyonId = canyonIdOf(db, title);
      if (canyonId === null) continue; // a patch for a canyon in some other file
      const stamps = readStamps(db, canyonId);
      const sections = {};
      for (const patch of list) {
        const local = stamps[patch.section];
        if (local && (local.digest === patch.digest || local.updated_at >= patch.updated_at)) {
          obsolete.push(patch.id);
        } else {
          sections[patch.section] = { digest: patch.digest, updated_at: patch.updated_at,
            tables: patch.tables };
          applied.push(patch.id);
        }
      }
      if (Object.keys(sections).length) applyDelta(db, { title, sections }, now);
    }
    return { applied, obsolete };
  }

  const api = {
    readStamps, readTileStamps, tileUpdates, haveDigests, compare, needsUpdate, applyDelta, patchesFromDelta, applyStoredPatches,
    patchId,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.gpkgUpdate = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
