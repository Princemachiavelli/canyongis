{
  lib,
  buildNpmPackage,
}:

buildNpmPackage {
  pname = "canyongis-geolibre-plugin";
  version = "0.1.0";

  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./package.json
      ./package-lock.json
      ./tsconfig.json
      ./plugin.json
      ./style.css
      ./src
    ];
  };

  npmDepsHash = "sha256-yI9ojnFKq6TjvUS2MXU+Qr4zOwcGTpEuye5p/t2ITrI=";

  installPhase = ''
    runHook preInstall
    mkdir -p "$out/share/canyongis-geolibre-plugin"
    cp -R dist/. "$out/share/canyongis-geolibre-plugin/"
    runHook postInstall
  '';

  meta = {
    description = "CanyonGIS external plugin for GeoLibre (manifest + ES module + CSS)";
    license = lib.licenses.mit;
  };
}
