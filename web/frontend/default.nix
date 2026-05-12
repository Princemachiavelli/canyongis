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

  npmDepsHash = "sha256-QVdXlYOD3nwVbMmiUoueOsCk0HBnAiJ19yBXrdZ2R5s=";

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
