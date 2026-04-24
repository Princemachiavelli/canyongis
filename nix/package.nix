{
  lib,
  buildPythonPackage,
  albumentations,
  setuptools,
  folium,
  geojson,
  geopandas,
  numpy,
  pandas,
  pyogrio,
  pyproj,
  pysheds,
  rasterio,
  scipy,
  scikit-image,
  segmentation_models_pytorch,
  shapely,
  torch,
  tqdm,
}:

buildPythonPackage {
  pname = "canyongis";
  version = "0.1.0";
  pyproject = true;

  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../pyproject.toml
      ../src
    ];
  };

  build-system = [ setuptools ];

  dependencies = [
    albumentations
    folium
    geojson
    geopandas
    numpy
    pandas
    pyogrio
    pyproj
    pysheds
    rasterio
    scipy
    scikit-image
    segmentation_models_pytorch
    shapely
    torch
    tqdm
  ];

  doCheck = false;
  pythonImportsCheck = [ "canyongis" ];

  # pysheds decorates module-level functions with @njit(cache=True). Importing
  # pysheds.grid triggers numba's FunctionCache setup, which searches for a
  # writable locator and fails inside the Nix sandbox (source lives in the
  # read-only /nix/store and $HOME is /homeless-shelter). Point numba at a
  # writable directory so UserProvidedCacheLocator is used. pythonImportsCheck
  # runs as a postFixup hook, so this needs to be exported before fixupPhase.
  preFixup = ''
    export NUMBA_CACHE_DIR="$TMPDIR/numba-cache"
    mkdir -p "$NUMBA_CACHE_DIR"
  '';

  meta = {
    description = "Canyoneering route finder via GIS terrain analysis";
    mainProgram = "canyongis";
    license = lib.licenses.mit;
    platforms = lib.platforms.unix;
  };
}
