{
  lib,
  buildPythonPackage,
  fetchPypi,
  setuptools,
  affine,
  geojson,
  looseversion,
  numba,
  numpy,
  pandas,
  pyproj,
  rasterio,
  scikit-image,
  scipy,
}:

buildPythonPackage rec {
  pname = "pysheds";
  version = "0.5";
  pyproject = true;

  src = fetchPypi {
    inherit pname version;
    hash = "sha256-tW+0eO0+QZS20KADPGbc3F2vr9kNvAWtCW/7RQ5qgAU=";
  };

  build-system = [ setuptools ];

  dependencies = [
    affine
    geojson
    looseversion
    numba
    numpy
    pandas
    pyproj
    rasterio
    scikit-image
    scipy
  ];

  doCheck = false;
  pythonImportsCheck = [ "pysheds" ];

  meta = {
    description = "Watershed delineation in Python";
    homepage = "https://github.com/mdbartos/pysheds";
    license = lib.licenses.gpl3Only;
  };
}
