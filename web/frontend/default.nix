{
  lib,
  buildNpmPackage,
}:

buildNpmPackage {
  pname = "canyongis-web-frontend";
  version = "0.1.0";

  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./index.html
      ./package.json
      ./package-lock.json
      ./tsconfig.json
      ./vite.config.ts
      ./src
    ];
  };

  npmDepsHash = "sha256-H/a8s4W2eu6zHffRCZaZd8RV5gAjLWpo7GSZesBGx2k=";

  installPhase = ''
    runHook preInstall
    mkdir -p "$out/share/canyongis-web"
    cp -R dist/. "$out/share/canyongis-web/"
    runHook postInstall
  '';

  meta = {
    description = "React/Leaflet frontend for canyongis";
    license = lib.licenses.mit;
  };
}
