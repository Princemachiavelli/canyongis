{
  lib,
  stdenvNoCC,
  fetchFromGitHub,
}:

stdenvNoCC.mkDerivation rec {
  pname = "rvt-qgis";
  version = "1.0.0";

  src = fetchFromGitHub {
    owner = "EarthObservation";
    repo = "rvt-qgis";
    rev = "160c3b7aca3c9cb6a5158c377e7b7bf470575b6c";
    hash = "sha256-KvBrtSU4FpQGcSb189VcTCWITZgSAasXbKtFuMLUf4o=";
  };

  postPatch = ''
        substituteInPlace qrvt.py \
          --replace-fail 'import os' 'import os
    import shutil'

        substituteInPlace qrvt.py \
          --replace-fail 'self.default_settings_path = os.path.abspath(os.path.join(self.plugin_dir, "settings", "default_settings.json"))' 'self.settings_dir = os.path.abspath(os.path.join(QgsApplication.qgisSettingsDirPath(), "rvt-qgis", "settings"))
            os.makedirs(self.settings_dir, exist_ok=True)
            packaged_settings_dir = os.path.abspath(os.path.join(self.plugin_dir, "settings"))
            for settings_file in (
                "default_settings.json",
                "default_blender_combinations.json",
                "default_terrains_settings.json",
            ):
                packaged_settings_path = os.path.join(packaged_settings_dir, settings_file)
                profile_settings_path = os.path.join(self.settings_dir, settings_file)
                if not os.path.exists(profile_settings_path) and os.path.exists(packaged_settings_path):
                    shutil.copyfile(packaged_settings_path, profile_settings_path)
            self.default_settings_path = os.path.abspath(os.path.join(self.settings_dir, "default_settings.json"))' \
          --replace-fail 'self.default_blender_combinations_path = os.path.abspath(os.path.join(self.plugin_dir, "settings",' 'self.default_blender_combinations_path = os.path.abspath(os.path.join(self.settings_dir,' \
          --replace-fail 'self.terrains_settings_path = os.path.abspath(os.path.join(self.plugin_dir, "settings",' 'self.terrains_settings_path = os.path.abspath(os.path.join(self.settings_dir,' \
          --replace-fail 'self.plugin_size_json_path = os.path.abspath(os.path.join(self.plugin_dir, "settings", "plugin_size.json"))' 'self.plugin_size_json_path = os.path.abspath(os.path.join(self.settings_dir, "plugin_size.json"))'
  '';

  installPhase = ''
    runHook preInstall

    pluginDir="$out/share/qgis/python/plugins/rvt-qgis"
    mkdir -p "$pluginDir"
    cp -R . "$pluginDir"/

    runHook postInstall
  '';

  meta = {
    description = "Relief Visualization Toolbox plugin for QGIS";
    homepage = "https://github.com/EarthObservation/rvt-qgis";
    license = lib.licenses.asl20;
  };
}
