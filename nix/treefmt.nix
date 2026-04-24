{
  perSystem = _: {
    treefmt = {
      projectRootFile = "flake.nix";

      programs = {
        nixfmt.enable = true;
        ruff-format.enable = true;
        ruff-check.enable = true;
        taplo.enable = true;
      };

      settings.global.excludes = [
        "*.lock"
        "*.tif"
        "*.html"
        "*.geojson"
      ];
    };
  };
}
