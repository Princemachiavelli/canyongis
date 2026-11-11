// Small inline SVG pictures for the forecast on the Companion screen: sun, clouds, rain, storm,
// snow, fog, and hot / cold thermometers. Static markup written here (no user text), so it is
// safe to set as innerHTML. Browser only (the markup strings also work in Node).
(function (root) {
  "use strict";

  const SUN = "#f5a623";
  const CLOUD = "#9aa5b1";
  const RAIN = "#3b82f6";
  const BOLT = "#f5c518";
  const ICE = "#7dd3fc";
  const CLOUD_PATH = "M18 10h-1.26A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z";

  const rays = (cx, cy, inner, outer, stroke) => [0, 45, 90, 135, 180, 225, 270, 315].map((deg) => {
    const a = (deg * Math.PI) / 180;
    const p = (r) => `${(cx + r * Math.cos(a)).toFixed(2)},${(cy + r * Math.sin(a)).toFixed(2)}`;
    return `<line x1="${p(inner).split(",")[0]}" y1="${p(inner).split(",")[1]}" `
      + `x2="${p(outer).split(",")[0]}" y2="${p(outer).split(",")[1]}" stroke="${SUN}" `
      + `stroke-width="${stroke}" stroke-linecap="round"/>`;
  }).join("");

  const cloud = (transform = "", fill = CLOUD) =>
    `<path d="${CLOUD_PATH}" fill="${fill}" transform="${transform}"/>`;
  const dropLine = (x, y) =>
    `<line x1="${x}" y1="${y}" x2="${x - 1}" y2="${y + 3}" stroke="${RAIN}" stroke-width="1.8" `
    + 'stroke-linecap="round"/>';

  // Each picture is drawn on a 24 x 24 grid.
  const BODIES = {
    sunny: `<circle cx="12" cy="12" r="4.6" fill="${SUN}"/>${rays(12, 12, 7.4, 10, 1.8)}`,
    partly: `<circle cx="8" cy="8" r="3.4" fill="${SUN}"/>${rays(8, 8, 5.4, 7.2, 1.5)}`
      + cloud("translate(5.5 6.5) scale(.76)"),
    cloudy: cloud("translate(0 1)"),
    rain: cloud("translate(1.2 -1.5) scale(.86)")
      + dropLine(8, 17.5) + dropLine(12, 17.5) + dropLine(16, 17.5),
    storm: cloud("translate(1.2 -1.5) scale(.86)")
      + `<polygon points="13.2,14 9,20.4 12,20.4 10.6,24 16,17.6 12.8,17.6 14.4,14" fill="${BOLT}"/>`,
    snow: cloud("translate(1.2 -1.5) scale(.86)")
      + [8, 12, 16].map((x, i) => `<circle cx="${x}" cy="${18.6 + (i % 2) * 2.2}" r="1.3" fill="${ICE}"/>`).join(""),
    fog: cloud("translate(1.2 -2) scale(.86)", "#b7bfc8")
      + [17.5, 20, 22.5].map((y) => `<line x1="5" y1="${y}" x2="19" y2="${y}" stroke="${CLOUD}" `
        + 'stroke-width="1.6" stroke-linecap="round"/>').join(""),
    hot: '<rect x="9.5" y="2.5" width="5" height="13" rx="2.5" fill="none" stroke="#6b7280" stroke-width="1.4"/>'
      + '<rect x="11" y="5" width="2" height="11" rx="1" fill="#dc2626"/>'
      + '<circle cx="12" cy="18" r="4" fill="#dc2626"/>',
    cold: '<rect x="9.5" y="2.5" width="5" height="13" rx="2.5" fill="none" stroke="#6b7280" stroke-width="1.4"/>'
      + '<rect x="11" y="11" width="2" height="5" rx="1" fill="#2563eb"/>'
      + '<circle cx="12" cy="18" r="4" fill="#2563eb"/>',
  };

  const NAMES = Object.keys(BODIES);

  function svg(name) {
    if (!BODIES[name]) throw new Error(`No weather picture called ${name}`);
    return `<svg viewBox="0 0 24 24" width="100%" height="100%" aria-hidden="true" focusable="false">`
      + `${BODIES[name]}</svg>`;
  }

  // A <span class="wx-icon"> holding the picture, named for screen readers and as a tooltip.
  function element(name, label, doc = root.document) {
    const span = doc.createElement("span");
    span.className = `wx-icon wx-${name}`;
    span.setAttribute("role", "img");
    span.setAttribute("aria-label", label);
    span.title = label;
    span.innerHTML = svg(name);
    return span;
  }

  const api = { NAMES, svg, element };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.weatherIcons = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
