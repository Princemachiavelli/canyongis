{
  lib,
  buildNpmPackage,
  fetchFromGitHub,
  nodejs_22,
  # canyongis-geolibre-plugin derivation; when set, its dist is copied into
  # public/plugins/ before the build so GeoLibre's bundledPlugins() Vite plugin
  # bakes CanyonGIS in as an always-loaded bundled plugin (no manifest-URL
  # install step in the UI).
  canyongisPlugin ? null,
}:

buildNpmPackage {
  pname = "geolibre-web";
  version = "1.0.0";

  src = fetchFromGitHub {
    owner = "opengeos";
    repo = "GeoLibre";
    rev = "v1.0.0";
    hash = "sha256-TMpNHr0leDBY2LTv5F5OCzHxjGhsx6qVgX2dgWlBhJE=";
  };

  nodejs = nodejs_22;
  npmDepsHash = "sha256-3jqbP7mw5PJJtlvouHCQv0ZZQZ0Uoje7WWI48Bfgx0k=";

  # Root `npm run build` delegates to the geolibre-desktop workspace
  # (tsc -b && vite build); bundled-plugin discovery happens at build start.
  preBuild = lib.optionalString (canyongisPlugin != null) ''
    mkdir -p apps/geolibre-desktop/public/plugins/canyongis
    cp -R ${canyongisPlugin}/share/canyongis-geolibre-plugin/. \
      apps/geolibre-desktop/public/plugins/canyongis/
  '';

  installPhase = ''
    runHook preInstall
    mkdir -p "$out/share/geolibre-web"
    cp -R apps/geolibre-desktop/dist/. "$out/share/geolibre-web/"
    runHook postInstall
  '';

  meta = {
    description = "GeoLibre web build (static SPA) with the CanyonGIS plugin bundled";
    homepage = "https://github.com/opengeos/GeoLibre";
    license = lib.licenses.mit;
  };
}
