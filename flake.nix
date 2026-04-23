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
        "x86_64-darwin"
        "aarch64-darwin"
      ];

      imports = [
        inputs.treefmt-nix.flakeModule
        ./nix/treefmt.nix
      ];

      perSystem =
        { config, pkgs, ... }:
        let
          python = pkgs.python312;

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
              fiona
              folium
              pandas
              geojson
              pysheds
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
            ];
            shellHook = ''
              echo "canyongis dev shell"
              echo "  Run:           python -m canyongis <dem.tif> <outdir>"
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
