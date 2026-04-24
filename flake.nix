{
  description = "Canyoneering route finder — GIS terrain analysis in Python";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-parts.url = "github:hercules-ci/flake-parts";
    flake-parts.inputs.nixpkgs-lib.follows = "nixpkgs";
    treefmt-nix.url = "github:numtide/treefmt-nix";
    treefmt-nix.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs =
    inputs@{ flake-parts, ... }:
    flake-parts.lib.mkFlake { inherit inputs; } {
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
      ];

      imports = [
        inputs.treefmt-nix.flakeModule
        ./nix/treefmt.nix
      ];

      perSystem =
        { config, pkgs, ... }:
        let
          # Override the python package set so rasterio and pyogrio share a single
          # python-matched gdal, avoiding two gdal-3.12.3 builds in the closure.
          python = pkgs.python313.override {
            self = python;
            packageOverrides = pyfinal: pyprev: {
             #rasterio = pyprev.rasterio.override {
             #  gdal = pkgs.gdal.override { python3 = pyfinal.python; };
             #};
            };
          };

          pysheds = python.pkgs.callPackage ./nix/pysheds.nix { };

          canyongis = python.pkgs.callPackage ./nix/package.nix {
            inherit pysheds;
          };

          pythonEnv = python.withPackages (
            ps: with ps; [
              numpy
              scipy
              rasterio
              geopandas
              shapely
              pyproj
              pyogrio
              folium
              pandas
              geojson
              pysheds
              pytest
            ]
          );
        in
        {
          packages = {
            default = canyongis;
            canyongis = canyongis;
            inherit pysheds;
          };

          apps.default = {
            type = "app";
            program = "${canyongis}/bin/canyongis";
          };

          devShells.default = pkgs.mkShell {
            inputsFrom = [ config.treefmt.build.devShell ];
            packages = [
              pythonEnv
	      pkgs.gdal
              #(pkgs.gdalMinimal.overrideAttrs { doCheck = false; })
            ];
            shellHook = ''
              # Make the in-tree source importable without a `pip install -e .`
              # (the dev shell only provides deps, not the canyongis package itself).
              export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
              echo "canyongis dev shell"
              echo "  Run:           python -m canyongis <dem.tif> <outdir>"
              echo "  Validate:      python -m canyongis.validate --kml <kml> --out-dir <dir>"
              echo "  Format:        nix fmt"
              echo "  Reproject DEM: gdalwarp -t_srs EPSG:32612 in.tif utm.tif"
            '';
          };

          checks = {
            inherit canyongis;
            formatting = config.treefmt.build.check inputs.self;
          };
        };
    };
}
