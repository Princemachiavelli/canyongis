import L from "leaflet";

// @geoman-io/leaflet-geoman-free is a plugin that registers itself onto
// window.L at import time. Leaflet's ESM build does not assign the global,
// so we do it here. This module must be imported before geoman.
(window as unknown as { L: typeof L }).L = L;
