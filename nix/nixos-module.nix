{
  config,
  lib,
  pkgs,
  # The flake passes the per-system package set (canyongis, the two frontend
  # builds, and the GeoLibre plugin) via `specialArgs.canyongisPackages`. When
  # the module is imported standalone, set the *.package / frontends.*.dist
  # options explicitly instead.
  canyongisPackages ? null,
  ...
}:
let
  cfg = config.services.canyongis;
  inherit (lib)
    mkOption
    mkEnableOption
    mkIf
    mkMerge
    types
    optional
    optionalAttrs
    ;

  pkgOr =
    name:
    if canyongisPackages != null then
      canyongisPackages.${name}
    else
      throw (
        "services.canyongis: no canyongisPackages provided via specialArgs; "
        + "set the package / frontends.*.dist options explicitly."
      );

  stateRoot = "/var/lib/canyongis";
  outputRoot = "${stateRoot}/web";
  secretEnv = "${stateRoot}/secret.env";
  cacheDir = "/var/cache/canyongis";

  # react frontend lives at the apex (empty subdomain); geolibre at a subdomain.
  fqdn = sub: if sub == "" || sub == null then cfg.domain else "${sub}.${cfg.domain}";

  enabledFrontends = lib.filterAttrs (_: fe: fe.enable) cfg.frontends;

  # Credentialed CORS needs explicit origins; seed both served vhosts so the
  # cross-subdomain export/open flow works, plus any user-supplied extras.
  autoCorsOrigins = lib.mapAttrsToList (_: fe: "https://${fqdn fe.subdomain}") enabledFrontends;
  corsOrigins = lib.unique (autoCorsOrigins ++ cfg.corsOrigins);

  postgisDeps = optional cfg.database.createLocally "postgresql.service";

  mkWebService = key: fe: {
    description = "canyongis web server (${key} frontend)";
    wantedBy = [ "multi-user.target" ];
    after = [
      "network.target"
      "canyongis-setup.service"
    ]
    ++ postgisDeps
    ++ optional cfg.database.createLocally "canyongis-postgis-setup.service";
    requires = [ "canyongis-setup.service" ] ++ postgisDeps;
    environment = {
      CANYONGIS_FRONTEND_DIST = fe.dist;
      NUMBA_CACHE_DIR = cacheDir;
      CANYONGIS_PG_HOST = cfg.database.host;
      CANYONGIS_PG_PORT = toString cfg.database.port;
      CANYONGIS_PG_DB = cfg.database.name;
      CANYONGIS_PG_USER = cfg.database.user;
      CANYONGIS_CORS_ORIGINS = lib.concatStringsSep "," corsOrigins;
      CANYONGIS_COOKIE_SAMESITE = cfg.cookieSameSite;
    }
    // optionalAttrs (fe.pluginDist != null) {
      CANYONGIS_GEOLIBRE_PLUGIN_DIST = fe.pluginDist;
    }
    // optionalAttrs cfg.singleUser {
      CANYONGIS_SINGLE_USER = "1";
    }
    // cfg.extraEnvironment;
    serviceConfig = {
      ExecStart = lib.concatStringsSep " " [
        "${cfg.package}/bin/canyongis-web"
        "--host 127.0.0.1"
        "--port ${toString fe.port}"
        "--output-root ${outputRoot}"
        "--frontend-dist ${fe.dist}"
      ];
      User = cfg.user;
      Group = cfg.group;
      EnvironmentFile = secretEnv;
      # Shared backend: every instance writes to the one StateDirectory and
      # reads the one session secret, so projects/jobs are visible from both UIs.
      StateDirectory = "canyongis";
      StateDirectoryMode = "0750";
      CacheDirectory = "canyongis";
      Restart = "on-failure";
      RestartSec = 5;
      # Hardening.
      NoNewPrivileges = true;
      ProtectSystem = "strict";
      ProtectHome = true;
      PrivateTmp = true;
      ProtectKernelTunables = true;
      ProtectKernelModules = true;
      ProtectControlGroups = true;
      RestrictSUIDSGID = true;
      RestrictRealtime = true;
      LockPersonality = true;
    };
  };
in
{
  options.services.canyongis = {
    enable = mkEnableOption "the canyongis geospatial web service (React/Leaflet + GeoLibre frontends)";

    package = mkOption {
      type = types.package;
      default = pkgOr "canyongis";
      defaultText = lib.literalExpression "canyongisPackages.canyongis";
      description = "The canyongis package providing the `canyongis-web` server.";
    };

    user = mkOption {
      type = types.str;
      default = "canyongis";
      description = "System user the services run as.";
    };

    group = mkOption {
      type = types.str;
      default = "canyongis";
      description = "Primary group for the service user.";
    };

    domain = mkOption {
      type = types.str;
      example = "canyongis.example.com";
      description = ''
        Base domain. The React/Leaflet frontend is served at this name and the
        GeoLibre frontend at `geolibre.<domain>` (see `frontends.*.subdomain`).
      '';
    };

    openFirewall = mkOption {
      type = types.bool;
      default = true;
      description = "Open TCP 80/443 for Caddy when the reverse proxy is enabled.";
    };

    singleUser = mkOption {
      type = types.bool;
      default = false;
      description = "Run in single-user mode (`CANYONGIS_SINGLE_USER=1`, no login).";
    };

    cookieSameSite = mkOption {
      type = types.enum [
        "lax"
        "strict"
        "none"
      ];
      default = "lax";
      description = ''
        Session cookie SameSite policy. Each frontend talks to its own
        same-origin API, so `lax` is fine. Use `none` (TLS-only) if you need the
        cross-subdomain "export from one UI, open in the other" flow.
      '';
    };

    corsOrigins = mkOption {
      type = types.listOf types.str;
      default = [ ];
      description = ''
        Extra credentialed CORS origins. The served vhost URLs are added
        automatically; list any additional origins here.
      '';
    };

    extraEnvironment = mkOption {
      type = types.attrsOf types.str;
      default = { };
      description = "Extra environment variables for the web services.";
    };

    acme.email = mkOption {
      type = types.nullOr types.str;
      default = null;
      example = "admin@example.com";
      description = "Let's Encrypt contact email used by Caddy for ACME.";
    };

    caddy.enable = mkOption {
      type = types.bool;
      default = true;
      description = "Run Caddy as a TLS-terminating reverse proxy (Let's Encrypt).";
    };

    database = {
      createLocally = mkOption {
        type = types.bool;
        default = true;
        description = "Provision a local PostgreSQL+PostGIS instance for canyongis.";
      };
      host = mkOption {
        type = types.str;
        default = "/run/postgresql";
        description = "PostgreSQL host. A path uses the local unix socket (peer auth).";
      };
      port = mkOption {
        type = types.port;
        default = 5432;
        description = "PostgreSQL port.";
      };
      name = mkOption {
        type = types.str;
        default = "canyongis";
        description = "Database name.";
      };
      user = mkOption {
        type = types.str;
        default = "canyongis";
        description = "Database role (matches the system user for peer auth).";
      };
    };

    frontends = {
      react = {
        enable = mkOption {
          type = types.bool;
          default = true;
          description = "Serve the React/Leaflet frontend.";
        };
        port = mkOption {
          type = types.port;
          default = 8765;
          description = "Loopback port for the React frontend backend.";
        };
        subdomain = mkOption {
          type = types.str;
          default = "";
          description = "Subdomain under `domain` (empty = apex).";
        };
        dist = mkOption {
          type = types.str;
          default = "${pkgOr "canyongis-web-frontend"}/share/canyongis-web";
          defaultText = lib.literalExpression ''"''${canyongisPackages.canyongis-web-frontend}/share/canyongis-web"'';
          description = "Built React/Leaflet SPA directory.";
        };
        pluginDist = mkOption {
          type = types.nullOr types.str;
          default = null;
          description = "Optional GeoLibre plugin dist to mount at /plugins/canyongis.";
        };
      };
      geolibre = {
        enable = mkOption {
          type = types.bool;
          default = true;
          description = "Serve the GeoLibre frontend.";
        };
        port = mkOption {
          type = types.port;
          default = 8766;
          description = "Loopback port for the GeoLibre frontend backend.";
        };
        subdomain = mkOption {
          type = types.str;
          default = "geolibre";
          description = "Subdomain under `domain`.";
        };
        dist = mkOption {
          type = types.str;
          default = "${pkgOr "geolibre-web"}/share/geolibre-web";
          defaultText = lib.literalExpression ''"''${canyongisPackages.geolibre-web}/share/geolibre-web"'';
          description = "Built GeoLibre SPA directory (with the canyongis plugin bundled).";
        };
        pluginDist = mkOption {
          type = types.nullOr types.str;
          default = "${pkgOr "canyongis-geolibre-plugin"}/share/canyongis-geolibre-plugin";
          defaultText = lib.literalExpression ''"''${canyongisPackages.canyongis-geolibre-plugin}/share/canyongis-geolibre-plugin"'';
          description = "GeoLibre plugin dist mounted at /plugins/canyongis (serves plugin.json).";
        };
      };
    };
  };

  config = mkIf cfg.enable (mkMerge [
    {
      assertions = [
        {
          assertion = cfg.domain != "";
          message = "services.canyongis.domain must be set.";
        }
        {
          assertion = !cfg.caddy.enable || cfg.acme.email != null;
          message = "services.canyongis.acme.email is required when caddy is enabled (Let's Encrypt).";
        }
      ];

      systemd.services = mkMerge [
        {
          # Create the shared state layout and a single persistent session
          # secret so both frontends issue/accept the same login cookies.
          canyongis-setup = {
            description = "canyongis state and session-secret initialization";
            wantedBy = [ "multi-user.target" ];
            serviceConfig = {
              Type = "oneshot";
              RemainAfterExit = true;
              User = cfg.user;
              Group = cfg.group;
              StateDirectory = "canyongis";
              StateDirectoryMode = "0750";
            };
            script = ''
              mkdir -p ${outputRoot}
              if [ ! -f ${secretEnv} ]; then
                umask 077
                echo "CANYONGIS_SESSION_SECRET=$(${pkgs.openssl}/bin/openssl rand -hex 32)" > ${secretEnv}
              fi
            '';
          };
        }
        (mkIf cfg.frontends.react.enable {
          canyongis-react = mkWebService "react" cfg.frontends.react;
        })
        (mkIf cfg.frontends.geolibre.enable {
          canyongis-geolibre = mkWebService "geolibre" cfg.frontends.geolibre;
        })
        (mkIf cfg.database.createLocally {
          # Migrations run on server start, but the PostGIS extension must exist
          # first; enable it as the postgres superuser before the web services.
          canyongis-postgis-setup = {
            description = "ensure the postgis extension in the canyongis database";
            wantedBy = [ "multi-user.target" ];
            after = [ "postgresql.service" ];
            requires = [ "postgresql.service" ];
            serviceConfig = {
              Type = "oneshot";
              User = "postgres";
            };
            script = ''
              ${config.services.postgresql.package}/bin/psql -d ${cfg.database.name} \
                -c 'CREATE EXTENSION IF NOT EXISTS postgis;'
            '';
          };
        })
      ];
    }

    (mkIf (cfg.user == "canyongis") {
      users.users.canyongis = {
        isSystemUser = true;
        group = cfg.group;
        home = stateRoot;
        description = "canyongis service user";
      };
      users.groups.canyongis = { };
    })

    (mkIf cfg.database.createLocally {
      services.postgresql = {
        enable = true;
        extensions = ps: [ ps.postgis ];
        ensureDatabases = [ cfg.database.name ];
        ensureUsers = [
          {
            name = cfg.database.user;
            ensureDBOwnership = true;
          }
        ];
      };
    })

    (mkIf cfg.caddy.enable {
      services.caddy = {
        enable = true;
        email = cfg.acme.email;
        virtualHosts = lib.mapAttrs' (_: fe: {
          name = fqdn fe.subdomain;
          value.extraConfig = "reverse_proxy 127.0.0.1:${toString fe.port}";
        }) enabledFrontends;
      };

      networking.firewall.allowedTCPPorts = mkIf cfg.openFirewall [
        80
        443
      ];
    })
  ]);
}
