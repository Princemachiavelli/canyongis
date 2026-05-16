{
  description = "Canyoneering route finder — GIS terrain analysis in Python";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    nixpkgs-fastmcp.url = "github:NixOS/nixpkgs/da5ad661ba4e5ef59ba743f0d112cbc30e474f32";
    flake-parts.url = "github:hercules-ci/flake-parts";
    flake-parts.inputs.nixpkgs-lib.follows = "nixpkgs";
    treefmt-nix.url = "github:numtide/treefmt-nix";
    treefmt-nix.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs =
    inputs@{ flake-parts, nixpkgs-fastmcp, ... }:
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
            overlays = [
              # Keep GRASS pinned to 8.5.0 rather than depending on the nixpkgs
              # channel's GRASS version for terrain-analysis behavior.
              (_final: prev: {
                grass = prev.callPackage ./nix/grass.nix { };
              })
            ];
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
            };
          };

          pysheds = python.pkgs.callPackage ./nix/pysheds.nix { };
          rvt-py = python.pkgs.callPackage ./nix/rvt-py.nix { };
          postgresqlWithPostGIS = pkgs.postgresql_15.withPackages (p: [ p.postgis ]);

          canyongis-web-frontend = pkgs.callPackage ./web/frontend { };

          canyongis = python.pkgs.callPackage ./nix/package.nix {
            inherit pysheds;
            inherit rvt-py;
            webFrontend = canyongis-web-frontend;
            gdal = pkgs.gdal;
            pmtiles = pkgs.pmtiles;
            psycopg = python.pkgs.psycopg;
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
            inherit rvt-py;
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
              pkgs.pmtiles
              pkgs.nodejs
              postgresqlWithPostGIS
              python.pkgs.pytest
              python.pkgs.pytest-cov
            ];
            shellHook = ''
              # Make the in-tree source importable without a `pip install -e .`
              # (the dev shell only provides deps, not the canyongis package itself).
              export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
              export PGDATA="$PWD/.pg"
              export PGHOST="$PGDATA"
              export PGPORT="5433"
              if [ ! -d "$PGDATA" ]; then
                initdb -D "$PGDATA" --no-instructions --auth=trust
              fi
              if ! pg_ctl -D "$PGDATA" status >/dev/null 2>&1; then
                pg_ctl -D "$PGDATA" -o "-p $PGPORT -k $PGHOST" -l "$PGDATA/postgres.log" start >/dev/null
              fi
              echo "canyongis dev shell"
              echo "  Run:           python -m canyongis <dem.tif> <outdir>"
              echo "  Validate:      python -m canyongis.validate --kml <kml> --out-dir <dir>"
              echo "  Web:           python -m canyongis web"
              echo "  Format:        nix fmt"
              echo "  Reproject DEM: gdalwarp -t_srs EPSG:32612 in.tif utm.tif"
              echo "  PostgreSQL:    psql -h $PGHOST -p $PGPORT -d postgres"
              echo "  PostGIS:       CREATE EXTENSION IF NOT EXISTS postgis;"
            '';
          };

          checks = {
            inherit canyongis;
            inherit canyongis-web-frontend;
            coverage = pkgs.stdenv.mkDerivation {
              pname = "canyongis-coverage";
              version = "0.1.0";
              src = inputs.self;

              nativeCheckInputs = [
                (python.withPackages (_: [
                  canyongis
                  python.pkgs.pytest
                  python.pkgs.pytest-cov
                ]))
                pkgs.gdal
                pkgs.grass
                pkgs.pmtiles
                postgresqlWithPostGIS
                (pkgs.postgresqlTestHook.overrideAttrs (old: {
                  meta = (old.meta or { }) // {
                    badPlatforms = [ ];
                  };
                }))
              ];

              dontBuild = true;
              doCheck = true;
              postgresqlTestUserOptions = "LOGIN CREATEDB";

              checkPhase = ''
                runHook preCheck
                trap 'runHook postCheck' EXIT

                export HOME="$TMPDIR"
                export NUMBA_CACHE_DIR="$TMPDIR/numba-cache"
                mkdir -p "$NUMBA_CACHE_DIR"
                export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
                export CANYONGIS_TEST_PG_HOST="$PGHOST"
                export CANYONGIS_TEST_PG_PORT="''${PGPORT:-5432}"
                export CANYONGIS_TEST_PG_ADMIN_DB="$PGDATABASE"
                export CANYONGIS_TEST_PG_USER="$PGUSER"

                pytest --cov-fail-under=50

                runHook postCheck
                trap - EXIT
              '';

              installPhase = ''
		cp coverage.xml $out
              '';
            };
            formatting = config.treefmt.build.check inputs.self;
          };
        };
    };
}
