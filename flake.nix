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
          rvt-qgis = pkgs.callPackage ./nix/rvt-qgis.nix { };
          qgisPluginPath = pkgs.linkFarm "qgis-python-plugins" [
            {
              name = "rvt-qgis";
              path = "${rvt-qgis}/share/qgis/python/plugins/rvt-qgis";
            }
          ];
          qgis-base = pkgs.qgis.override {
            withGrass = true;
            extraPythonPackages = ps: [
              ps.matplotlib
              ps.scipy
            ];
          };
          qgis = pkgs.symlinkJoin {
            inherit (qgis-base) version;
            pname = "qgis";
            paths = [ qgis-base ];
            nativeBuildInputs = [ pkgs.makeWrapper ];
            postBuild = ''
              for program in $out/bin/*; do
                if [[ -f "$program" || -L "$program" ]]; then
                  wrapProgram "$program" \
                    --set QGIS_PLUGINPATH ${qgisPluginPath}
                fi
              done
            ''
            + pkgs.lib.optionalString pkgs.stdenv.hostPlatform.isDarwin ''
              for program in $out/Applications/QGIS.app/Contents/MacOS/qgis \
                             $out/Applications/QGIS.app/Contents/MacOS/qgis_process; do
                if [[ -e "$program" ]]; then
                  wrapProgram "$program" \
                    --set QGIS_PLUGINPATH ${qgisPluginPath}
                fi
              done
            '';
            passthru = qgis-base.passthru;
            meta = qgis-base.meta;
          };
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

          # Console scripts from pyproject.toml, run against the in-tree source (PYTHONPATH is set
          # by the dev shell hook) so they are available without installing the package.
          devScripts = pkgs.lib.mapAttrsToList (
            name: entry:
            let
              parts = pkgs.lib.splitString ":" entry;
            in
            pkgs.writeShellScriptBin name ''
              exec python -c 'import sys; from ${builtins.elemAt parts 0} import ${builtins.elemAt parts 1} as f; sys.argv[0] = "${name}"; sys.exit(f())' "$@"
            ''
          ) (builtins.fromTOML (builtins.readFile ./pyproject.toml)).project.scripts;

          # Serve web/canyon-viewer over HTTPS with a Tailscale certificate (the Companion view
          # needs HTTPS for GPS). The host name is this machine's current Tailscale name, so
          # nothing needs editing when it changes. The tailscale CLI comes from the system
          # (services.tailscale) so it matches the running daemon.
          canyon-viewer-serve = pkgs.writeShellApplication {
            name = "canyon-viewer-serve";
            runtimeInputs = [
              pkgs.jq
              # canyongis is importable so the server can run the canyon build API.
              (python.withPackages (_: [ canyongis ]))
            ];
            text = ''
              usage() {
                echo "usage: canyon-viewer-serve [--port N] [--gpkg-dir DIR] [--build-dir DIR]" >&2
                echo "                           [--no-build-api] [--no-tiles]" >&2
                echo "  Serves the viewer at https://<tailscale-host>:N (default 8443) and every" >&2
                echo "  *.gpkg in DIR (default: current directory) at /gpkg/<name>." >&2
                echo "  Also serves the canyon search/build API (needs PostgreSQL with PostGIS," >&2
                echo "  CANYONGIS_PG_* as for the web app); --build-dir holds its files (default" >&2
                echo "  ~/.local/state/canyongis/gpkg-builds). --no-tiles skips the offline map" >&2
                echo "  (no multi-GB downloads)." >&2
                exit 2
              }
              port=8443
              gpkg_dir=$PWD
              build_dir=''${XDG_STATE_HOME:-$HOME/.local/state}/canyongis/gpkg-builds
              build_api=1
              extra=()
              while [ $# -gt 0 ]; do
                case "$1" in
                  --port) [ $# -ge 2 ] || usage; port=$2; shift 2 ;;
                  --gpkg-dir) [ $# -ge 2 ] || usage; gpkg_dir=$2; shift 2 ;;
                  --build-dir) [ $# -ge 2 ] || usage; build_dir=$2; shift 2 ;;
                  --no-build-api) build_api=0; shift ;;
                  --no-tiles) extra+=(--no-tiles); shift ;;
                  *) usage ;;
                esac
              done
              if [ "$build_api" = 1 ]; then
                mkdir -p "$build_dir"
                extra+=(--build-dir "$build_dir")
              fi

              if ! command -v tailscale >/dev/null; then
                echo "tailscale is not on PATH (enable services.tailscale)" >&2
                exit 1
              fi
              host=$(tailscale status --json | jq -r '.Self.DNSName | rtrimstr(".")')
              if [ -z "$host" ] || [ "$host" = null ]; then
                echo "Tailscale has no host name; is it running, with MagicDNS enabled?" >&2
                exit 1
              fi

              certs=''${XDG_STATE_HOME:-$HOME/.local/state}/canyongis/tailscale-certs
              mkdir -p "$certs"
              # Renews the certificate when it is close to expiry. Needs HTTPS enabled for the
              # tailnet, and either root or `sudo tailscale set --operator=$USER` once.
              if ! tailscale cert --cert-file "$certs/$host.crt" --key-file "$certs/$host.key" "$host"; then
                echo "tailscale cert failed for $host (see the message above)" >&2
                exit 1
              fi

              viewer=''${VIEWER_DIR:-$PWD/web/canyon-viewer}
              [ -f "$viewer/index.html" ] || viewer=${./web/canyon-viewer}
              echo "Open https://$host:$port/?gpkg=gpkg/<file>.gpkg"
              exec python3 ${./web/canyon-viewer/serve.py} \
                --cert "$certs/$host.crt" --key "$certs/$host.key" \
                --port "$port" --directory "$viewer" --gpkg-dir "$gpkg_dir" "''${extra[@]}"
            '';
          };
        in
        {
          legacyPackages = pkgs;
          packages = {
            default = canyongis;
            canyongis = canyongis;
            inherit canyongis-web-frontend;
            inherit canyon-viewer-serve;
            inherit pysheds;
            inherit rvt-py;
            inherit rvt-qgis;
            inherit qgisPluginPath;
            qgis = qgis;
            segmentation_models_pytorch = python.pkgs.segmentation_models_pytorch;
          };

          apps.default = {
            type = "app";
            program = "${canyongis}/bin/canyongis";
          };

          apps.canyon-viewer-serve = {
            type = "app";
            program = "${canyon-viewer-serve}/bin/canyon-viewer-serve";
          };

          devShells.default = pkgs.mkShell {
            inputsFrom = [
              canyongis
              config.treefmt.build.devShell
            ];
            packages =
              devScripts
              ++ [ canyon-viewer-serve ]
              ++ (with pkgs; [
                grass
                gdal
                pmtiles
                #qgis # very large and slow to build.
                nodejs
                postgresqlWithPostGIS
                python.pkgs.pytest
                python.pkgs.pytest-cov
                #python.pkgs.napari
              ]);
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
              # SUPERUSER: the canyon schema migration runs CREATE EXTENSION postgis.
              postgresqlTestUserOptions = "LOGIN CREATEDB SUPERUSER";

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
