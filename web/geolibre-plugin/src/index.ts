// CanyonGIS GeoLibre plugin entry. The GeoLibre loader fetches this bundle as
// text, imports it from a blob URL, and expects the plugin object as the
// default export with id/name/version matching plugin.json exactly.

import { CanyonGisControl, loadSettings } from "./panel";
import type { PluginSettings } from "./panel";
import type { ControlPosition, GeoLibreAppAPI, GeoLibrePlugin } from "./geolibre";

let control: CanyonGisControl | undefined;
let position: ControlPosition = "top-right";
const settings: PluginSettings = loadSettings();

const plugin: GeoLibrePlugin = {
  id: "canyongis",
  name: "CanyonGIS",
  version: "0.1.0",
  urlParameterNames: ["canyongis-server", "canyongis-project"],

  activate(app: GeoLibreAppAPI) {
    if (control) return;
    control = new CanyonGisControl(app, settings);
    app.addMapControl(control, position);
  },

  deactivate(app: GeoLibreAppAPI) {
    if (!control) return;
    app.removeMapControl(control);
    control = undefined;
  },

  handleUrlParameters(_app: GeoLibreAppAPI, params: URLSearchParams) {
    const server = params.get("canyongis-server");
    const project = params.get("canyongis-project");
    if (!server && !project) return;
    if (server) settings.serverUrl = server.replace(/\/+$/, "");
    if (project) settings.projectId = project;
    control?.applySettings(settings);
  },

  getMapControlPosition() {
    return position;
  },

  setMapControlPosition(app: GeoLibreAppAPI, next: ControlPosition) {
    position = next;
    if (control) {
      app.removeMapControl(control);
      app.addMapControl(control, position);
    }
    return true;
  },

  getProjectState() {
    return {
      serverUrl: settings.serverUrl,
      projectId: settings.projectId,
      position,
    };
  },

  applyProjectState(_app: GeoLibreAppAPI, state: unknown) {
    if (!state || typeof state !== "object") return false;
    const s = state as {
      serverUrl?: unknown;
      projectId?: unknown;
      position?: unknown;
      autoLoad?: unknown;
    };
    if (typeof s.serverUrl === "string" && s.serverUrl) {
      settings.serverUrl = s.serverUrl.replace(/\/+$/, "");
    }
    if (typeof s.projectId === "string" && s.projectId) settings.projectId = s.projectId;
    // One-shot hydration flag set by server-exported projects. Intentionally
    // absent from getProjectState so a re-saved project doesn't re-hydrate.
    if (s.autoLoad === true) settings.autoLoad = true;
    if (
      s.position === "top-left" ||
      s.position === "top-right" ||
      s.position === "bottom-left" ||
      s.position === "bottom-right"
    ) {
      position = s.position;
    }
    control?.applySettings(settings);
    return true;
  },
};

export default plugin;
