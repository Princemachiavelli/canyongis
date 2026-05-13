{
  lib,
  buildPythonPackage,
  fetchFromGitHub,
  gdal,
  geopandas,
  jupyter,
  matplotlib,
  numpy,
  pandas,
  rasterio,
  scipy,
  setuptools,
}:

buildPythonPackage rec {
  pname = "rvt-py";
  version = "2.2.3";
  pyproject = true;

  src = fetchFromGitHub {
    owner = "EarthObservation";
    repo = "RVT_py";
    rev = "d26f1fbd987a148154d547bba9372f271bf23527";
    hash = "sha256-3I8Ah1PB4tYu+H7BpfdlCP6kulpA3vxys0DkLLQTM84=";
  };

  build-system = [ setuptools ];

  dependencies = [
    gdal
    geopandas
    jupyter
    matplotlib
    numpy
    pandas
    rasterio
    scipy
  ];

  doCheck = false;
  pythonImportsCheck = [
    "rvt"
    "rvt.blend_func"
    "rvt.vis"
  ];

  meta = {
    description = "Relief Visualization Toolbox Python library";
    homepage = "https://github.com/EarthObservation/RVT_py";
    license = lib.licenses.asl20;
  };
}
