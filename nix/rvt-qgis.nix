{
  lib,
  stdenvNoCC,
  fetchFromGitHub,
}:

stdenvNoCC.mkDerivation rec {
  pname = "rvt-qgis";
  version = "1.0.0";

  src = fetchFromGitHub {
    owner = "EarthObservation";
    repo = "rvt-qgis";
    rev = "160c3b7aca3c9cb6a5158c377e7b7bf470575b6c";
    hash = "sha256-KvBrtSU4FpQGcSb189VcTCWITZgSAasXbKtFuMLUf4o=";
  };

  installPhase = ''
    runHook preInstall

    pluginDir="$out/share/qgis/python/plugins/rvt-qgis"
    mkdir -p "$pluginDir"
    cp -R . "$pluginDir"/

    runHook postInstall
  '';

  meta = {
    description = "Relief Visualization Toolbox plugin for QGIS";
    homepage = "https://github.com/EarthObservation/rvt-qgis";
    license = lib.licenses.asl20;
  };
}
