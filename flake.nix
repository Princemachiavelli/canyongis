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
        { config, system, ... }:
        let
          pkgs = import inputs.nixpkgs {
            inherit system;
            config = {
              allowUnfree = true;
              allowUnfreePredicate = true;
              cudaSupport = (system == "x86_64-linux");
              cudaCapabilities = [ "8.6" ];
            };
          };

          # Override the python package set so rasterio and pyogrio share a single
          # python-matched gdal, avoiding two gdal-3.12.3 builds in the closure.
          python = pkgs.python313.override {
            self = python;
            packageOverrides = pyfinal: pyprev: {
              segmentation_models_pytorch = pyfinal.callPackage ./nix/segmentation-models-pytorch.nix {
                torch = pyfinal.torch;
                torchvision = pyfinal.torchvision;
              };
              #rasterio = pyprev.rasterio.override {
              #  gdal = pkgs.gdal.override { python3 = pyfinal.python; };
              #};
            };
          };

          pysheds = python.pkgs.callPackage ./nix/pysheds.nix { };

          canyongis-web-frontend = pkgs.callPackage ./web/frontend { };

          canyongis = python.pkgs.callPackage ./nix/package.nix {
            inherit pysheds;
            webFrontend = canyongis-web-frontend;
            torch = python.pkgs.torch;
            segmentation_models_pytorch = python.pkgs.segmentation_models_pytorch;
          };
        in
        {
	  legacyPackages = pkgs;
          packages = {
            default = canyongis;
            canyongis = canyongis;
            inherit canyongis-web-frontend;
            inherit pysheds;
            segmentation_models_pytorch = python.pkgs.segmentation_models_pytorch;
          };

          apps.default = {
            type = "app";
            program = "${canyongis}/bin/canyongis";
          };

          devShells.default = pkgs.mkShell {
            inputsFrom = [
              canyongis
              config.treefmt.build.devShell
            ];
            packages = [
              pkgs.grass
              pkgs.gdal
              pkgs.nodejs
              python.pkgs.pytest
            ];
            shellHook = ''
              # Make the in-tree source importable without a `pip install -e .`
              # (the dev shell only provides deps, not the canyongis package itself).
              export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
              echo "canyongis dev shell"
              echo "  Run:           python -m canyongis <dem.tif> <outdir>"
              echo "  Validate:      python -m canyongis.validate --kml <kml> --out-dir <dir>"
              echo "  Web:           python -m canyongis web"
              echo "  Format:        nix fmt"
              echo "  Reproject DEM: gdalwarp -t_srs EPSG:32612 in.tif utm.tif"
            '';
          };

          checks = {
            inherit canyongis;
            inherit canyongis-web-frontend;
            formatting = config.treefmt.build.check inputs.self;
          };
        };
    };
}
