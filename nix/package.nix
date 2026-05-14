{
  lib,
  buildPythonPackage,
  albumentations,
  alembic,
  bcrypt,
  fastapi,
  fastmcp,
  gdal,
  grass,
  setuptools,
  folium,
  geojson,
  geopandas,
  itsdangerous,
  numpy,
  pandas,
  pillow,
  python-multipart,
  pyogrio,
  pyproj,
  pysheds,
  psycopg,
  pmtiles,
  rasterio,
  rvt-py,
  scipy,
  scikit-image,
  segmentation_models_pytorch,
  shapely,
  torch,
  tqdm,
  uvicorn,
  webFrontend ? null,
  zarr,
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
    alembic
    bcrypt
    fastapi
    fastmcp
    folium
    geojson
    geopandas
    itsdangerous
    numpy
    pandas
    pillow
    python-multipart
    pyogrio
    pyproj
    pysheds
    psycopg
    rasterio
    rvt-py
    scipy
    scikit-image
    segmentation_models_pytorch
    shapely
    torch
    tqdm
    uvicorn
    zarr
  ];

  makeWrapperArgs = [
    "--set-default"
    "NUMBA_CACHE_DIR"
    "/tmp/canyongis-numba-cache"
    "--prefix"
    "PATH"
    ":"
    (lib.makeBinPath [
      gdal
      grass
      pmtiles
    ])
  ]
  ++ lib.optionals (webFrontend != null) [
    "--set-default"
    "CANYONGIS_FRONTEND_DIST"
    "${webFrontend}/share/canyongis-web"
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
