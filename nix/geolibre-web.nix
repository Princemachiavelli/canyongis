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
  version = "1.7.0";

  src = fetchFromGitHub {
    owner = "opengeos";
    repo = "GeoLibre";
    rev = "v1.7.0";
    hash = "sha256-MvKy2CaPxCYVGDKzCz6f2efTXqNtpgqdw22FNJAfZfI=";
  };

  nodejs = nodejs_22;
  npmDepsHash = "sha256-l7ToPf+2TWr1qzOIRdtV6l2xuI58xH88JQCRWlvG52Y=";

  # The fetched npm cache lands in the read-only Nix store owned by the build
  # user; newer npm refuses to use a cache it cannot write to ("root-owned
  # files"). Copy it into a writable dir before install.
  makeCacheWritable = true;

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
